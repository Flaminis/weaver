"""
LoL Signal Model — event detection, combo tracking, direction + sizing.

Rule-based v1 (no calibration data). Trades on:
- Baron (always)
- Inhibitor (always)
- Drake (always — soul detection via count tracking)
- Kill (always — but single kills are small; teamfights amplified)

Does NOT trade on:
- Towers (noise, small impact, frequent)

Combo detection: events within 30s amplify each other.
Baron + towers + inhib in sequence = game-ending push signal.

Priced-in gate: if market moved >5c in our direction in last 2s, skip
UNLESS we already hold that direction and it's a consecutive event.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

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
    direction: str          # "buy_a" or "buy_b"  (a = first team listed)
    size_usd: float
    confidence: float       # 0-1
    expected_impact: float  # expected price move in our direction
    reason: str
    events: list[LolEvent]


@dataclass
class ComboWindow:
    """Tracks recent events for combo amplification."""
    events: list[LolEvent] = field(default_factory=list)

    def add(self, ev: LolEvent):
        self.events.append(ev)
        cutoff = time.time() - cfg.COMBO_WINDOW_SEC
        self.events = [e for e in self.events if e.ts >= cutoff]

    def recent_kills(self, team_id: int, window_sec: float = None) -> int:
        w = window_sec or cfg.TEAMFIGHT_WINDOW_SEC
        cutoff = time.time() - w
        return sum(
            e.delta for e in self.events
            if e.etype == EventType.KILL and e.team_id == team_id and e.ts >= cutoff
        )

    def recent_events(self, team_id: int) -> list[LolEvent]:
        cutoff = time.time() - cfg.COMBO_WINDOW_SEC
        return [e for e in self.events if e.team_id == team_id and e.ts >= cutoff]

    def has_baron(self, team_id: int) -> bool:
        cutoff = time.time() - cfg.COMBO_WINDOW_SEC
        return any(
            e.etype == EventType.BARON and e.team_id == team_id and e.ts >= cutoff
            for e in self.events
        )


TRADEABLE_EVENTS = {EventType.BARON, EventType.INHIBITOR, EventType.DRAKE, EventType.KILL}


class SignalModel:
    """
    Stateful signal model per match.
    Tracks drake counts for soul detection, combos, and priced-in state.
    """

    def __init__(self, team_a_id: int, team_b_id: int):
        self.team_a_id = team_a_id
        self.team_b_id = team_b_id
        self.combo = ComboWindow()
        self._prev_mid: float = 0.0
        self._drake_counts: dict[int, int] = {team_a_id: 0, team_b_id: 0}
        self._initialized = False
        self._event_count = 0

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
        """
        Process a single LLF event and decide whether to trade.

        Returns (Signal, skip_reason).
        Signal is None if no trade; skip_reason explains why.
        """

        if not self._initialized:
            self._prev_mid = mid_a
            self._initialized = True

        self._event_count += 1
        self.combo.add(event)

        if self._event_count == 1:
            self._prev_mid = mid_a
            return None, "WARMUP_FIRST_EVENT"

        if event.etype == EventType.TOWER:
            self._prev_mid = mid_a
            return None, "TOWER_SKIP"

        if event.etype == EventType.STATUS:
            self._prev_mid = mid_a
            return None, "STATUS_SKIP"

        if event.etype not in TRADEABLE_EVENTS:
            self._prev_mid = mid_a
            return None, f"NOT_TRADEABLE_{event.etype.value}"

        # ── Drake soul detection ────────────────────────────────────────
        is_soul_drake = False
        if event.etype == EventType.DRAKE:
            self._drake_counts[event.team_id] = event.new_value
            if event.new_value >= 4:
                is_soul_drake = True

        # ── Spread gate ─────────────────────────────────────────────────
        if spread > cfg.MAX_SPREAD:
            self._prev_mid = mid_a
            return None, f"SPREAD_WIDE_{spread:.3f}"

        # ── Direction ───────────────────────────────────────────────────
        team_side = self._team_side(event.team_id)
        direction = "buy_a" if team_side == "a" else "buy_b"
        buy_price = round(ask_a, 2) if direction == "buy_a" else round(1.0 - bid_a, 2)

        # ── Price band gate ─────────────────────────────────────────────
        if buy_price < cfg.TRADE_MIN_PRICE or buy_price > cfg.TRADE_MAX_PRICE:
            self._prev_mid = mid_a
            return None, f"PRICE_BAND_{buy_price:.3f}"

        # ── Near resolved ───────────────────────────────────────────────
        if mid_a < cfg.NEAR_RESOLVED_FLOOR or mid_a > cfg.NEAR_RESOLVED_CEIL:
            self._prev_mid = mid_a
            return None, f"NEAR_RESOLVED_{mid_a:.3f}"

        # ── Combo tracking (for priced-in bypass logic) ──────────────────
        combo_events = self.combo.recent_events(event.team_id)
        combo_types = {e.etype for e in combo_events}
        teamfight_kills = self.combo.recent_kills(event.team_id, cfg.TEAMFIGHT_WINDOW_SEC)

        # ── Priced-in gate ──────────────────────────────────────────────
        # Use actual 2-second market move from WebSocket price history.
        # If market already moved >5c in our direction, skip
        # UNLESS we already hold that direction AND either:
        #   - consecutive kills (2+ in teamfight window)
        #   - baron/inhib/soul drake (always worth adding to)
        #   - multi-event combo (3+ event types in combo window)
        if direction == "buy_a":
            directional_move = recent_move_2s
        else:
            directional_move = -recent_move_2s

        already_priced = directional_move > cfg.PRICED_IN_THRESHOLD

        if already_priced:
            already_holding = (holding_direction == direction)

            bypass = False
            bypass_reason = ""

            if already_holding:
                if event.etype == EventType.BARON:
                    bypass = True
                    bypass_reason = "BARON_WHILE_HOLDING"
                elif event.etype == EventType.INHIBITOR:
                    bypass = True
                    bypass_reason = "INHIB_WHILE_HOLDING"
                elif event.etype == EventType.DRAKE and is_soul_drake:
                    bypass = True
                    bypass_reason = "SOUL_WHILE_HOLDING"
                elif event.etype == EventType.KILL and self.combo.recent_kills(event.team_id, cfg.TEAMFIGHT_WINDOW_SEC) >= 2:
                    bypass = True
                    bypass_reason = "CONSECUTIVE_KILLS_WHILE_HOLDING"
                elif len({e.etype for e in self.combo.recent_events(event.team_id)}) >= 3:
                    bypass = True
                    bypass_reason = "MULTI_COMBO_WHILE_HOLDING"

            if not bypass:
                self._prev_mid = mid_a
                return None, f"PRICED_IN_{directional_move:.3f}_move2s={recent_move_2s:.3f}"

        self._prev_mid = mid_a

        # ── Sizing (flat $10 per event) ─────────────────────────────────
        size_usd = cfg.BET_SIZE_BASE

        # ── Build reason string ─────────────────────────────────────────
        parts = [event.etype.value.upper()]
        if is_soul_drake:
            parts.append("SOUL")
        if teamfight_kills >= cfg.TEAMFIGHT_KILL_THRESHOLD:
            parts.append(f"TF{teamfight_kills}k")
        if self.combo.has_baron(event.team_id):
            parts.append("BARON_COMBO")
        if len(combo_types) >= 3:
            parts.append(f"MULTI{len(combo_types)}")
        reason = " ".join(parts)

        signal = Signal(
            direction=direction,
            size_usd=size_usd,
            confidence=1.0,
            expected_impact=0.0,
            reason=reason,
            events=[event],
        )

        return signal, "TRADE"


