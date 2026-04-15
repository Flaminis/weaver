#!/usr/bin/env python3
"""
LoL Live Web Dashboard — TradingView price charts + real-time game state.

Usage:
    python3 scripts/lol_web_dashboard.py [MATCH_ID]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import threading
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import httpx
import websockets

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
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HTTP_PORT = 8080
WS_PORT = 8765
HTML_FILE = Path(__file__).resolve().parent / "dashboard.html"


class State:
    def __init__(self, match_id: int):
        self.match_id = match_id
        self.match_name = ""
        self.league = ""
        self.team_names: dict[int, str] = {}
        self.team_acrs: dict[int, str] = {}
        self.series_score: dict[int, int] = {}
        self.games: list[dict] = []
        self.markets: list[dict] = []
        self.primary_token = ""
        self.primary_label = ""
        self.price_history: list[dict] = []
        self.events: list[dict] = []
        self.clients: set = set()
        self._prev: dict[int, dict[int, dict]] = {}
        self._prev_st: dict[int, str] = {}
        self.running = True
        self.has_llf = False
        self.llf_url = ""
        self.burst_until = 0.0
        self.book: dict = {"bids": [], "asks": []}


ST: State | None = None


# ── Broadcast ───────────────────────────────────────────────────────────

async def broadcast(msg: dict):
    if not ST or not ST.clients:
        return
    data = json.dumps(msg)
    dead = set()
    for ws in ST.clients:
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    ST.clients -= dead


# ── PandaScore API ──────────────────────────────────────────────────────

async def fetch_match_info():
    s = ST
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(
            f"{PS_BASE}/lol/matches/{s.match_id}",
            headers={"Authorization": f"Bearer {PS_KEY}"},
        )
        if r.status_code != 200:
            print(f"[!] PandaScore HTTP {r.status_code}")
            return
        m = r.json()
        s.match_name = m.get("name", f"Match #{s.match_id}")
        s.league = m.get("league", {}).get("name", "")
        llf = m.get("low_latency_feed", {})
        if llf.get("supported") and llf.get("url"):
            s.llf_url = llf["url"]
            s.has_llf = True
        for opp in m.get("opponents", []):
            o = opp.get("opponent", {})
            tid = o.get("id", 0)
            s.team_names[tid] = o.get("name", f"Team {tid}")
            s.team_acrs[tid] = o.get("acronym", o.get("name", "?")[:3].upper())
        for res in m.get("results", []):
            s.series_score[res["team_id"]] = res.get("score", 0)
        print(f"[+] {s.match_name} ({s.league}) LLF={'yes' if s.has_llf else 'no'}")


# ── Polymarket Discovery ───────────────────────────────────────────────

async def discover_markets():
    s = ST
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(f"{GAMMA}/events", params={
                "tag_id": 64, "active": "true", "closed": "false", "limit": 200,
            })
            if r.status_code != 200:
                return
            for ev in r.json():
                title = (ev.get("title") or "").lower()
                matched = True
                for name in s.team_names.values():
                    words = [w for w in name.lower().split()
                             if w not in ("team", "esports", "gaming", "esport")]
                    key = max(words, key=len) if words else name.lower()
                    if key not in title:
                        matched = False
                        break
                if not matched or not s.team_names:
                    continue

                print(f"[+] Polymarket: {ev.get('title')}")
                for mkt in ev.get("markets", []):
                    q = mkt.get("question", "")
                    outcomes = mkt.get("outcomes", "[]")
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    clob_ids = mkt.get("clobTokenIds", "[]")
                    if isinstance(clob_ids, str):
                        clob_ids = json.loads(clob_ids)
                    if len(clob_ids) < 2 or len(outcomes) < 2:
                        continue
                    ql = q.lower()
                    if "(bo3)" in ql or (
                        any(w in ql for w in ("match", "moneyline"))
                        and "game" not in ql
                    ):
                        mtype = "match_winner"
                    elif "game" in ql and "winner" in ql:
                        mtype = "game_winner"
                    elif "kill" in ql and ("over" in ql or "under" in ql):
                        mtype = "kill_ou"
                    elif "handicap" in ql:
                        mtype = "handicap"
                    else:
                        mtype = "other"
                    s.markets.append({
                        "question": q, "type": mtype,
                        "token_yes": clob_ids[0], "token_no": clob_ids[1],
                        "outcomes": outcomes,
                    })

                for m in s.markets:
                    if m["type"] == "match_winner":
                        s.primary_token = m["token_yes"]
                        s.primary_label = m["question"]
                        break
                if not s.primary_token and s.markets:
                    s.primary_token = s.markets[0]["token_yes"]
                    s.primary_label = s.markets[0]["question"]
                print(f"[+] {len(s.markets)} markets, primary: {s.primary_label or 'none'}")
                return
    except Exception as e:
        print(f"[!] Market discovery: {e}")


# ── LLF Client ──────────────────────────────────────────────────────────

def _clock(timer_obj: dict | None) -> str:
    if not timer_obj:
        return "00:00"
    base = timer_obj.get("timer", 0) or 0
    if not timer_obj.get("paused", True) and timer_obj.get("issued_at"):
        try:
            dt = datetime.fromisoformat(timer_obj["issued_at"].replace("Z", "+00:00"))
            base += (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            pass
    t = max(0, int(base))
    return f"{t // 60:02d}:{t % 60:02d}"


def _detect_changes(game: dict):
    s = ST
    gid = game.get("id", 0)
    pos = game.get("position", 0)
    teams = game.get("teams", [])
    clk = _clock(game.get("timer"))
    curr = {t["id"]: dict(t) for t in teams}
    prev = s._prev.get(gid)
    s._prev[gid] = curr
    if prev is None:
        return
    now = time.time()

    snap = {
        "game_id": gid, "position": pos, "clock": clk,
        "before": {str(k): v for k, v in prev.items()},
        "after": {str(k): v for k, v in curr.items()},
        "timer": game.get("timer"),
        "draft": game.get("draft"),
    }

    for tid, st in curr.items():
        old = prev.get(tid)
        if not old:
            continue
        side = (st.get("side") or "?")[:3].upper()
        name = s.team_acrs.get(tid, f"T{tid}")
        for key, label in [("kills", "KILL"), ("towers", "TOWER"), ("drakes", "DRAKE"),
                           ("nashors", "BARON"), ("inhibitors", "INHIB")]:
            ov = old.get(key, 0) or 0
            nv = st.get(key, 0) or 0
            if nv != ov:
                ev = {
                    "ts": now,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "etype": label,
                    "game": pos,
                    "clock": clk,
                    "desc": f"{name} ({side}): {ov}→{nv} (+{nv - ov})",
                    "raw": snap,
                }
                s.events.append(ev)
                if len(s.events) > 200:
                    s.events = s.events[-100:]
                s.burst_until = now + 15
                asyncio.create_task(broadcast({"type": "event", **ev}))

    new_status = game.get("status", "?")
    old_status = s._prev_st.get(gid)
    s._prev_st[gid] = new_status
    if old_status and old_status != new_status:
        ev = {
            "ts": now, "time": datetime.now().strftime("%H:%M:%S"),
            "etype": "STATE", "game": pos, "clock": clk,
            "desc": f"Game {pos}: {old_status} → {new_status}",
            "raw": {"status_change": {"from": old_status, "to": new_status}, **snap},
        }
        s.events.append(ev)
        asyncio.create_task(broadcast({"type": "event", **ev}))
        if new_status == "finished":
            asyncio.create_task(_refresh_score())


async def _refresh_score():
    await asyncio.sleep(3)
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{PS_BASE}/lol/matches/{ST.match_id}",
                headers={"Authorization": f"Bearer {PS_KEY}"},
            )
            if r.status_code == 200:
                for res in r.json().get("results", []):
                    ST.series_score[res["team_id"]] = res.get("score", 0)
    except Exception:
        pass


async def llf_client():
    s = ST
    if not s.has_llf:
        print("[.] No LLF for this match")
        return
    url = f"{s.llf_url}?token={PS_KEY}"
    while s.running:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                print("[+] LLF connected")
                while s.running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=120)
                    except asyncio.TimeoutError:
                        break
                    msg = json.loads(raw)
                    mtype = msg.get("type", "")
                    if mtype == "hello":
                        continue
                    games = []
                    if mtype == "scoreboard":
                        games = msg.get("scoreboard", {}).get("games", [])
                    elif mtype == "update":
                        games = msg.get("payload", {}).get("games", [])
                    if games:
                        for g in games:
                            _detect_changes(g)
                        s.games = sorted(games, key=lambda g: g.get("position", 0))
                        await broadcast({
                            "type": "game", "games": s.games,
                            "series_score": {str(k): v for k, v in s.series_score.items()},
                        })
        except websockets.exceptions.ConnectionClosed as e:
            code = getattr(e, "code", 0) or 0
            if code == 1000 or "finished" in str(e).lower():
                print("[+] LLF: match ended")
                return
            elif code == 4004 or "unavailable" in str(e).lower():
                print("[.] LLF not open yet — 30s")
                await asyncio.sleep(30)
            else:
                print(f"[!] LLF closed ({code}) — 5s")
                await asyncio.sleep(5)
        except Exception as e:
            print(f"[!] LLF: {e} — 10s")
            await asyncio.sleep(10)


# ── Price Poller ────────────────────────────────────────────────────────

async def price_poller():
    s = ST
    while s.running:
        if not s.primary_token:
            await asyncio.sleep(5)
            continue
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                while s.running and s.primary_token:
                    bid_r, ask_r = await asyncio.gather(
                        http.get(f"{CLOB}/price",
                                 params={"token_id": s.primary_token, "side": "buy"}),
                        http.get(f"{CLOB}/price",
                                 params={"token_id": s.primary_token, "side": "sell"}),
                    )
                    ts = time.time()
                    bid = float(bid_r.json().get("price", 0)) if bid_r.status_code == 200 else 0
                    ask = float(ask_r.json().get("price", 0)) if ask_r.status_code == 200 else 0
                    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
                    if mid > 0:
                        tick = {"ts": ts, "mid": mid, "bid": bid, "ask": ask}
                        s.price_history.append(tick)
                        if len(s.price_history) > 7200:
                            s.price_history = s.price_history[-3600:]
                        await broadcast({"type": "price", **tick})
                    interval = 0.3 if time.time() < s.burst_until else 1.5
                    await asyncio.sleep(interval)
        except Exception as e:
            print(f"[!] Price poller: {e}")
            await asyncio.sleep(5)


# ── Orderbook Poller ────────────────────────────────────────────────────

async def book_poller():
    s = ST
    while s.running:
        if not s.primary_token:
            await asyncio.sleep(5)
            continue
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                while s.running and s.primary_token:
                    r = await http.get(
                        f"{CLOB}/book",
                        params={"token_id": s.primary_token},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        bids = sorted(
                            [{"p": float(o["price"]), "s": float(o["size"])}
                             for o in (data.get("bids") or [])],
                            key=lambda x: -x["p"],
                        )[:8]
                        asks = sorted(
                            [{"p": float(o["price"]), "s": float(o["size"])}
                             for o in (data.get("asks") or [])],
                            key=lambda x: x["p"],
                        )[:8]
                        s.book = {"bids": bids, "asks": asks}
                        await broadcast({"type": "book", **s.book})
                    await asyncio.sleep(2)
        except Exception as e:
            print(f"[!] Book poller: {e}")
            await asyncio.sleep(5)


# ── WebSocket Handler ───────────────────────────────────────────────────

async def ws_handler(websocket, path=None):
    s = ST
    s.clients.add(websocket)
    print(f"[+] Browser connected ({len(s.clients)})")
    try:
        init = {
            "type": "init",
            "match_id": s.match_id,
            "match_name": s.match_name,
            "league": s.league,
            "team_names": {str(k): v for k, v in s.team_names.items()},
            "team_acrs": {str(k): v for k, v in s.team_acrs.items()},
            "series_score": {str(k): v for k, v in s.series_score.items()},
            "games": s.games,
            "markets": [{"question": m["question"], "type": m["type"]} for m in s.markets],
            "primary_label": s.primary_label,
            "has_llf": s.has_llf,
            "price_history": s.price_history[-600:],
            "events": s.events[-50:],
            "book": s.book,
        }
        await websocket.send(json.dumps(init))
        async for _ in websocket:
            pass
    except Exception:
        pass
    finally:
        s.clients.discard(websocket)
        print(f"[-] Browser disconnected ({len(s.clients)})")


# ── HTTP Server ─────────────────────────────────────────────────────────

class HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html", "/dashboard.html"):
            try:
                content = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(content))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "dashboard.html not found")
        else:
            self.send_error(404)

    def log_message(self, *_):
        pass


def start_http():
    srv = HTTPServer(("0.0.0.0", HTTP_PORT), HTTPHandler)
    srv.serve_forever()


# ── Main ────────────────────────────────────────────────────────────────

async def main():
    global ST
    match_id = 1407095
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        match_id = int(sys.argv[1])

    if not PS_KEY:
        print("[!] PANDASCORE_API_KEY not set")
        return

    ST = State(match_id)
    print(f"[*] LoL Live Web Dashboard — Match #{match_id}")

    await fetch_match_info()
    await discover_markets()

    threading.Thread(target=start_http, daemon=True).start()
    print(f"[*] http://localhost:{HTTP_PORT}")

    ws_srv = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
    print(f"[*] ws://localhost:{WS_PORT}")

    webbrowser.open(f"http://localhost:{HTTP_PORT}")

    await asyncio.gather(
        llf_client(),
        price_poller(),
        book_poller(),
    )

    ws_srv.close()
    await ws_srv.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Shutdown.")
