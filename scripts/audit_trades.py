#!/usr/bin/env python3
"""
Fetch /api/state and review the last N closed trades: PnL, hold time, sanity flags.
Run while lol_trader.py is up:  python3 scripts/audit_trades.py

Does not place orders — read-only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

import lol_trader_config as cfg


def main() -> int:
    p = argparse.ArgumentParser(description="Audit recent trades from Oracle-LoL /api/state")
    p.add_argument("--url", default="http://38.180.152.197:8430", help="Trader HTTP base (production VPS)")
    p.add_argument("-n", type=int, default=10, help="How many recent trades to show")
    args = p.parse_args()

    try:
        r = httpx.get(f"{args.url.rstrip('/')}/api/state", timeout=8)
    except httpx.ConnectError as e:
        print(f"Cannot connect to {args.url} — start the trader first:\n  python3 scripts/lol_trader.py\n{e}")
        return 1
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:500]}")
        return 1

    d = r.json()
    trades = d.get("trades") or []
    dry = d.get("dry_run", True)
    daily = d.get("daily_pnl", 0)
    wr = d.get("win_rate", 0)

    print("=" * 72)
    print(f"  dry_run={'YES (paper)' if dry else 'LIVE'}  daily_pnl=${daily:.2f}  win_rate={wr*100:.0f}%  trades_recorded={len(trades)}")
    print(f"  Strategy: HOLD TO RESOLUTION (no active selling)")
    print("=" * 72)

    tail = trades[-args.n :] if len(trades) > args.n else trades
    if not tail:
        print("No closed trades in state yet (risk.trades empty).")
        return 0

    for i, t in enumerate(tail, 1):
        entry = float(t.get("entry", 0))
        exitp = float(t.get("exit", 0))
        size = float(t.get("size", 0))
        pnl = float(t.get("pnl", 0))
        hold = float(t.get("hold_sec", 0))
        notion = entry * size if entry > 0 and size > 0 else 0
        ret_pct = (pnl / notion * 100) if notion > 0 else 0.0

        flags = []
        if hold < 60 and hold > 0:
            flags.append("quick_resolve")
        if pnl < -notion * 0.2 and notion > 0:
            flags.append("large_loss_vs_notional")
        if entry > 0 and exitp > 0 and abs(exitp - entry) / entry > 0.5:
            flags.append("big_price_move")

        fg = f" [{', '.join(flags)}]" if flags else ""

        print(f"\n{i}. {t.get('match', '?')}")
        print(f"   dir={t.get('direction')}  entry={entry:.4f}  exit={exitp:.4f}  size={size:.2f} sh")
        print(f"   PnL=${pnl:.4f}  (~{ret_pct:+.1f}% on ${notion:.2f} notional)  hold={hold:.1f}s{fg}")
        print(f"   reason: {t.get('reason', '')[:120]}")

    last = tail[-1]
    print("\n" + "-" * 72)
    print(
        "Sanity: entry should match token you wanted; exit ~30s+ after entry if GTC lag OK; "
        "negative PnL on dry_run still means model + prices moved against you on paper."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
