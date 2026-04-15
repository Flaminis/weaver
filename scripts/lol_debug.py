#!/usr/bin/env python3
"""
Debug dump tool for Oracle-LoL trader.
Hit the /api/state endpoint and dump everything in a human-readable format.
Save to logs/debug_YYYYMMDD_HHMMSS.txt for context in future sessions.

Usage:
    python3 scripts/lol_debug.py                    # print to stdout
    python3 scripts/lol_debug.py --save              # save to file + stdout
    python3 scripts/lol_debug.py --url http://host:port  # custom API
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import httpx


def dump(url: str = "http://localhost:8422") -> str:
    r = httpx.get(f"{url}/api/state", timeout=5)
    if r.status_code != 200:
        return f"ERROR: API returned {r.status_code}\n{r.text}"

    d = r.json()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = []
    lines.append(f"{'='*70}")
    lines.append(f"  ORACLE-LoL DEBUG DUMP — {now}")
    lines.append(f"{'='*70}")
    lines.append("")

    # ── Global state ────────────────────────────────────────────────
    lines.append("[GLOBAL]")
    lines.append(f"  dry_run:       {d.get('dry_run')}")
    lines.append(f"  uptime:        {d.get('uptime_sec', 0):.0f}s")
    lines.append(f"  capital:       ${d.get('capital', 0):.2f} (bankroll ${d.get('bankroll', 0):.2f})")
    lines.append(f"  daily_pnl:     ${d.get('daily_pnl', 0):.2f}")
    lines.append(f"  exposure:      ${d.get('exposure', 0):.2f}")
    lines.append(f"  trades:        {d.get('total_trades', 0)} (wr={d.get('win_rate', 0)*100:.0f}%)")
    lines.append(f"  circuit:       {d.get('circuit_active', False)}")
    lines.append(f"  consec_losses: {d.get('consecutive_losses', 0)}")
    lines.append("")

    # ── Matches ─────────────────────────────────────────────────────
    matches = d.get("matches", {})
    lines.append(f"[MATCHES] ({len(matches)} total)")
    for mid, m in sorted(matches.items(), key=lambda x: -int(x[1].get("active", True))):
        active_flag = "ACTIVE" if m.get("active", True) else "FINISHED"
        has_mkt = "HAS_MARKET" if m.get("has_market") else "NO_MARKET"
        has_book = "BOOK_OK" if m.get("has_book") else "NO_BOOK"
        mid_c = f"{m.get('mid', 0)*100:.1f}c" if m.get('mid', 0) > 0 else "—"
        spread_c = f"{m.get('spread', 1)*100:.1f}c" if m.get('spread', 1) < 1 else "—"

        lines.append(f"  #{mid} {m.get('name', '?')}")
        lines.append(f"    status:     {active_flag} | {has_mkt} | {has_book}")
        lines.append(f"    teams:      {m.get('team_a', '?')} (id={m.get('team_a_id')}) vs {m.get('team_b', '?')} (id={m.get('team_b_id')})")
        lines.append(f"    league:     {m.get('league', '?')}")
        lines.append(f"    market:     {m.get('market_question', '—')}")
        lines.append(f"    mkt_type:   {m.get('active_market_type', '—')} (game #{m.get('current_game_num', 0)}, {m.get('total_markets', 0)} total)")
        lines.append(f"    price:      mid={mid_c} spread={spread_c}")
        lines.append(f"    token_a:    {m.get('token_a', '—')[:24]}...")
        lines.append(f"    token_b:    {m.get('token_b', '—')[:24]}...")
        lines.append(f"    book_bids:  {len(m.get('book_bids', []))} levels")
        lines.append(f"    book_asks:  {len(m.get('book_asks', []))} levels")
        lines.append(f"    history:    {len(m.get('price_history', []))} pts")
        lines.append(f"    events:     {m.get('event_count', 0)}")

        games = m.get("games", [])
        if games:
            for g in games:
                teams = g.get("teams", [])
                t_strs = []
                for t in teams:
                    side = (t.get("side") or "?")[:3]
                    t_strs.append(f"{side}: K{t.get('kills',0)} T{t.get('towers',0)} D{t.get('drakes',0)} B{t.get('nashors',0)} I{t.get('inhibitors',0)}")
                lines.append(f"    game_{g.get('position', '?')}: {g.get('status', '?')} — {' | '.join(t_strs)}")
        lines.append("")

    # ── Positions ───────────────────────────────────────────────────
    positions = d.get("positions", [])
    open_pos = [p for p in positions if not p.get("closed")]
    closed_pos = [p for p in positions if p.get("closed")]

    lines.append(f"[POSITIONS] ({len(open_pos)} open, {len(closed_pos)} closed)")
    for p in positions:
        status = "OPEN" if not p.get("closed") else "CLOSED"
        pnl = p.get("unrealized_pnl", 0) if not p.get("closed") else p.get("exit_pnl", 0)
        lines.append(f"  [{status}] {p.get('match_name', '?')}")
        lines.append(f"    dir={p.get('direction')} entry={p.get('entry_price',0)*100:.1f}c size={p.get('size',0):.1f}")
        lines.append(f"    current={p.get('current_price',0)*100:.1f}c pnl=${pnl:.4f} age={p.get('age_sec',0):.0f}s")
        lines.append(f"    reason: {p.get('signal_reason', '?')}")
        if p.get("sell_order_id"):
            lines.append(f"    sell_order: {p.get('sell_order_id', '')[:24]}...")
    lines.append("")

    # ── Trades ──────────────────────────────────────────────────────
    trades = d.get("trades", [])
    lines.append(f"[TRADES] ({len(trades)} total)")
    for t in trades[-20:]:
        ts = datetime.fromtimestamp(t.get("ts", 0)).strftime("%H:%M:%S")
        pnl_sign = "+" if t.get("pnl", 0) >= 0 else ""
        lines.append(f"  {ts} {t.get('match', '?')} {t.get('direction', '?')} "
                     f"entry={t.get('entry',0)*100:.1f}c exit={t.get('exit',0)*100:.1f}c "
                     f"pnl={pnl_sign}${t.get('pnl',0):.4f} hold={t.get('hold_sec',0):.0f}s "
                     f"— {t.get('reason', '?')}")
    lines.append("")

    # ── Recent events ───────────────────────────────────────────────
    events = d.get("events", [])
    lines.append(f"[EVENTS] ({len(events)} total, last 30)")
    for ev in events[-30:]:
        lines.append(f"  {ev.get('time', '?')} [{ev.get('action', '?'):15}] "
                     f"{ev.get('etype', '?'):6} G{ev.get('game', '?')} [{ev.get('clock', '?')}] "
                     f"{ev.get('team', '?')} {ev.get('desc', '')} "
                     f"mid={ev.get('mid',0)*100:.1f}c spread={ev.get('spread',0)*100:.1f}c"
                     f"{(' → ' + ev.get('signal_reason', '')) if ev.get('signal_reason') else ''}")
    lines.append("")
    lines.append(f"{'='*70}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Oracle-LoL Debug Dump")
    p.add_argument("--url", default="http://localhost:8422")
    p.add_argument("--save", action="store_true", help="Save to logs/debug_*.txt")
    args = p.parse_args()

    try:
        text = dump(args.url)
    except Exception as e:
        print(f"ERROR: Could not connect to {args.url} — {e}")
        sys.exit(1)

    print(text)

    if args.save:
        log_dir = Path(__file__).parent / "logs"
        log_dir.mkdir(exist_ok=True)
        path = log_dir / f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(path, "w") as f:
            f.write(text)
        print(f"\nSaved to {path}")
