#!/usr/bin/env python3
"""
LoL LLF Live Dashboard
Real-time console dashboard for PandaScore Low Latency Feed.

Usage:
    python3 scripts/lol_dashboard.py [MATCH_ID]
    python3 scripts/lol_dashboard.py 1407095
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

try:
    import websockets
except ImportError:
    print("pip install websockets")
    sys.exit(1)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

PS_KEY = os.environ.get("PANDASCORE_API_KEY", "")
PS_BASE = "https://api.pandascore.co"

# ── ANSI ────────────────────────────────────────────────────────────────
RST  = "\033[0m"
B    = "\033[1m"
DIM  = "\033[2m"
FR   = "\033[91m"
FG   = "\033[92m"
FY   = "\033[93m"
FB   = "\033[94m"
FM   = "\033[95m"
FC   = "\033[96m"
FW   = "\033[97m"

CLR  = "\033[2J\033[H"
HIDE = "\033[?25l"
SHOW = "\033[?25h"

ANSI_RE = re.compile(r'\033\[[0-9;]*m')

def vlen(s: str) -> int:
    return len(ANSI_RE.sub('', s))

def vpad(s: str, w: int, align: str = 'l') -> str:
    diff = max(0, w - vlen(s))
    if align == 'c':
        lp = diff // 2
        return ' ' * lp + s + ' ' * (diff - lp)
    if align == 'r':
        return ' ' * diff + s
    return s + ' ' * diff


# ── Box (total width = 72) ──────────────────────────────────────────────
#
# Full row:  ║ content(68) ║   = 1+1+68+1+1 = 72
# Split row: ║ left(32) ║ right(33) ║  = 1+1+32+1+1+1+33+1+1 = 72
# Heavy hr:  ╠═(70)═╣               = 1+70+1 = 72
# Split hr:  ╠═(34)═╬═(35)═╣        = 1+34+1+35+1 = 72

W  = 72
CW = 68
LW = 32
RW = 33

X = FC  # box color


def hr_top():    return f"{X}╔{'═'*(W-2)}╗{RST}"
def hr_bot():    return f"{X}╚{'═'*(W-2)}╝{RST}"
def hr_h():      return f"{X}╠{'═'*(W-2)}╣{RST}"
def hr_l():      return f"{X}╠{'─'*(W-2)}╣{RST}"
def hr_hs():     return f"{X}╠{'═'*34}╬{'═'*35}╣{RST}"
def hr_ls():     return f"{X}╠{'─'*34}╬{'─'*35}╣{RST}"

def row(t, a='l'):   return f"{X}║{RST} {vpad(t,CW,a)} {X}║{RST}"
def row2(l, r):      return f"{X}║{RST} {vpad(l,LW)} {X}║{RST} {vpad(r,RW)} {X}║{RST}"


# ── Dashboard State ─────────────────────────────────────────────────────

class Dashboard:
    def __init__(self, match_id: int):
        self.match_id = match_id
        self.team_names: dict[int, str] = {}
        self.team_acrs: dict[int, str] = {}
        self.league = ""
        self.match_name = ""
        self.games: list[dict] = []
        self.series_score: dict[int, int] = {}
        self.events: deque = deque(maxlen=15)
        self.connected = False
        self.msg_count = 0
        self.last_at = ""
        self._prev: dict[int, dict[int, dict]] = {}
        self._prev_status: dict[int, str] = {}
        self._needs_score_refresh = False

    def acr(self, tid: int) -> str:
        return self.team_acrs.get(tid, self.team_names.get(tid, f"T{tid}"))

    @staticmethod
    def clock(timer_obj: dict | None) -> str:
        if not timer_obj:
            return "--:--"
        base = timer_obj.get("timer", 0) or 0
        paused = timer_obj.get("paused", True)
        issued = timer_obj.get("issued_at", "")
        if not paused and issued:
            try:
                dt = datetime.fromisoformat(issued.replace("Z", "+00:00"))
                base += (datetime.now(timezone.utc) - dt).total_seconds()
            except Exception:
                pass
        t = max(0, int(base))
        return f"{t // 60:02d}:{t % 60:02d}"

    # ── Message handling ────────────────────────────────────────────────

    def ingest(self, msg: dict):
        mtype = msg.get("type", "")
        self.msg_count += 1
        self.last_at = msg.get("at", "")

        if mtype == "hello":
            p = msg.get("payload", {})
            self._log(f"{FG}{B}HELLO{RST}  status={p.get('status','?')}")
            return

        games = []
        if mtype == "scoreboard":
            games = msg.get("scoreboard", {}).get("games", [])
        elif mtype == "update":
            games = msg.get("payload", {}).get("games", [])

        if games:
            for g in games:
                self._diff(g)
            self.games = sorted(games, key=lambda g: g.get("position", 0))

    def _diff(self, game: dict):
        gid = game.get("id", 0)
        pos = game.get("position", 0)
        teams = game.get("teams", [])
        clk = self.clock(game.get("timer"))
        new_st = game.get("status", "?")
        old_st = self._prev_status.get(gid)

        curr = {t["id"]: dict(t) for t in teams}
        prev = self._prev.get(gid)
        self._prev[gid] = curr
        self._prev_status[gid] = new_st

        if prev is not None:
            for tid, s in curr.items():
                old = prev.get(tid)
                if not old:
                    continue
                side = (s.get("side") or "?")[:3].upper()
                name = self.acr(tid)
                for key, label, color in [
                    ("kills",      "KILL",  FR),
                    ("towers",     "TOWER", FB),
                    ("drakes",     "DRAKE", FM),
                    ("nashors",    "BARON", FY),
                    ("inhibitors", "INHIB", FR),
                ]:
                    ov = old.get(key, 0) or 0
                    nv = s.get(key, 0) or 0
                    if nv != ov:
                        self._log(
                            f"{color}{B}{label:5}{RST}  G{pos} [{clk}]  "
                            f"{name} ({side}): {ov}->{nv} (+{nv-ov})"
                        )

        if old_st and old_st != new_st:
            self._log(f"{FG}{B}STATE{RST}  G{pos}: {old_st} -> {new_st}")
            if new_st == "finished":
                self._needs_score_refresh = True

    def _log(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.events.append(f"{DIM}{ts}{RST}  {text}")

    # ── Render ──────────────────────────────────────────────────────────

    def render(self) -> str:
        o: list[str] = []

        o.append(hr_top())
        o.append(row(f"{B}{FW}{self.match_name or 'Connecting...'}{RST}", 'c'))
        if self.league:
            o.append(row(f"{DIM}{self.league}{RST}", 'c'))
        o.append(hr_h())

        if not self.games:
            o.append(row(f"{DIM}Waiting for match data...{RST}", 'c'))
        else:
            t_ids = [t["id"] for t in self.games[0].get("teams", [])]
            if len(t_ids) >= 2:
                t1, t2 = t_ids[0], t_ids[1]
                a1, a2 = self.acr(t1), self.acr(t2)
                w1 = self.series_score.get(t1, 0)
                w2 = self.series_score.get(t2, 0)

                series = (
                    f"{B}{FW}{a1}{RST}  {FB}{B}{w1}{RST}"
                    f"  {DIM}—{RST}  "
                    f"{FR}{B}{w2}{RST}  {B}{FW}{a2}{RST}"
                )
                o.append(row(series, 'c'))
                o.append(hr_h())

                for g in self.games:
                    self._render_game(o, g, t1, t2)

        self._render_events(o)
        self._render_footer(o)

        return CLR + "\n".join(o)

    def _render_game(self, o: list[str], g: dict, t1: int, t2: int):
        pos = g.get("position", 0)
        status = g.get("status", "?")
        gt = g.get("teams", [])
        g1 = next((t for t in gt if t["id"] == t1), {})
        g2 = next((t for t in gt if t["id"] == t2), {})

        if status == "running":
            clk = self.clock(g.get("timer"))
            o.append(row(
                f"{FY}{B}GAME {pos}{RST}   "
                f"{FR}{B}● LIVE{RST}   "
                f"{B}{FW}{clk}{RST}",
                'c',
            ))
        elif status == "finished":
            o.append(row(
                f"GAME {pos}   {FG}✓ FINISHED{RST}",
                'c',
            ))
        else:
            o.append(row(f"{DIM}GAME {pos}   ○ PENDING{RST}", 'c'))

        if status not in ("running", "finished"):
            o.append(hr_h())
            return

        s1 = (g1.get("side") or "?").upper()
        s2 = (g2.get("side") or "?").upper()
        n1 = self.team_names.get(t1, self.acr(t1))
        n2 = self.team_names.get(t2, self.acr(t2))
        c1 = FB if s1 == "BLUE" else FR
        c2 = FB if s2 == "BLUE" else FR

        o.append(hr_hs())
        o.append(row2(
            f" {c1}{B}{s1}{RST}  {B}{n1}{RST}",
            f" {c2}{B}{s2}{RST}  {B}{n2}{RST}",
        ))
        o.append(hr_ls())

        for label, key in [
            ("Kills ", "kills"),
            ("Towers", "towers"),
            ("Drakes", "drakes"),
            ("Baron ", "nashors"),
            ("Inhibs", "inhibitors"),
        ]:
            v1 = g1.get(key, 0) or 0
            v2 = g2.get(key, 0) or 0
            if v1 > v2:
                d1, d2 = f"{FG}{B}{v1:>2}{RST}", f"{DIM}{v2:>2}{RST}"
            elif v2 > v1:
                d1, d2 = f"{DIM}{v1:>2}{RST}", f"{FG}{B}{v2:>2}{RST}"
            else:
                d1, d2 = f"{v1:>2}", f"{v2:>2}"
            o.append(row2(f"   {label}  {d1}", f"   {label}  {d2}"))

        draft = g.get("draft", {})
        picks = draft.get("picks", [])
        if picks:
            o.append(hr_ls())
            p1 = {p["role"]: p["champion_slug"] for p in picks if p.get("team_id") == t1}
            p2 = {p["role"]: p["champion_slug"] for p in picks if p.get("team_id") == t2}
            for role in ["top", "jun", "mid", "adc", "sup"]:
                ch1 = p1.get(role, "—")
                ch2 = p2.get(role, "—")
                o.append(row2(
                    f"   {FC}{role.upper():3}{RST}  {ch1}",
                    f"   {FC}{role.upper():3}{RST}  {ch2}",
                ))

        o.append(hr_h())

    def _render_events(self, o: list[str]):
        o.append(row(f"{B}EVENT LOG{RST}"))
        o.append(hr_l())
        evts = list(self.events)
        if evts:
            for ev in evts[-8:]:
                o.append(row(ev))
        else:
            o.append(row(f"{DIM}Waiting for events...{RST}"))
        o.append(hr_l())

    def _render_footer(self, o: list[str]):
        conn = f"{FG}● Connected{RST}" if self.connected else f"{FR}○ Disconnected{RST}"
        ts = self.last_at[11:19] if len(self.last_at) > 19 else "—"
        o.append(row(f"{conn}  {DIM}msgs: {self.msg_count}  last: {ts}{RST}"))
        o.append(hr_bot())


# ── Network ─────────────────────────────────────────────────────────────

async def fetch_match_info(db: Dashboard):
    if httpx is None:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{PS_BASE}/lol/matches/{db.match_id}",
                headers={"Authorization": f"Bearer {PS_KEY}"},
            )
            if r.status_code != 200:
                return
            m = r.json()
            db.match_name = m.get("name", f"Match #{db.match_id}")
            db.league = m.get("league", {}).get("name", "")
            for opp in m.get("opponents", []):
                o = opp.get("opponent", {})
                tid = o.get("id", 0)
                db.team_names[tid] = o.get("name", f"Team {tid}")
                db.team_acrs[tid] = o.get("acronym", o.get("name", "?")[:3].upper())
            for res in m.get("results", []):
                db.series_score[res["team_id"]] = res.get("score", 0)
    except Exception as e:
        db.events.append(f"{FR}API error: {e}{RST}")


async def refresh_score(db: Dashboard):
    """Re-fetch series score from API when a game finishes."""
    await asyncio.sleep(3)
    if httpx is None:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{PS_BASE}/lol/matches/{db.match_id}",
                headers={"Authorization": f"Bearer {PS_KEY}"},
            )
            if r.status_code == 200:
                m = r.json()
                for res in m.get("results", []):
                    db.series_score[res["team_id"]] = res.get("score", 0)
    except Exception:
        pass


async def ws_loop(db: Dashboard):
    url = f"wss://live.pandascore.co/matches/{db.match_id}/low_latency_feed?token={PS_KEY}"

    while True:
        try:
            db.connected = False
            db._log(f"{DIM}Connecting to LLF...{RST}")
            print(db.render(), flush=True)

            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=30,
                close_timeout=10,
                max_size=2**20,
            ) as ws:
                db.connected = True
                db._log(f"{FG}WebSocket connected{RST}")
                print(db.render(), flush=True)

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=120)
                    except asyncio.TimeoutError:
                        db._log(f"{FY}Timeout — reconnecting{RST}")
                        break

                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    db.ingest(msg)

                    if db._needs_score_refresh:
                        db._needs_score_refresh = False
                        asyncio.create_task(refresh_score(db))

                    print(db.render(), flush=True)

        except websockets.exceptions.ConnectionClosed as e:
            db.connected = False
            code = getattr(e, "code", 0) or 0
            reason = str(e).lower()
            if code == 4004 or "unavailable" in reason:
                db._log(f"{FY}LLF not open yet — retrying 30s{RST}")
                print(db.render(), flush=True)
                await asyncio.sleep(30)
            elif code == 1000 or "finished" in reason or "closing" in reason:
                db._log(f"{FG}{B}Match ended.{RST}")
                print(db.render(), flush=True)
                return
            else:
                db._log(f"{FR}Disconnected ({code}) — retrying 5s{RST}")
                print(db.render(), flush=True)
                await asyncio.sleep(5)

        except OSError:
            db.connected = False
            db._log(f"{FR}Network error — retrying 15s{RST}")
            print(db.render(), flush=True)
            await asyncio.sleep(15)

        except Exception as e:
            db.connected = False
            db._log(f"{FR}Error: {type(e).__name__}: {e}{RST}")
            print(db.render(), flush=True)
            await asyncio.sleep(10)


async def tick(db: Dashboard):
    while True:
        await asyncio.sleep(1)
        if any(g.get("status") == "running" for g in db.games):
            print(db.render(), flush=True)


async def main():
    match_id = 1407095
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.isdigit():
            match_id = int(arg)
        elif arg in ("-h", "--help"):
            print("Usage: python3 lol_dashboard.py [MATCH_ID]")
            return

    if not PS_KEY:
        print(f"{FR}PANDASCORE_API_KEY not set in .env{RST}")
        return

    db = Dashboard(match_id)

    print(HIDE, end="", flush=True)
    try:
        await fetch_match_info(db)
        print(db.render(), flush=True)
        await asyncio.gather(ws_loop(db), tick(db))
    finally:
        print(SHOW, end="", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(SHOW, end="", flush=True)
        print(f"\n{DIM}Dashboard closed.{RST}")
