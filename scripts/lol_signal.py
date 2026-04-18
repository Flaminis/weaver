"""
LoL Signal Model v3 — ML-driven impact, EV-based edge gating.

Uses a trained LightGBM model (EventImpactModel) to predict the win-probability
shift from each in-game event. The trader downstream computes:
  p_fair = pre_event_market_mid + model_impact
  edge   = p_fair - ask * (1 + taker_fee)
and sizes via Kelly criterion.

Tower / status events are never traded.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import lol_trader_config as cfg
from training.event_impact import EventImpactModel

log = logging.getLogger("lol_signal")

_IMPACT_MODEL: EventImpactModel | None = None
_FALLBACK_V1_MODEL: EventImpactModel | None = None


def _primary_model_path() -> "Path":
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent / "data" / "models"
    choice = getattr(cfg, "PRIMARY_MODEL", "v2")
    if choice == "v1":
        return base / "winprob_lgbm.joblib"
    return base / "winprob_lgbm_v2.joblib"


def _get_impact_model() -> EventImpactModel:
    """Primary inference model per PRIMARY_MODEL config. Lazily loaded."""
    global _IMPACT_MODEL
    if _IMPACT_MODEL is None:
        _IMPACT_MODEL = EventImpactModel(_primary_model_path())
        log.info("[SIGNAL] Loaded primary model: %s (%s, %d features)",
                 _IMPACT_MODEL.model_path.name,
                 "v2" if _IMPACT_MODEL.is_v2 else "v1",
                 len(_IMPACT_MODEL.features))
    return _IMPACT_MODEL


def _get_fallback_v1_model() -> EventImpactModel | None:
    """Lazily loaded v1 model for defensive fallback if v2 inference throws.
    Returns None if v1 file isn't present (shouldn't happen — baseline ships
    alongside v2)."""
    global _FALLBACK_V1_MODEL
    if _FALLBACK_V1_MODEL is not None:
        return _FALLBACK_V1_MODEL
    from pathlib import Path
    v1_path = Path(__file__).resolve().parent.parent / "data" / "models" / "winprob_lgbm.joblib"
    if not v1_path.exists():
        return None
    try:
        _FALLBACK_V1_MODEL = EventImpactModel(v1_path)
        log.info("[SIGNAL] Loaded v1 fallback model: %s (%d features)",
                 v1_path.name, len(_FALLBACK_V1_MODEL.features))
    except Exception as exc:
        log.warning("[SIGNAL] Could not load v1 fallback model: %s", exc)
        return None
    return _FALLBACK_V1_MODEL


def reset_impact_model_for_test() -> None:
    """Test-only: clear cached models so the next call reloads from disk."""
    global _IMPACT_MODEL, _FALLBACK_V1_MODEL
    _IMPACT_MODEL = None
    _FALLBACK_V1_MODEL = None


# ── Safe inference wrappers ───────────────────────────────────────────
# If primary (v2) inference throws (malformed feature row, NaN, whatever),
# we DO NOT want the bot to skip the event — we want graceful degradation
# to the baseline model which we trust from months of live use. Only skip
# if BOTH fail.

def _predict_once(game_minute: float, team_stats: dict, opponent_stats: dict,
                   is_blue: bool = True, **kwargs) -> float:
    """One predict_win_prob with primary → v1 fallback. No smoothing."""
    primary = _get_impact_model()
    try:
        return primary.predict_win_prob(game_minute, team_stats, opponent_stats,
                                         is_blue=is_blue, **kwargs)
    except Exception as exc:
        if not getattr(cfg, "MODEL_FALLBACK_TO_V1_ON_ERROR", True):
            raise
        log.warning("[SIGNAL] primary model predict_win_prob failed: %s (%s) — trying v1 fallback",
                    type(exc).__name__, exc)
        fb = _get_fallback_v1_model()
        if fb is None or fb is primary:
            raise
        # v1 ignores state_history/champs/pin_totals via is_v2=False; strip
        # defensively so signature drift can't surface.
        safe_kw = {k: v for k, v in kwargs.items()
                   if k not in ("state_history", "current_ts", "team_champs",
                                "opp_champs", "pin_totals_from")}
        return fb.predict_win_prob(game_minute, team_stats, opponent_stats,
                                    is_blue=is_blue, **safe_kw)


def safe_predict_win_prob(game_minute: float, team_stats: dict, opponent_stats: dict,
                           is_blue: bool = True, **kwargs) -> float:
    """predict_win_prob with primary → v1-fallback + optional game_minute
    smoothing across ±(MODEL_SMOOTH_WINDOW_SEC/2) averaged over
    MODEL_SMOOTH_N_SAMPLES points. Smoothing flattens tree-split artifacts
    in the game_minute feature so consecutive predictions at static state
    drift smoothly instead of stepping. Controlled by a single config
    knob; setting MODEL_SMOOTH_WINDOW_SEC=0 disables smoothing everywhere.
    """
    window_sec = float(getattr(cfg, "MODEL_SMOOTH_WINDOW_SEC", 0))
    n_samples = int(getattr(cfg, "MODEL_SMOOTH_N_SAMPLES", 1))
    if window_sec <= 0 or n_samples < 2:
        return _predict_once(game_minute, team_stats, opponent_stats,
                              is_blue=is_blue, **kwargs)
    offsets_min = [((i / (n_samples - 1)) - 0.5) * (window_sec / 60.0)
                   for i in range(n_samples)]
    probs: list[float] = []
    for off in offsets_min:
        gm = max(0.0, game_minute + off)
        try:
            probs.append(_predict_once(gm, team_stats, opponent_stats,
                                         is_blue=is_blue, **kwargs))
        except Exception:
            pass
    if not probs:
        # Re-raise via one more unsmoothed attempt so the caller sees the error
        return _predict_once(game_minute, team_stats, opponent_stats,
                              is_blue=is_blue, **kwargs)
    return sum(probs) / len(probs)


def safe_predict_impact_from_llf(
    game_minute: float,
    team_before: dict, team_after: dict,
    opp_before: dict, opp_after: dict,
    is_blue: bool = True,
    **kwargs,
) -> tuple[float, float, float]:
    """predict_impact_from_llf decomposed into two smoothed safe_predict_win_prob
    calls with pin_totals semantics preserved. Both p_before and p_after are
    averaged across the same ±window of game_minute, so their delta is robust
    to tree splits in the game_minute feature."""
    p_before = safe_predict_win_prob(
        game_minute, team_before, opp_before, is_blue=is_blue, **kwargs,
    )
    # Pin totals to BEFORE state so monotone diff features drive the sign.
    blue_before = team_before if is_blue else opp_before
    red_before = opp_before if is_blue else team_before
    p_after = safe_predict_win_prob(
        game_minute, team_after, opp_after, is_blue=is_blue,
        pin_totals_from=(blue_before, red_before), **kwargs,
    )
    return p_after - p_before, p_before, p_after


class EventType(str, Enum):
    KILL = "kill"
    TOWER = "tower"
    DRAKE = "drake"
    BARON = "baron"
    INHIBITOR = "inhibitor"
    STATUS = "status"


@dataclass
class LolEvent:
    ts: float
    etype: EventType
    team_id: int
    side: str
    delta: int
    game_position: int
    game_timer_sec: int
    new_value: int
    old_value: int


@dataclass
class Signal:
    direction: str          # "buy_a" or "buy_b"
    size_usd: float         # placeholder — trader computes via Kelly
    confidence: float
    expected_impact: float  # ML model's win-probability delta
    reason: str
    events: list[LolEvent]


@dataclass
class ComboWindow:
    events: list[LolEvent] = field(default_factory=list)

    def add(self, ev: LolEvent):
        self.events.append(ev)
        cutoff = time.time() - cfg.COMBO_WINDOW_SEC
        self.events = [e for e in self.events if e.ts >= cutoff]

    def recent_kills(self, team_id: int, window_sec: float | None = None) -> int:
        w = window_sec or cfg.TEAMFIGHT_WINDOW_SEC
        cutoff = time.time() - w
        return sum(
            e.delta
            for e in self.events
            if e.etype == EventType.KILL and e.team_id == team_id and e.ts >= cutoff
        )

    def recent_events(self, team_id: int) -> list[LolEvent]:
        cutoff = time.time() - cfg.COMBO_WINDOW_SEC
        return [e for e in self.events if e.team_id == team_id and e.ts >= cutoff]


def _tradeable_set() -> set:
    """Read fresh each call so flipping ENABLE_TOWER_TRADING in config +
    restart takes effect. No module import caching needed."""
    base = {EventType.BARON, EventType.INHIBITOR, EventType.DRAKE, EventType.KILL}
    if getattr(cfg, "ENABLE_TOWER_TRADING", False):
        base.add(EventType.TOWER)
    return base

_STAT_KEY_MAP = {
    EventType.KILL: "kills",
    EventType.TOWER: "towers",
    EventType.DRAKE: "drakes",
    EventType.BARON: "nashors",
    EventType.INHIBITOR: "inhibitors",
}


def _direction_for_team(team_id: int, team_a_id: int) -> str:
    return "buy_a" if team_id == team_a_id else "buy_b"


def _tier_label(etype: EventType, is_soul: bool, teamfight_kills: int,
                 tower_index: int = 0) -> str:
    if etype == EventType.BARON:
        return "BARON"
    if etype == EventType.INHIBITOR:
        return "INHIB"
    if etype == EventType.DRAKE:
        return "DRAKE_SOUL" if is_soul else "DRAKE"
    if etype == EventType.KILL:
        if teamfight_kills >= 3:
            return f"TF{teamfight_kills}k"
        if teamfight_kills == 2:
            return "STK2"
        return "KILL1"
    if etype == EventType.TOWER:
        # Nth tower by a single team — 1-3 is outer/mid, 4+ is inhib-tier.
        # Late towers tend to carry the real macro signal; label accordingly.
        if tower_index >= 8:
            return "TOWER_NEXUS"
        if tower_index >= 4:
            return "TOWER_INHIB"
        return f"TOWER{tower_index}"
    return "NONE"


class SignalModel:
    """Per-match state: rolling combo for kill counts; ML-based impact."""

    def __init__(self, team_a_id: int, team_b_id: int):
        self.team_a_id = team_a_id
        self.team_b_id = team_b_id
        self.combo = ComboWindow()
        self._drake_counts: dict[int, int] = {team_a_id: 0, team_b_id: 0}

    def on_event(
        self,
        event: LolEvent,
        mid_a: float,
        bid_a: float,
        ask_a: float,
        spread: float,
        prev_teams: dict[int, dict] | None = None,
        state_history: list[tuple[float, dict]] | None = None,
        acting_champs: list[str] | None = None,
        opp_champs: list[str] | None = None,
    ) -> tuple[Signal | None, str]:
        self.combo.add(event)

        if event.etype == EventType.STATUS:
            return None, "STATUS_SKIP"
        tradeable = _tradeable_set()
        if event.etype == EventType.TOWER and event.etype not in tradeable:
            return None, "TOWER_SKIP"
        if event.etype not in tradeable:
            return None, f"NOT_TRADEABLE_{event.etype.value}"

        is_soul = False
        if event.etype == EventType.DRAKE:
            self._drake_counts[event.team_id] = event.new_value
            is_soul = event.new_value >= 4

        if spread > cfg.MAX_SPREAD:
            return None, f"SPREAD_WIDE_{spread:.3f}"

        direction = _direction_for_team(event.team_id, self.team_a_id)
        buy_price = round(ask_a, 2) if direction == "buy_a" else round(1.0 - bid_a, 2)
        if buy_price < cfg.TRADE_MIN_PRICE or buy_price > cfg.TRADE_MAX_PRICE:
            return None, f"PRICE_BAND_{buy_price:.3f}"
        if mid_a < cfg.NEAR_RESOLVED_FLOOR or mid_a > cfg.NEAR_RESOLVED_CEIL:
            return None, f"NEAR_RESOLVED_{mid_a:.3f}"

        if prev_teams is None:
            return None, "NO_GAME_STATE"

        acting_id = event.team_id
        opponent_id = self.team_b_id if acting_id == self.team_a_id else self.team_a_id

        acting_before = prev_teams.get(acting_id)
        opp_before = prev_teams.get(opponent_id)
        if not acting_before or not opp_before:
            return None, "MISSING_TEAM_STATE"

        acting_after = {**acting_before}
        stat_key = _STAT_KEY_MAP.get(event.etype)
        if stat_key:
            acting_after[stat_key] = event.new_value
        opp_after = {**opp_before}

        is_blue = str(acting_before.get("side", "")).lower().startswith("blu")
        game_minute = event.game_timer_sec / 60.0

        try:
            model_impact, _, _ = safe_predict_impact_from_llf(
                game_minute=game_minute,
                team_before=acting_before,
                team_after=acting_after,
                opp_before=opp_before,
                opp_after=opp_after,
                is_blue=is_blue,
                state_history=state_history, current_ts=event.ts,
                team_champs=acting_champs, opp_champs=opp_champs,
            )
        except Exception as exc:
            log.warning("[SIGNAL] Both primary and fallback model failed: %s", exc)
            return None, f"MODEL_ERROR_{exc}"

        if model_impact <= 0:
            return None, f"NEG_IMPACT_{model_impact:.4f}"

        teamfight_kills = self.combo.recent_kills(event.team_id, cfg.TEAMFIGHT_WINDOW_SEC)
        combo_types = {e.etype for e in self.combo.recent_events(event.team_id)}
        # For TOWER events, event.new_value is the team's cumulative tower count —
        # passed to _tier_label so it can distinguish outer (1-3) from inhib-tier (4+).
        tier = _tier_label(event.etype, is_soul, teamfight_kills,
                           tower_index=int(event.new_value) if event.etype == EventType.TOWER else 0)

        reason_parts = [tier]
        if is_soul:
            reason_parts.append("SOUL")
        if len(combo_types) >= 3:
            reason_parts.append(f"MIX{len(combo_types)}")
        reason = " ".join(reason_parts)

        signal = Signal(
            direction=direction,
            size_usd=0.0,
            confidence=min(0.95, 0.50 + model_impact * 3),
            expected_impact=round(model_impact, 4),
            reason=reason,
            events=[event],
        )
        return signal, "TRADE"

