"""
Risk layer: circuit breaker, position tracking, PnL, exposure limits, cooldowns.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import lol_trader_config as cfg
from polymarket.logger import get_logger

log = get_logger("risk")


@dataclass
class Position:
    match_id: int
    match_name: str
    direction: str              # "buy_a" or "buy_b"
    token_id: str
    entry_price: float
    size: float                 # shares
    cost_usd: float             # entry_price * size
    entry_time: float
    entry_game_min: int
    signal_reason: str
    neg_risk: bool = False

    sell_order_id: str = ""
    sell_price: float = 0.0
    sell_time: float = 0.0
    exit_pnl: float = 0.0
    closed: bool = False
    exit_story: list = field(default_factory=list)


@dataclass
class TradeRecord:
    ts: float
    match_name: str
    direction: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    hold_sec: float
    reason: str


class RiskManager:
    def __init__(self, bankroll: float = 500.0):
        self.bankroll = bankroll
        self.capital = bankroll
        self.positions: list[Position] = []
        self.trades: list[TradeRecord] = []
        self._token_cooldowns: dict[str, float] = {}
        self._match_cooldowns: dict[int, float] = {}
        self._consecutive_losses: int = 0
        self._circuit_breaker_until: float = 0.0
        self._session_start = time.time()
        self._daily_pnl: float = 0.0

    # ── Circuit breaker ─────────────────────────────────────────────────

    @property
    def circuit_active(self) -> bool:
        if self._circuit_breaker_until > time.time():
            return True
        if self._daily_pnl < -cfg.DAILY_LOSS_LIMIT:
            return True
        return False

    @property
    def circuit_seconds_left(self) -> float:
        """Seconds until time-based breaker expires. 0 if not active or daily-loss triggered."""
        remaining = self._circuit_breaker_until - time.time()
        return max(0.0, remaining)

    @property
    def circuit_reason(self) -> str:
        if self._circuit_breaker_until > time.time():
            return f"{self._consecutive_losses} consecutive losses"
        if self._daily_pnl < -cfg.DAILY_LOSS_LIMIT:
            return f"daily loss ${self._daily_pnl:.2f} exceeds -${cfg.DAILY_LOSS_LIMIT:.0f} limit"
        return ""

    def _trigger_circuit_breaker(self, reason: str):
        self._circuit_breaker_until = time.time() + cfg.CIRCUIT_BREAKER_MINUTES * 60
        log.warning("CIRCUIT BREAKER triggered: %s — paused for %d min",
                     reason, cfg.CIRCUIT_BREAKER_MINUTES)

    # ── Exposure ────────────────────────────────────────────────────────

    @property
    def total_exposure(self) -> float:
        return sum(p.cost_usd for p in self.positions if not p.closed)

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if not p.closed]

    def position_for_token(self, token_id: str) -> Position | None:
        for p in self.positions:
            if p.token_id == token_id and not p.closed:
                return p
        return None

    def holding_direction_for_match(self, match_id: int) -> str | None:
        for p in self.positions:
            if p.match_id == match_id and not p.closed:
                return p.direction
        return None

    # ── Pre-trade checks ────────────────────────────────────────────────

    def check_entry(self, token_id: str, match_id: int, size_usd: float) -> tuple[bool, str]:
        now = time.time()

        if self.circuit_active:
            return False, "CIRCUIT_BREAKER"

        cd = self._token_cooldowns.get(token_id, 0)
        if now < cd:
            return False, f"TOKEN_COOLDOWN_{cd - now:.0f}s"

        mcd = self._match_cooldowns.get(match_id, 0)
        if now < mcd:
            return False, f"MATCH_COOLDOWN_{mcd - now:.0f}s"

        if self.total_exposure + size_usd > cfg.MAX_TOTAL_EXPOSURE:
            return False, f"EXPOSURE_{self.total_exposure:.1f}+{size_usd:.1f}>{cfg.MAX_TOTAL_EXPOSURE}"

        existing = self.position_for_token(token_id)
        if existing:
            return False, "ALREADY_POSITIONED"

        return True, "OK"

    # ── Record entry ────────────────────────────────────────────────────

    def record_entry(self, pos: Position):
        self.positions.append(pos)
        self._token_cooldowns[pos.token_id] = time.time() + cfg.TOKEN_COOLDOWN_SEC
        self._match_cooldowns[pos.match_id] = time.time() + cfg.MATCH_COOLDOWN_SEC
        log.info("POSITION OPENED: %s %s %.1f shares @ %.3f ($%.2f) — %s",
                 pos.direction, pos.match_name, pos.size, pos.entry_price,
                 pos.cost_usd, pos.signal_reason)

    # ── Record exit ─────────────────────────────────────────────────────

    def record_exit(self, pos: Position, exit_price: float, fill_size: float):
        pos.sell_price = exit_price
        pos.sell_time = time.time()
        pos.exit_pnl = (exit_price - pos.entry_price) * fill_size
        pos.closed = True

        self._daily_pnl += pos.exit_pnl
        self.capital += pos.exit_pnl

        rec = TradeRecord(
            ts=time.time(),
            match_name=pos.match_name,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=fill_size,
            pnl=pos.exit_pnl,
            hold_sec=pos.sell_time - pos.entry_time,
            reason=pos.signal_reason,
        )
        self.trades.append(rec)

        if pos.exit_pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= cfg.MAX_CONSECUTIVE_LOSSES:
                self._trigger_circuit_breaker(
                    f"{self._consecutive_losses} consecutive losses")
        else:
            self._consecutive_losses = 0

        emoji = "+" if pos.exit_pnl >= 0 else ""
        log.info("POSITION CLOSED: %s %s entry=%.3f exit=%.3f pnl=%s$%.2f hold=%.0fs",
                 pos.direction, pos.match_name, pos.entry_price, exit_price,
                 emoji, pos.exit_pnl, pos.sell_time - pos.entry_time)

    # ── Resolve (match ended, no sell needed) ───────────────────────────

    def resolve_position(self, pos: Position, resolved_price: float):
        pos.sell_price = resolved_price
        pos.sell_time = time.time()
        pos.exit_pnl = (resolved_price - pos.entry_price) * pos.size
        pos.closed = True
        self._daily_pnl += pos.exit_pnl
        self.capital += pos.exit_pnl
        log.info("POSITION RESOLVED: %s %s pnl=$%.2f (price=%.2f)",
                 pos.direction, pos.match_name, pos.exit_pnl, resolved_price)

    # ── Session report ──────────────────────────────────────────────────

    def session_report(self) -> str:
        elapsed = time.time() - self._session_start
        n = len(self.trades)
        wins = sum(1 for t in self.trades if t.pnl > 0)
        losses = sum(1 for t in self.trades if t.pnl < 0)
        total_pnl = sum(t.pnl for t in self.trades)
        avg_pnl = total_pnl / n if n > 0 else 0
        wr = wins / n * 100 if n > 0 else 0

        lines = [
            f"{'='*60}",
            f"  SESSION REPORT ({elapsed/60:.0f} min)",
            f"{'='*60}",
            f"  Trades: {n} ({wins}W / {losses}L)",
            f"  Win rate: {wr:.0f}%",
            f"  Total PnL: ${total_pnl:.2f}",
            f"  Avg PnL: ${avg_pnl:.2f}",
            f"  Capital: ${self.capital:.2f} (started ${self.bankroll:.2f})",
            f"  Open positions: {len(self.open_positions)}",
            f"{'='*60}",
        ]
        return "\n".join(lines)

    def save_trades(self, path: Path | None = None):
        if not self.trades and not self.positions:
            return
        if path is None:
            log_dir = Path(__file__).parent / cfg.LOG_TRADES_DIR
            log_dir.mkdir(exist_ok=True)
            path = log_dir / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        closed_trades = []
        for t in self.trades:
            closed_trades.append({
                "ts": t.ts,
                "match": t.match_name,
                "direction": t.direction,
                "entry": t.entry_price,
                "exit": t.exit_price,
                "size": t.size,
                "pnl": round(t.pnl, 4),
                "hold_sec": round(t.hold_sec, 1),
                "reason": t.reason,
            })

        all_positions = []
        for p in self.positions:
            all_positions.append({
                "match_id": p.match_id,
                "match_name": p.match_name,
                "direction": p.direction,
                "entry_price": p.entry_price,
                "size": p.size,
                "cost_usd": p.cost_usd,
                "entry_time": p.entry_time,
                "signal_reason": p.signal_reason,
                "closed": p.closed,
                "sell_price": p.sell_price,
                "exit_pnl": round(p.exit_pnl, 4),
            })

        session = {
            "session_start": self._session_start,
            "session_end": time.time(),
            "bankroll": self.bankroll,
            "final_capital": self.capital,
            "daily_pnl": round(self._daily_pnl, 4),
            "total_trades": len(self.trades),
            "trades": closed_trades,
            "positions": all_positions,
        }

        with open(path, "w") as f:
            json.dump(session, f, indent=2)
        log.info("Saved session (%d trades, %d positions) to %s",
                 len(closed_trades), len(all_positions), path)
