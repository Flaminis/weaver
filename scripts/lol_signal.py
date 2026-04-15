"""
LoL Signal Model v2 — tiered objectives, kill-stack gates, edge vs spread.

Design goals (differs from v1):
- Do NOT fire on isolated +1 kills (main source of -spread churn).
- Only trade kills when the same team has stacked kills in a short window (skirmish).
- Objectives (baron / inhib / drake) use conservative prior "impact" estimates.
- Enforce cfg.MIN_EDGE: expected_impact must exceed spread + MIN_EDGE (else skip).
- Size scales with event severity; still capped by cfg.MAX_SINGLE_BET.
- Priced-in: stricter on noise (kills); objectives get a wider skip threshold.

Tower / status events are never traded.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Tuple

import lol_trader_config as cfg


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
    size_usd: float
    confidence: float
    expected_impact: float  # prior edge in price space (same units as mid)
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

    def had_objective_since(
        self,
        team_id: int,
        types: tuple[EventType, ...],
        within_sec: float,
    ) -> bool:
        cutoff = time.time() - within_sec
        return any(
            e.etype in types and e.team_id == team_id and e.ts >= cutoff
            for e in self.events
        )


TRADEABLE = {EventType.BARON, EventType.INHIBITOR, EventType.DRAKE, EventType.KILL}


def _direction_for_team(team_id: int, team_a_id: int) -> str:
    return "buy_a" if team_id == team_a_id else "buy_b"


def _tier_impact_and_size(
    etype: EventType,
    is_soul: bool,
    teamfight_kills: int,
    had_obj_followup: bool,
) -> Tuple[float, float, str]:
    """
    Return (expected_impact prior, size multiplier vs base, reason suffix).

    Impacts are deliberately conservative fractions of $1 — not calibrated ML,
    but used only vs MIN_EDGE + spread so obviously weak prints (single kills) fail.
    """
    if etype == EventType.BARON:
        return 0.09, 1.35, "BARON"
    if etype == EventType.INHIBITOR:
        return 0.055, 1.15, "INHIB"
    if etype == EventType.DRAKE:
        if is_soul:
            return 0.065, 1.25, "DRAKE_SOUL"
        return 0.038, 1.0, "DRAKE"
    if etype == EventType.KILL:
        # Kill path only reached after stack / teamfight gating
        if teamfight_kills >= cfg.TEAMFIGHT_KILL_THRESHOLD:
            mult = 1.15
            tier = f"TF{teamfight_kills}k"
        elif had_obj_followup:
            mult = 1.05
            tier = "POST_OBJ"
        else:
            mult = 1.0
            tier = f"STK{teamfight_kills}"
        return 0.05, mult, tier  # stacked kills: barely clears MIN_EDGE + typical 1–2c sprd
    return 0.0, 0.0, "NONE"


class SignalModel:
    """
    Per-match state: rolling combo for kill counts; v2 gating.
    """

    def __init__(self, team_a_id: int, team_b_id: int):
        self.team_a_id = team_a_id
        self.team_b_id = team_b_id
        self.combo = ComboWindow()
        self._drake_counts: dict[int, int] = {team_a_id: 0, team_b_id: 0}
        self._initialized = False

    def _team_side(self, team_id: int) -> str:
        return "a" if team_id == self.team_a_id else "b"

    def on_event(
        self,
        event: LolEvent,
        mid_a: float,
        bid_a: float,
        ask_a: float,
        spread: float,
        holding_direction: str | None = None,
        recent_move_2s: float = 0.0,
    ) -> tuple[Signal | None, str]:
        if not self._initialized:
            self._initialized = True

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

        # Liquidity / price hygiene (same as before, explicit)
        if spread > cfg.MAX_SPREAD:
            return None, f"SPREAD_WIDE_{spread:.3f}"

        direction = _direction_for_team(event.team_id, self.team_a_id)
        buy_price = round(ask_a, 2) if direction == "buy_a" else round(1.0 - bid_a, 2)
        if buy_price < cfg.TRADE_MIN_PRICE or buy_price > cfg.TRADE_MAX_PRICE:
            return None, f"PRICE_BAND_{buy_price:.3f}"
        if mid_a < cfg.NEAR_RESOLVED_FLOOR or mid_a > cfg.NEAR_RESOLVED_CEIL:
            return None, f"NEAR_RESOLVED_{mid_a:.3f}"

        # Priced-in directional move (positive = toward team A token)
        if direction == "buy_a":
            directional_move = recent_move_2s
        else:
            directional_move = -recent_move_2s
        already_priced_lo = directional_move > cfg.PRICED_IN_THRESHOLD
        already_priced_hi = directional_move > cfg.PRICED_IN_THRESHOLD * 1.6

        teamfight_kills = self.combo.recent_kills(event.team_id, cfg.TEAMFIGHT_WINDOW_SEC)
        combo_types = {e.etype for e in self.combo.recent_events(event.team_id)}
        post_obj = self.combo.had_objective_since(
            event.team_id,
            (EventType.BARON, EventType.INHIBITOR, EventType.DRAKE),
            within_sec=cfg.POST_OBJECTIVE_KILL_WINDOW_SEC,
        )

        # ── Kill gating (v2): no trade on first blood / isolated trades ──
        if event.etype == EventType.KILL:
            if teamfight_kills < cfg.MIN_STACKED_KILLS:
                if not post_obj:
                    return None, f"KILL_THIN_stack={teamfight_kills}"
            # If market already ran on a kill print, skip — kills are the noisiest leg
            if already_priced_lo:
                if not (holding_direction == direction):
                    return None, f"PRICED_IN_KILL_mv={directional_move:.3f}"
                # Allow adding while holding only on serious fight
                if teamfight_kills < cfg.TEAMFIGHT_KILL_THRESHOLD:
                    return None, f"PRICED_HOLD_NEED_TF_mv={directional_move:.3f}"

        # Objectives: skip if the whole move already happened (wider bar for real objs)
        if event.etype in (EventType.BARON, EventType.INHIBITOR, EventType.DRAKE):
            if already_priced_hi:
                if holding_direction != direction:
                    return None, f"PRICED_OBJ_mv={directional_move:.3f}"
                if event.etype != EventType.BARON:
                    return None, f"PRICED_ADD_SKIP_{event.etype.value}"

        impact, size_mult, tier = _tier_impact_and_size(
            event.etype, is_soul, teamfight_kills, post_obj
        )
        if impact <= 0:
            return None, "NO_IMPACT_TIER"

        # Core edge check — this is what v1 never did with MIN_EDGE
        edge_after_spread = impact - spread
        if edge_after_spread < cfg.MIN_EDGE:
            return None, f"LOW_EDGE_imp{impact:.3f}_spr{spread:.3f}_need{cfg.MIN_EDGE:.3f}"

        size_usd = min(cfg.BET_SIZE_BASE * size_mult, cfg.MAX_SINGLE_BET)
        # Small bump when multiple event types in combo window (real scrap)
        if len(combo_types) >= 3 and event.etype == EventType.KILL:
            size_usd = min(size_usd * 1.08, cfg.MAX_SINGLE_BET)

        reason_parts = [tier]
        if is_soul:
            reason_parts.append("SOUL")
        if len(combo_types) >= 3:
            reason_parts.append(f"MIX{len(combo_types)}")
        reason = " ".join(reason_parts)

        signal = Signal(
            direction=direction,
            size_usd=round(size_usd, 2),
            confidence=min(0.95, 0.55 + impact * 4),
            expected_impact=round(impact, 4),
            reason=reason,
            events=[event],
        )
        return signal, "TRADE"

