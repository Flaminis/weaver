#!/usr/bin/env python3
"""
LoL LLF vs Polymarket moneyline race.

Finds live LoL matches with LLF on PandaScore, matches them to Polymarket
moneyline markets (>$10k volume), and measures whether LLF game events
arrive before the market reprices.

Usage:
    python3 scripts/lol_llf_vs_market.py [--duration 7200]
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
    import websockets
except ImportError:
    sys.exit("pip install websockets")

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
ESPORTS_TAG = 64
MIN_VOLUME = 10_000
MOVE_THRESHOLD_C = 0.5  # cents

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


# ---------- data ----------

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
    server_ts: str
    game_pos: int
    game_timer: int
    pre_snap: PriceSnap | None
    post_snaps: list[PriceSnap] = field(default_factory=list)
    market_move_ts: float | None = None
    market_move_mid: float | None = None
    edge_ms: float | None = None


@dataclass
class Target:
    ps_match_id: int
    name: str
    llf_url: str
    token: str
    question: str
    volume: float
    last_teams: dict[int, TeamSnap] = field(default_factory=dict)
    last_status: dict[int, str] = field(default_factory=dict)
    initialized: bool = False


@dataclass
class State:
    targets: list[Target] = field(default_factory=list)
    events: list[GameEvent] = field(default_factory=list)
    latest_snap: dict[str, PriceSnap] = field(default_factory=dict)
    burst_until: dict[str, float] = field(default_factory=dict)
    running: bool = True


# ---------- discovery ----------

async def discover(state: State):
    """Find running LoL matches with LLF + Polymarket moneyline >$10k."""
    async with httpx.AsyncClient(timeout=15) as http:
        # running LoL matches with LLF
        r = await http.get(
            f"{PS_BASE}/lol/matches/running",
            headers={"Authorization": f"Bearer {PS_KEY}"},
            params={
                "filter[low_latency_feed]": "true",
                "per_page": 10,
            },
        )
        if r.status_code != 200:
            print(f"  {R}PandaScore: HTTP {r.status_code}{RST}")
            return
        ps_matches = r.json()

        # Polymarket LoL moneylines
        r2 = await http.get(f"{GAMMA}/events", params={
            "tag_id": ESPORTS_TAG, "active": "true",
            "closed": "false", "limit": 100,
        })
        if r2.status_code != 200:
            print(f"  {R}Gamma: HTTP {r2.status_code}{RST}")
            return
        gamma_events = r2.json()

        moneylines: dict[str, dict] = {}  # lowercase key -> market info
        for ev in gamma_events:
            for mkt in ev.get("markets", []):
                q = mkt.get("question", "")
                vol = mkt.get("volumeNum", 0) or 0
                if vol < MIN_VOLUME:
                    continue
                ql = q.lower()
                if "lol:" not in ql:
                    continue
                is_ml = ("(bo3)" in ql or "(bo1)" in ql or "(bo5)" in ql)
                if not is_ml and " vs " in ql:
                    skip = ["game ", "handicap", "kill", "total", "drake",
                            "nashor", "tower", "inhibitor", "blood", "penta",
                            "quadra", "odd", "over", "under"]
                    if not any(k in ql for k in skip):
                        is_ml = True
                if not is_ml:
                    continue

                clob_ids = mkt.get("clobTokenIds", "[]")
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)
                if len(clob_ids) < 2:
                    continue

                moneylines[ql] = {
                    "question": q, "token": clob_ids[0], "volume": vol,
                }

        # match them
        for m in ps_matches:
            mid = m["id"]
            if any(t.ps_match_id == mid for t in state.targets):
                continue

            llf = m.get("low_latency_feed", {})
            if not llf.get("url"):
                continue
            opps = m.get("opponents", [])
            if len(opps) < 2:
                continue

            ta = opps[0]["opponent"]["name"]
            tb = opps[1]["opponent"]["name"]
            ta_kw = _longest_keyword(ta)
            tb_kw = _longest_keyword(tb)

            for key, info in moneylines.items():
                if ta_kw in key and tb_kw in key:
                    # check price is live (mid > 0)
                    try:
                        br = await http.get(f"{CLOB}/price",
                                            params={"token_id": info["token"], "side": "buy"},
                                            timeout=5)
                        ar = await http.get(f"{CLOB}/price",
                                            params={"token_id": info["token"], "side": "sell"},
                                            timeout=5)
                        bid = float(br.json().get("price", 0))
                        ask = float(ar.json().get("price", 0))
                        mid_price = (bid + ask) / 2
                    except Exception:
                        mid_price = 0

                    if mid_price <= 0:
                        continue  # market settled, skip

                    tgt = Target(
                        ps_match_id=m["id"],
                        name=m.get("name", f"{ta} vs {tb}"),
                        llf_url=llf["url"],
                        token=info["token"],
                        question=info["question"],
                        volume=info["volume"],
                    )
                    state.targets.append(tgt)
                    status = m.get("status", "?")
                    league = m.get("league", {}).get("name", "?")
                    print(f"  {G}TARGET{RST}: #{tgt.ps_match_id} {tgt.name} [{league}]")
                    print(f"    Market: {tgt.question}")
                    print(f"    Vol: ${tgt.volume:,.0f} | "
                          f"Mid: {mid_price*100:.1f}c | "
                          f"Spread: {(ask-bid)*100:.1f}c")
                    print(f"    Status: {status}")
                    break


def _longest_keyword(name: str) -> str:
    skip = {"team", "esports", "gaming", "esport", "org"}
    words = [w for w in name.lower().split() if w not in skip]
    return max(words, key=len) if words else name.lower()


# ---------- LLF listener ----------

async def llf_listen(state: State, tgt: Target):
    url = f"{tgt.llf_url}?token={PS_KEY}"
    tag = f"[{tgt.name}]"

    while state.running:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                print(f"  {M}{tag}{RST} LLF connected")
                while state.running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=120)
                    except asyncio.TimeoutError:
                        print(f"  {M}{tag}{RST} silent 120s, reconnecting")
                        break

                    wall = time.time()
                    msg = json.loads(raw)
                    mtype = msg.get("type", "")

                    if mtype == "hello":
                        p = msg.get("payload", {})
                        print(f"  {M}{tag}{RST} hello status={p.get('status')}")
                        if p.get("status") == "closing":
                            return
                        continue

                    if mtype != "scoreboard":
                        continue

                    sb = msg.get("scoreboard", {})
                    server_ts = msg.get("at", "")

                    for g in sb.get("games") or []:
                        gid = g.get("id", 0)
                        gpos = g.get("position", 0)
                        gstatus = g.get("status", "?")
                        timer_obj = g.get("timer") or {}
                        timer_s = timer_obj.get("timer", 0) or 0
                        teams_raw = g.get("teams") or []
                        if not teams_raw:
                            continue

                        new_teams = {}
                        for t in teams_raw:
                            tid = t.get("id") or 0
                            if not tid:
                                continue
                            ts_obj = TeamSnap(
                                id=tid,
                                side=t.get("side") or "?",
                                kills=t.get("kills") or 0,
                                towers=t.get("towers") or 0,
                                drakes=t.get("drakes") or 0,
                                nashors=t.get("nashors") or 0,
                                inhibitors=t.get("inhibitors") or 0,
                            )
                            new_teams[ts_obj.id] = ts_obj
                        if not new_teams:
                            continue

                        # first message for this game → init
                        if gid not in tgt.last_teams or not tgt.initialized:
                            tgt.last_teams[gid] = new_teams
                            tgt.last_status[gid] = gstatus
                            if not tgt.initialized:
                                tgt.initialized = True
                            _print_state(tag, gpos, gstatus, timer_s, new_teams, "init")
                            continue

                        # diff
                        old_teams = tgt.last_teams.get(gid, {})
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
                                    changes.append((
                                        fld,
                                        f"{nt.side} {fld}: {ov}→{nv} (+{delta})",
                                    ))

                        old_status = tgt.last_status.get(gid)
                        if old_status and old_status != gstatus:
                            changes.append(("status", f"game {gpos}: {old_status}→{gstatus}"))

                        tgt.last_teams[gid] = new_teams
                        tgt.last_status[gid] = gstatus

                        if not changes:
                            continue

                        # event detected
                        pre = state.latest_snap.get(tgt.token)
                        _print_state(tag, gpos, gstatus, timer_s, new_teams, None)

                        for etype, desc in changes:
                            ev = GameEvent(
                                event_type=etype, description=desc,
                                wall_ts=wall, server_ts=server_ts,
                                game_pos=gpos, game_timer=timer_s,
                                pre_snap=pre,
                            )
                            state.events.append(ev)

                            color = {"kills": C, "towers": G, "drakes": M,
                                     "nashors": Y, "inhibitors": R}.get(etype, RST)
                            pre_str = f" @{pre.mid*100:.1f}c" if pre and pre.mid > 0 else ""
                            m, s = divmod(timer_s, 60)
                            print(f"  {color}{BOLD}[{etype.upper():10}]{RST} "
                                  f"{ts()} G{gpos} [{m}:{s:02d}] {desc}{pre_str}")

                            if pre and pre.mid > 0:
                                state.burst_until[tgt.token] = time.time() + 20
                                asyncio.create_task(
                                    _track_reaction(state, tgt, ev))

        except websockets.exceptions.ConnectionClosed as e:
            code = getattr(e, "code", 0) or 0
            if code == 4004 or "unavailable" in str(e).lower():
                print(f"  {M}{tag}{RST} LLF not open yet — retry 30s")
                await asyncio.sleep(30)
            elif code == 1000 or "finished" in str(e).lower():
                print(f"  {M}{tag}{RST} match ended")
                return
            else:
                print(f"  {M}{tag}{RST} closed ({code}) — retry 5s")
                await asyncio.sleep(5)
        except OSError as e:
            print(f"  {M}{tag}{RST} network: {e} — retry 15s")
            await asyncio.sleep(15)
        except Exception as e:
            print(f"  {M}{tag}{RST} {type(e).__name__}: {e} — retry 10s")
            await asyncio.sleep(10)


def _print_state(tag, gpos, gstatus, timer_s, teams: dict[int, TeamSnap], label):
    m, s = divmod(timer_s, 60)
    parts = []
    for t in teams.values():
        parts.append(f"{t.side[:1].upper()}: K{t.kills} T{t.towers} "
                     f"D{t.drakes} N{t.nashors} I{t.inhibitors}")
    suffix = f" [{label}]" if label else ""
    print(f"  {DIM}  G{gpos} [{m}:{s:02d}] {gstatus} | "
          f"{' | '.join(parts)}{suffix}{RST}")


# ---------- price tracking ----------

async def price_poller(state: State):
    async with httpx.AsyncClient(timeout=5) as http:
        while state.running:
            for tgt in state.targets:
                burst = state.burst_until.get(tgt.token, 0) > time.time()
                try:
                    br, ar = await asyncio.gather(
                        http.get(f"{CLOB}/price",
                                 params={"token_id": tgt.token, "side": "buy"}),
                        http.get(f"{CLOB}/price",
                                 params={"token_id": tgt.token, "side": "sell"}),
                    )
                    w = time.time()
                    bid = float(br.json().get("price", 0)) if br.status_code == 200 else 0
                    ask = float(ar.json().get("price", 0)) if ar.status_code == 200 else 0
                    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
                    state.latest_snap[tgt.token] = PriceSnap(
                        wall_ts=w, mid=mid, bid=bid, ask=ask,
                        spread=round(ask - bid, 4) if ask > bid else 0,
                    )
                except Exception:
                    pass
                await asyncio.sleep(0.15 if burst else 1.5)

            if not state.targets:
                await asyncio.sleep(5)


async def _track_reaction(state: State, tgt: Target, ev: GameEvent):
    pre_mid = ev.pre_snap.mid if ev.pre_snap else 0
    if pre_mid <= 0:
        return

    start = time.time()
    detected = False

    async with httpx.AsyncClient(timeout=3) as http:
        while time.time() - start < 20 and state.running:
            try:
                br, ar = await asyncio.gather(
                    http.get(f"{CLOB}/price",
                             params={"token_id": tgt.token, "side": "buy"}),
                    http.get(f"{CLOB}/price",
                             params={"token_id": tgt.token, "side": "sell"}),
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
                        print(f"  {G}{BOLD}>>> LLF {fmt_ms(elapsed)} "
                              f"AHEAD of market <<<{RST}\n")
                    else:
                        print(f"  {R}{BOLD}>>> Market {fmt_ms(abs(elapsed))} "
                              f"BEFORE LLF <<<{RST}\n")

            except Exception:
                pass
            await asyncio.sleep(0.15)

    if not detected:
        print(f"  {DIM}({ev.event_type}) no move ≥{MOVE_THRESHOLD_C}c "
              f"in 20s (stayed ~{pre_mid*100:.1f}c){RST}")


# ---------- periodic status ----------

async def status_loop(state: State, interval=60):
    while state.running:
        await asyncio.sleep(interval)
        n = len(state.events)
        n_edge = sum(1 for e in state.events if e.edge_ms is not None)
        print(f"\n  {BOLD}[STATUS {ts()}]{RST} events={n} priced={n_edge}")
        for tgt in state.targets:
            snap = state.latest_snap.get(tgt.token)
            if snap:
                print(f"    {tgt.name}: {snap.mid*100:.1f}c "
                      f"(spread {snap.spread*100:.1f}c) vol=${tgt.volume:,.0f}")

        edges = [e.edge_ms for e in state.events if e.edge_ms is not None]
        if edges:
            avg = sum(edges) / len(edges)
            ahead = sum(1 for e in edges if e > 0)
            print(f"    avg edge: {fmt_ms(avg)} | "
                  f"LLF ahead: {ahead}/{len(edges)}")
        print()


# ---------- final report ----------

def final_report(state: State):
    print(f"\n{'='*60}")
    print(f"  {BOLD}LoL LLF vs MARKET — REPORT{RST}")
    print(f"{'='*60}")

    if not state.events:
        print(f"  No events captured.\n{'='*60}")
        return

    by_type: dict[str, list[GameEvent]] = {}
    for ev in state.events:
        by_type.setdefault(ev.event_type, []).append(ev)

    print(f"\n  {BOLD}Events:{RST}")
    for etype, evs in sorted(by_type.items()):
        n_p = sum(1 for e in evs if e.edge_ms is not None)
        print(f"    {etype:12}: {len(evs)} total, {n_p} w/ price move")

    edges = [e.edge_ms for e in state.events if e.edge_ms is not None]
    if edges:
        avg = sum(edges) / len(edges)
        ahead = sum(1 for e in edges if e > 0)
        print(f"\n  {BOLD}Edge:{RST}")
        print(f"    avg={fmt_ms(avg)} min={fmt_ms(min(edges))} max={fmt_ms(max(edges))}")
        print(f"    LLF ahead: {ahead}/{len(edges)} ({ahead/len(edges)*100:.0f}%)")
    else:
        print(f"\n  {Y}No price moves detected to compare.{RST}")

    print(f"\n  {BOLD}Log:{RST}")
    for ev in state.events[-50:]:
        m, s = divmod(ev.game_timer, 60)
        edge_str = ""
        if ev.edge_ms is not None:
            sign = G if ev.edge_ms > 0 else R
            label = "LLF+" if ev.edge_ms > 0 else "MKT+"
            edge_str = f" {sign}{label}{fmt_ms(abs(ev.edge_ms))}{RST}"
        elif ev.pre_snap and ev.pre_snap.mid > 0:
            edge_str = f" {DIM}no move{RST}"
        pre_c = f" @{ev.pre_snap.mid*100:.1f}c" if ev.pre_snap and ev.pre_snap.mid > 0 else ""
        print(f"    [{ev.event_type:10}] G{ev.game_pos} [{m}:{s:02d}] "
              f"{ev.description}{pre_c}{edge_str}")

    print(f"{'='*60}")

    out = Path(__file__).parent / "lol_race_report.json"
    report = [{
        "event_type": e.event_type, "description": e.description,
        "game_pos": e.game_pos, "game_timer": e.game_timer,
        "llf_wall_ts": e.wall_ts, "server_ts": e.server_ts,
        "pre_mid": e.pre_snap.mid if e.pre_snap else None,
        "edge_ms": e.edge_ms, "market_move_mid": e.market_move_mid,
        "post_snaps": [{"ts": s.wall_ts, "mid": s.mid} for s in e.post_snaps],
    } for e in state.events]
    try:
        out.write_text(json.dumps(report, indent=2))
        print(f"  Saved: {out}")
    except Exception as exc:
        print(f"  {R}Write error: {exc}{RST}")


# ---------- main ----------

async def main(duration: int = 7200):
    print(f"\n{'='*60}")
    print(f"  {BOLD}LoL LLF vs POLYMARKET — MONEYLINE RACE{RST}")
    print(f"  Duration: {duration}s | Min volume: ${MIN_VOLUME:,}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}\n")

    if not PS_KEY:
        sys.exit("PANDASCORE_API_KEY not set")

    state = State()

    print(f"  {BOLD}Discovering live targets...{RST}")
    await discover(state)

    if not state.targets:
        print(f"\n  {Y}No live LoL matches with LLF + active moneyline >$10k.{RST}")
        print(f"  {Y}Run again when matches are live.{RST}")
        return

    print(f"\n  {BOLD}Tracking {len(state.targets)} target(s). Listening...{RST}\n")

    tasks = []
    for tgt in state.targets:
        tasks.append(asyncio.create_task(llf_listen(state, tgt)))
    tasks.append(asyncio.create_task(price_poller(state)))
    tasks.append(asyncio.create_task(status_loop(state)))

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
    dur = 7200
    if "--duration" in sys.argv:
        idx = sys.argv.index("--duration")
        if idx + 1 < len(sys.argv):
            dur = int(sys.argv[idx + 1])
    elif len(sys.argv) > 1 and sys.argv[1].isdigit():
        dur = int(sys.argv[1])

    try:
        asyncio.run(main(dur))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
