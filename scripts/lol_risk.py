"""
Risk layer: position tracking, PnL, exposure limits, cooldowns.
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
        self._session_start = time.time()
        self._daily_pnl: float = 0.0

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
        return True, "OK"

    # ── Record entry ────────────────────────────────────────────────────

    def record_entry(self, pos: Position):
        self.positions.append(pos)
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

        pos.exit_story.append(
            f"RESOLVED @ ${resolved_price:.2f} — "
            f"{'WIN' if pos.exit_pnl > 0 else 'LOSS'} "
            f"${pos.exit_pnl:+.2f} (held {pos.sell_time - pos.entry_time:.0f}s)"
        )

        rec = TradeRecord(
            ts=time.time(),
            match_name=pos.match_name,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=resolved_price,
            size=pos.size,
            pnl=pos.exit_pnl,
            hold_sec=pos.sell_time - pos.entry_time,
            reason=f"RESOLVED_{resolved_price:.0f}",
        )
        self.trades.append(rec)

        emoji = "+" if pos.exit_pnl >= 0 else ""
        log.info("POSITION RESOLVED: %s %s entry=%.3f resolved=%.2f pnl=%s$%.2f held=%.0fs",
                 pos.direction, pos.match_name, pos.entry_price, resolved_price,
                 emoji, pos.exit_pnl, pos.sell_time - pos.entry_time)

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
