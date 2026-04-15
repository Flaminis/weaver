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


def _get_impact_model() -> EventImpactModel:
    global _IMPACT_MODEL
    if _IMPACT_MODEL is None:
        _IMPACT_MODEL = EventImpactModel()
        log.info("[SIGNAL] Loaded LightGBM win-probability model")
    return _IMPACT_MODEL


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


TRADEABLE = {EventType.BARON, EventType.INHIBITOR, EventType.DRAKE, EventType.KILL}

_STAT_KEY_MAP = {
    EventType.KILL: "kills",
    EventType.TOWER: "towers",
    EventType.DRAKE: "drakes",
    EventType.BARON: "nashors",
    EventType.INHIBITOR: "inhibitors",
}


def _direction_for_team(team_id: int, team_a_id: int) -> str:
    return "buy_a" if team_id == team_a_id else "buy_b"


def _tier_label(etype: EventType, is_soul: bool, teamfight_kills: int) -> str:
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
    ) -> tuple[Signal | None, str]:
        self.combo.add(event)

        if event.etype == EventType.TOWER:
            return None, "TOWER_SKIP"
        if event.etype == EventType.STATUS:
            return None, "STATUS_SKIP"
        if event.etype not in TRADEABLE:
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
            model = _get_impact_model()
            model_impact, _, _ = model.predict_impact_from_llf(
                game_minute=game_minute,
                team_before=acting_before,
                team_after=acting_after,
                opp_before=opp_before,
                opp_after=opp_after,
                is_blue=is_blue,
            )
        except Exception as exc:
            log.warning("[SIGNAL] Model prediction failed: %s", exc)
            return None, f"MODEL_ERROR_{exc}"

        if model_impact <= 0:
            return None, f"NEG_IMPACT_{model_impact:.4f}"

        teamfight_kills = self.combo.recent_kills(event.team_id, cfg.TEAMFIGHT_WINDOW_SEC)
        combo_types = {e.etype for e in self.combo.recent_events(event.team_id)}
        tier = _tier_label(event.etype, is_soul, teamfight_kills)

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

