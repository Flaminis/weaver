#!/usr/bin/env python3
"""
LoL REST poll vs Polymarket price race.

For matches WITHOUT LLF — polls PandaScore REST at ~3Hz,
detects game state changes, measures edge against Polymarket moneyline.

Usage:
    python3 scripts/lol_rest_vs_market.py --match 1445628 --slug lol-tl2-fly-2026-04-14 [--duration 3600]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

sys.stdout.reconfigure(line_buffering=True)

PS_KEY = os.environ.get("PANDASCORE_API_KEY", "")
PS_BASE = "https://api.pandascore.co"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
POLL_HZ = 3.0
MOVE_THRESHOLD_C = 0.5

BOLD = "\033[1m"
RST = "\033[0m"
G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
C = "\033[96m"
M = "\033[95m"
DIM = "\033[2m"


def ts() -> str:
    t = time.time()
    return time.strftime("%H:%M:%S.", time.localtime(t)) + f"{t % 1:.3f}"[2:]

def fmt_ms(ms: float) -> str:
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


@dataclass
class PriceSnap:
    wall_ts: float
    mid: float
    bid: float
    ask: float
    spread: float

@dataclass
class TeamSnap:
    id: int
    side: str
    kills: int
    towers: int
    drakes: int
    nashors: int
    inhibitors: int

@dataclass
class GameEvent:
    event_type: str
    description: str
    wall_ts: float
    game_pos: int
    game_timer: int
    pre_snap: PriceSnap | None
    post_snaps: list[PriceSnap] = field(default_factory=list)
    market_move_ts: float | None = None
    market_move_mid: float | None = None
    edge_ms: float | None = None

@dataclass
class State:
    match_id: int
    token: str
    question: str
    volume: float
    events: list[GameEvent] = field(default_factory=list)
    latest_snap: PriceSnap | None = None
    burst_until: float = 0
    last_teams: dict[int, dict[int, TeamSnap]] = field(default_factory=dict)
    last_status: dict[int, str] = field(default_factory=dict)
    initialized: set = field(default_factory=set)
    running: bool = True


def _parse_team(t: dict) -> TeamSnap | None:
    tid = t.get("id") or 0
    if not tid:
        return None
    return TeamSnap(
        id=tid,
        side=t.get("side") or "?",
        kills=t.get("kills") or 0,
        towers=t.get("towers") or 0,
        drakes=t.get("drakes") or 0,
        nashors=t.get("nashors") or 0,
        inhibitors=t.get("inhibitors") or 0,
    )


async def rest_poller(state: State):
    """Poll PandaScore REST for game state changes."""
    async with httpx.AsyncClient(timeout=5) as http:
        while state.running:
            try:
                r = await http.get(
                    f"{PS_BASE}/matches/{state.match_id}",
                    headers={"Authorization": f"Bearer {PS_KEY}"},
                )
                if r.status_code != 200:
                    await asyncio.sleep(1)
                    continue

                wall = time.time()
                m = r.json()

                for g in m.get("games", []):
                    gid = g.get("id", 0)
                    gpos = g.get("position", 0)
                    gstatus = g.get("status", "?")

                    teams_raw = g.get("teams") or []
                    if not teams_raw:
                        continue

                    new_teams = {}
                    for t in teams_raw:
                        ts_obj = _parse_team(t)
                        if ts_obj:
                            new_teams[ts_obj.id] = ts_obj

                    if not new_teams:
                        continue

                    if gid not in state.initialized:
                        state.last_teams[gid] = new_teams
                        state.last_status[gid] = gstatus
                        state.initialized.add(gid)
                        _print_state(gpos, gstatus, 0, new_teams, "init")
                        continue

                    old_teams = state.last_teams.get(gid, {})
                    changes = []

                    for tid, nt in new_teams.items():
                        ot = old_teams.get(tid)
                        if ot is None:
                            continue
                        for fld in ("kills", "towers", "drakes", "nashors", "inhibitors"):
                            ov = getattr(ot, fld)
                            nv = getattr(nt, fld)
                            if nv != ov:
                                delta = nv - ov
                                changes.append((fld, f"{nt.side} {fld}: {ov}→{nv} (+{delta})"))

                    old_st = state.last_status.get(gid)
                    if old_st and old_st != gstatus:
                        changes.append(("status", f"game {gpos}: {old_st}→{gstatus}"))

                    state.last_teams[gid] = new_teams
                    state.last_status[gid] = gstatus

                    if not changes:
                        continue

                    pre = state.latest_snap
                    _print_state(gpos, gstatus, 0, new_teams, None)

                    for etype, desc in changes:
                        ev = GameEvent(
                            event_type=etype, description=desc,
                            wall_ts=wall, game_pos=gpos, game_timer=0,
                            pre_snap=pre,
                        )
                        state.events.append(ev)

                        color = {"kills": C, "towers": G, "drakes": M,
                                 "nashors": Y, "inhibitors": R, "status": G}.get(etype, RST)
                        pre_str = f" @{pre.mid*100:.1f}c" if pre and pre.mid > 0 else ""
                        print(f"  {color}{BOLD}[{etype.upper():10}]{RST} "
                              f"{ts()} G{gpos} {desc}{pre_str}")

                        if pre and pre.mid > 0:
                            state.burst_until = time.time() + 20
                            asyncio.create_task(_track_reaction(state, ev))

            except Exception as e:
                print(f"  {R}REST poll error: {e}{RST}")

            await asyncio.sleep(1.0 / POLL_HZ)


async def price_poller(state: State):
    async with httpx.AsyncClient(timeout=5) as http:
        while state.running:
            burst = state.burst_until > time.time()
            try:
                br, ar = await asyncio.gather(
                    http.get(f"{CLOB}/price", params={"token_id": state.token, "side": "buy"}),
                    http.get(f"{CLOB}/price", params={"token_id": state.token, "side": "sell"}),
                )
                w = time.time()
                bid = float(br.json().get("price", 0)) if br.status_code == 200 else 0
                ask = float(ar.json().get("price", 0)) if ar.status_code == 200 else 0
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
                state.latest_snap = PriceSnap(
                    wall_ts=w, mid=mid, bid=bid, ask=ask,
                    spread=round(ask - bid, 4) if ask > bid else 0,
                )
            except Exception:
                pass
            await asyncio.sleep(0.15 if burst else 1.5)


async def _track_reaction(state: State, ev: GameEvent):
    pre_mid = ev.pre_snap.mid if ev.pre_snap else 0
    if pre_mid <= 0:
        return

    start = time.time()
    detected = False

    async with httpx.AsyncClient(timeout=3) as http:
        while time.time() - start < 20 and state.running:
            try:
                br, ar = await asyncio.gather(
                    http.get(f"{CLOB}/price", params={"token_id": state.token, "side": "buy"}),
                    http.get(f"{CLOB}/price", params={"token_id": state.token, "side": "sell"}),
                )
                w = time.time()
                bid = float(br.json().get("price", 0)) if br.status_code == 200 else 0
                ask = float(ar.json().get("price", 0)) if ar.status_code == 200 else 0
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0

                snap = PriceSnap(wall_ts=w, mid=mid, bid=bid, ask=ask,
                                 spread=round(ask - bid, 4) if ask > bid else 0)
                ev.post_snaps.append(snap)

                delta_c = abs(mid - pre_mid) * 100
                elapsed = (w - ev.wall_ts) * 1000

                if delta_c >= MOVE_THRESHOLD_C and not detected:
                    detected = True
                    ev.market_move_ts = w
                    ev.market_move_mid = mid
                    ev.edge_ms = elapsed

                    direction = "UP" if mid > pre_mid else "DOWN"
                    print(f"  {C}{BOLD}MARKET MOVED{RST}: "
                          f"{pre_mid*100:.1f}c → {mid*100:.1f}c "
                          f"({direction} {delta_c:.1f}c)")
                    if elapsed > 0:
                        print(f"  {G}{BOLD}>>> REST {fmt_ms(elapsed)} "
                              f"AHEAD of market <<<{RST}\n")
                    else:
                        print(f"  {R}{BOLD}>>> Market {fmt_ms(abs(elapsed))} "
                              f"BEFORE REST <<<{RST}\n")
            except Exception:
                pass
            await asyncio.sleep(0.15)

    if not detected:
        print(f"  {DIM}({ev.event_type}) no move ≥{MOVE_THRESHOLD_C}c "
              f"in 20s (stayed ~{pre_mid*100:.1f}c){RST}")


def _print_state(gpos, gstatus, timer_s, teams: dict[int, TeamSnap], label):
    parts = []
    for t in teams.values():
        parts.append(f"{t.side[:1].upper()}: K{t.kills} T{t.towers} "
                     f"D{t.drakes} N{t.nashors} I{t.inhibitors}")
    suffix = f" [{label}]" if label else ""
    print(f"  {DIM}  G{gpos} {gstatus} | {' | '.join(parts)}{suffix}{RST}")


async def status_loop(state: State, interval=60):
    while state.running:
        await asyncio.sleep(interval)
        n = len(state.events)
        n_edge = sum(1 for e in state.events if e.edge_ms is not None)
        snap = state.latest_snap
        print(f"\n  {BOLD}[STATUS {ts()}]{RST} events={n} priced={n_edge}")
        if snap:
            print(f"    mid={snap.mid*100:.1f}c spread={snap.spread*100:.1f}c")
        edges = [e.edge_ms for e in state.events if e.edge_ms is not None]
        if edges:
            avg = sum(edges) / len(edges)
            ahead = sum(1 for e in edges if e > 0)
            print(f"    avg edge: {fmt_ms(avg)} | ahead: {ahead}/{len(edges)}")
        print()


def final_report(state: State):
    print(f"\n{'='*60}")
    print(f"  {BOLD}LoL REST vs MARKET — REPORT{RST}")
    print(f"  {state.question}")
    print(f"{'='*60}")

    if not state.events:
        print(f"  No events captured.\n{'='*60}")
        return

    edges = [e.edge_ms for e in state.events if e.edge_ms is not None]
    if edges:
        avg = sum(edges) / len(edges)
        ahead = sum(1 for e in edges if e > 0)
        print(f"\n  {BOLD}Edge:{RST}")
        print(f"    avg={fmt_ms(avg)} min={fmt_ms(min(edges))} max={fmt_ms(max(edges))}")
        print(f"    REST ahead: {ahead}/{len(edges)} ({ahead/len(edges)*100:.0f}%)")

    print(f"\n  {BOLD}Log:{RST}")
    for ev in state.events[-50:]:
        edge_str = ""
        if ev.edge_ms is not None:
            sign = G if ev.edge_ms > 0 else R
            label = "REST+" if ev.edge_ms > 0 else "MKT+"
            edge_str = f" {sign}{label}{fmt_ms(abs(ev.edge_ms))}{RST}"
        elif ev.pre_snap and ev.pre_snap.mid > 0:
            edge_str = f" {DIM}no move{RST}"
        pre_c = f" @{ev.pre_snap.mid*100:.1f}c" if ev.pre_snap and ev.pre_snap.mid > 0 else ""
        post_c = ""
        if ev.market_move_mid:
            post_c = f" →{ev.market_move_mid*100:.1f}c"
        print(f"    [{ev.event_type:10}] G{ev.game_pos} "
              f"{ev.description}{pre_c}{post_c}{edge_str}")

    print(f"{'='*60}")

    out = Path(__file__).parent / "lol_rest_race_report.json"
    report = [{
        "event_type": e.event_type, "description": e.description,
        "game_pos": e.game_pos,
        "llf_wall_ts": e.wall_ts,
        "pre_mid": e.pre_snap.mid if e.pre_snap else None,
        "edge_ms": e.edge_ms,
        "market_move_mid": e.market_move_mid,
        "post_snaps": [{"ts": s.wall_ts, "mid": s.mid} for s in e.post_snaps],
    } for e in state.events]
    try:
        out.write_text(json.dumps(report, indent=2))
        print(f"  Saved: {out}")
    except Exception as exc:
        print(f"  {R}Write error: {exc}{RST}")


async def main(match_id: int, slug: str, duration: int = 3600):
    print(f"\n{'='*60}")
    print(f"  {BOLD}LoL REST vs POLYMARKET — MONEYLINE RACE{RST}")
    print(f"  Match: {match_id} | Poll: {POLL_HZ}Hz")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}\n")

    if not PS_KEY:
        sys.exit("PANDASCORE_API_KEY not set")

    # Find moneyline from slug
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(f"{GAMMA}/events", params={"slug": slug, "limit": 5})
        if r.status_code != 200 or not r.json():
            sys.exit(f"Event not found for slug={slug}")
        ev = r.json()[0]
        print(f"  Event: {ev.get('title')}")

        token = question = ""
        volume = 0
        for mkt in ev.get("markets", []):
            q = mkt.get("question", "")
            vol = mkt.get("volumeNum", 0) or 0
            ql = q.lower()
            if not ("(bo3)" in ql or "(bo1)" in ql or "(bo5)" in ql):
                continue
            if vol < 1000:
                continue
            clob_ids = mkt.get("clobTokenIds", "[]")
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)
            if len(clob_ids) < 2:
                continue

            br = await http.get(f"{CLOB}/price", params={"token_id": clob_ids[0], "side": "buy"}, timeout=5)
            ar = await http.get(f"{CLOB}/price", params={"token_id": clob_ids[0], "side": "sell"}, timeout=5)
            bid = float(br.json().get("price", 0))
            ask = float(ar.json().get("price", 0))
            mid_price = (bid + ask) / 2
            if mid_price <= 0:
                continue
            token = clob_ids[0]
            question = q
            volume = vol
            print(f"  Market: {q}")
            print(f"  Vol: ${vol:,.0f} | Mid: {mid_price*100:.1f}c | Spread: {(ask-bid)*100:.1f}c")
            break

        if not token:
            sys.exit("No active moneyline found")

    state = State(match_id=match_id, token=token, question=question, volume=volume)

    print(f"\n  {BOLD}Polling REST at {POLL_HZ}Hz. Listening...{RST}\n")

    tasks = [
        asyncio.create_task(rest_poller(state)),
        asyncio.create_task(price_poller(state)),
        asyncio.create_task(status_loop(state)),
    ]

    try:
        await asyncio.sleep(duration)
    except asyncio.CancelledError:
        pass
    finally:
        state.running = False
        for t in tasks:
            t.cancel()
        await asyncio.sleep(1)

    final_report(state)


if __name__ == "__main__":
    match_id = 0
    slug = ""
    duration = 3600

    if "--match" in sys.argv:
        idx = sys.argv.index("--match")
        if idx + 1 < len(sys.argv):
            match_id = int(sys.argv[idx + 1])
    if "--slug" in sys.argv:
        idx = sys.argv.index("--slug")
        if idx + 1 < len(sys.argv):
            slug = sys.argv[idx + 1]
    if "--duration" in sys.argv:
        idx = sys.argv.index("--duration")
        if idx + 1 < len(sys.argv):
            duration = int(sys.argv[idx + 1])

    if not match_id or not slug:
        sys.exit("Usage: --match <pandascore_id> --slug <gamma_slug>")

    try:
        asyncio.run(main(match_id, slug, duration))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
