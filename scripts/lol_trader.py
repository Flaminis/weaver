#!/usr/bin/env python3
"""
LoL Polymarket Trading Bot — Oracle-LoL.

Connects to PandaScore LLF for real-time LoL game state.
Detects events (kills, drakes, barons, inhibitors).
Places FAK buy orders on Polymarket CLOB.
Auto-sells after 30s via GTC limit order (maker).
Falls back to FAK sell if GTC doesn't fill.

Usage:
    python3 scripts/lol_trader.py                   # dry run
    python3 scripts/lol_trader.py --live             # real money
    python3 scripts/lol_trader.py --live --bankroll 100
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import lol_trader_config as cfg
from lol_signal import (
    EventType, LolEvent, Signal, SignalModel,
    _STAT_KEY_MAP, _get_impact_model,
    safe_predict_win_prob, safe_predict_impact_from_llf,
)
from lol_risk import Position, RiskManager
from polymarket.client import poly_client
from polymarket.ws_prices import BookState, MarketWebSocket
from polymarket.logger import get_logger

log = get_logger("trader")

PS_KEY = os.environ.get("PANDASCORE_API_KEY", "")

# ── CLOB fill parsing (avoids phantom positions on bad / missing fills) ──


def _coerce_trade_fill_shares(fill: dict, expected_shares: float) -> tuple[float, str | None]:
    """Extract outcome-token shares from a get_trades row; rescale obvious 1e3/1e6 mistakes."""
    raw = None
    for k in ("size", "size_matched", "matched_amount", "amount"):
        if k in fill and fill[k] not in (None, ""):
            raw = fill[k]
            break
    if raw is None:
        return max(expected_shares, 0.0), None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return expected_shares, "parse_fail"
    if expected_shares <= 0.05:
        return v, None
    if v <= expected_shares * 250:
        return v, None
    for div, tag in ((1_000_000, "÷1e6"), (1_000, "÷1e3"), (100, "÷100")):
        v2 = v / div
        if expected_shares * 0.02 <= v2 <= expected_shares * 250:
            return v2, f"rescaled{tag}"
    return v, None


def _fill_size_plausible(fill_size: float, expected_shares: float) -> bool:
    """Reject insane API values so we don't open a 990k-sh position by mistake."""
    if fill_size <= 0 or fill_size > 2_000_000:
        return False
    if expected_shares <= 0.05:
        return fill_size <= 500_000
    ratio = fill_size / expected_shares
    return 0.02 <= ratio <= 150


# ── Data classes ────────────────────────────────────────────────────────


def _norm_llf(d: dict | None) -> dict[str, int]:
    """LLF scoreboard stats → canonical dict accepted by FeatureVectorBuilder.
    LLF uses 'nashors' and 'inhibitors'; the builder accepts either shape."""
    if not d:
        return {"kills": 0, "towers": 0, "drakes": 0, "barons": 0, "inhibs": 0}
    return {
        "kills":  int(d.get("kills", 0) or 0),
        "towers": int(d.get("towers", 0) or 0),
        "drakes": int(d.get("drakes", 0) or 0),
        "barons": int(d.get("nashors", d.get("barons", 0)) or 0),
        "inhibs": int(d.get("inhibitors", d.get("inhibs", 0)) or 0),
    }


def _scoreboard_changed(prev_entry: dict, blue_now: dict, red_now: dict) -> bool:
    """True if any tracked stat differs between prev history entry and now.
    Keeps state_history from bloating with identical snapshots."""
    keys = ("kills", "towers", "drakes", "nashors", "inhibitors")
    pb = prev_entry.get("blue", {}) or {}
    pr = prev_entry.get("red", {}) or {}
    for k in keys:
        if (pb.get(k, 0) or 0) != (blue_now.get(k, 0) or 0):
            return True
        if (pr.get(k, 0) or 0) != (red_now.get(k, 0) or 0):
            return True
    return False


@dataclass
class MarketSlot:
    """One Polymarket market for a match (series winner, game 1, game 2, etc.)."""
    question: str
    market_type: str          # "series", "game_1", "game_2", "game_3", etc.
    game_number: int          # 0 for series, 1/2/3/... for game markets
    token_a: str
    token_b: str
    condition_id: str
    neg_risk: bool


@dataclass
class LiveMatch:
    ps_match_id: int
    name: str
    llf_url: str
    team_a: str
    team_b: str
    team_a_id: int = 0
    team_b_id: int = 0
    signal_model: SignalModel | None = None
    _prev_teams: dict[int, dict] = field(default_factory=dict)
    _prev_status: dict[int, str] = field(default_factory=dict)
    games: list[dict] = field(default_factory=list)
    series_score: dict[int, int] = field(default_factory=dict)
    active: bool = True
    finished_at: float = 0.0
    scheduled_at: float = 0.0
    league: str = ""
    status: str = ""
    all_markets: list[MarketSlot] = field(default_factory=list)
    _current_game_num: int = 0
    _price_log: list[tuple[float, float]] = field(default_factory=list)
    _model_prob_log: list[tuple[float, float]] = field(default_factory=list)
    # Per-game rolling scoreboard history for momentum features (v2 model).
    # Keyed by game_id → list of (ts_sec, {"blue": {kills,...}, "red": {kills,...}}).
    # Trimmed to last 15 minutes to bound memory.
    _state_history: dict[int, list[tuple[float, dict]]] = field(default_factory=dict)
    # Champion picks per game — {game_id: {team_id: [list of 5 champion slugs by role order]}}.
    # Populated from LLF draft.picks on first scoreboard with complete draft.
    _champion_picks: dict[int, dict[int, list[str]]] = field(default_factory=dict)
    # Gamma event metadata
    gamma: dict = field(default_factory=dict)
    # LLF debug
    llf_status: str = ""
    llf_last_msg_at: float = 0
    llf_last_msg_type: str = ""
    llf_msg_count: int = 0
    # Last time we applied games[] from LLF (scoreboard/update payload) — for dashboard staleness.
    llf_scoreboard_updated_at: float = 0.0

    @property
    def active_market(self) -> MarketSlot | None:
        """Pick the best market for the currently live game.

        For deciding games (G3 in BO3, G5 in BO5), use the SERIES market
        because game winner = series winner and series has more liquidity.

        For non-deciding games, prefer game-specific market, fall back to series.
        """
        gn = self._current_game_num
        series_mkt = next((m for m in self.all_markets if m.market_type == "series"), None)

        if gn > 0:
            # Detect deciding game: BO3 G3, BO5 G5
            bo = 3
            score = self.gamma.get("score", "")
            if "Bo5" in score or "bo5" in score:
                bo = 5
            deciding_game = (bo == 3 and gn >= 3) or (bo == 5 and gn >= 5)

            if deciding_game and series_mkt:
                return series_mkt

            for m in self.all_markets:
                if m.game_number == gn:
                    return m

        return series_mkt or (self.all_markets[0] if self.all_markets else None)

    @property
    def token_a(self) -> str:
        m = self.active_market
        return m.token_a if m else ""

    @property
    def token_b(self) -> str:
        m = self.active_market
        return m.token_b if m else ""

    @property
    def condition_id(self) -> str:
        m = self.active_market
        return m.condition_id if m else ""

    @property
    def neg_risk(self) -> bool:
        m = self.active_market
        return m.neg_risk if m else False

    @property
    def market_question(self) -> str:
        m = self.active_market
        return m.question if m else ""


# ── Trader ──────────────────────────────────────────────────────────────


class LoLTrader:
    def __init__(self, dry_run: bool = True, bankroll: float = 500.0):
        self.dry_run = dry_run
        self.risk = RiskManager(bankroll)
        self.matches: dict[int, LiveMatch] = {}
        self.ws_prices = MarketWebSocket(on_price_update=self._on_price_update)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clob")
        self._http: httpx.AsyncClient | None = None
        self._running = True
        self._start_time = time.time()
        self._recent_events: list[dict] = []
        self._token_match_map: dict[str, LiveMatch] = {}
        self._rest_book_cache: dict[str, BookState] = {}

    def _rebuild_token_map(self):
        self._token_match_map.clear()
        for m in self.matches.values():
            # Map active market token → match
            if m.token_a:
                self._token_match_map[m.token_a] = m
            # Map series market token → match (fallback)
            for slot in m.all_markets:
                if slot.market_type == "series":
                    self._token_match_map[slot.token_a] = m

    # ── Startup ─────────────────────────────────────────────────────────

    async def start(self):
        log.info("="*60)
        log.info("  LoL Polymarket Trader%s", " (DRY RUN)" if self.dry_run else " (LIVE)")
        log.info("  Bankroll: $%.2f", self.risk.bankroll)
        log.info("  Spread gate: %.0fc | Exit: HOLD TO RESOLUTION",
                 cfg.MAX_SPREAD * 100)
        log.info("="*60)

        self._http = httpx.AsyncClient(timeout=15)

        if not self.dry_run:
            log.info("Connecting to Polymarket CLOB...")
            await poly_client.connect()
            if poly_client.is_ready:
                bal = await asyncio.get_event_loop().run_in_executor(
                    self._executor, poly_client.get_balance)
                log.info("CLOB balance: $%.2f (allowance $%.2f)",
                         bal["balance"], bal["allowance"])
                await asyncio.get_event_loop().run_in_executor(
                    self._executor, poly_client.cancel_all)
                log.info("Cancelled all stale orders")
            else:
                log.warning("CLOB not ready — falling back to dry run")
                self.dry_run = True

        log.info("Discovering LoL matches with LLF...")
        await self._discover_matches()

        if not self.matches:
            log.warning("No matches with LLF found. Exiting.")
            return

        log.info("Found %d match(es) with LLF", len(self.matches))

        log.info("Discovering Polymarket markets...")
        await self._discover_markets()

        log.info("Starting price WebSocket...")
        await self.ws_prices.start()

        tasks = [
            asyncio.create_task(self._exit_loop()),
            asyncio.create_task(self._market_refresh_loop()),
            asyncio.create_task(self._rest_book_loop()),
            asyncio.create_task(self._model_rescore_loop()),
        ]
        self._llf_tasks: dict[int, asyncio.Task] = {}
        self._start_llf_for_priority_matches()

        log.info("All systems running. Listening for events...\n")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            await self.ws_prices.stop()
            if self._http:
                await self._http.aclose()
            log.info("\n%s", self.risk.session_report())
            self.risk.save_trades()

    # ── REST book fallback (like Gelik) ───────────────────────────────

    async def _rest_book_loop(self):
        """Poll REST /book for active markets where WS has no data. Runs every 2s."""
        while self._running:
            await asyncio.sleep(2)
            for m in self.matches.values():
                if not m.active or not m.token_a:
                    continue
                ws_book = self.ws_prices.get_book(m.token_a)
                if ws_book and ws_book.has_book and (time.time() - ws_book.last_update) < 3.0:
                    continue  # WS is fresh, no need for REST
                try:
                    r = await self._http.get(
                        f"{cfg.CLOB_API}/book",
                        params={"token_id": m.token_a},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        bids = data.get("bids", [])
                        asks = data.get("asks", [])
                        if bids or asks:
                            bs = BookState(token_id=m.token_a)
                            rest_bids = [{"price": str(b.get("price", b.price if hasattr(b, "price") else 0)),
                                          "size": str(b.get("size", b.size if hasattr(b, "size") else 0))}
                                         for b in bids]
                            rest_asks = [{"price": str(a.get("price", a.price if hasattr(a, "price") else 0)),
                                          "size": str(a.get("size", a.size if hasattr(a, "size") else 0))}
                                         for a in asks]
                            bs.update_from_book(rest_bids, rest_asks)
                            self._rest_book_cache[m.token_a] = bs
                            if bs.has_book:
                                m._price_log.append((time.time(), bs.mid))
                except Exception:
                    pass

    # ── LLF connection management (max 3) ─────────────────────────────

    def _start_llf_for_priority_matches(self):
        """Connect LLF to top 3 matches by volume. Only consider matches that
        are running OR scheduled to start within 2 hours. Sticky: once a match
        receives LLF data (game started), it keeps its slot until the match finishes."""
        MAX_LLF = 3
        LOOKAHEAD_SEC = 2 * 3600  # 2 hours
        now = time.time()

        # Sticky matches: already connected AND have received game data (llf_msg_count > 0)
        sticky_ids: set[int] = set()
        for mid, task in self._llf_tasks.items():
            if task.done():
                continue
            m = self.matches.get(mid)
            if m and m.active and m.llf_msg_count > 0:
                sticky_ids.add(mid)

        free_slots = MAX_LLF - len(sticky_ids)

        candidates = []
        for m in self.matches.values():
            if m.ps_match_id in sticky_ids:
                continue
            if not m.active or not m.llf_url or not m.all_markets:
                continue
            # Already running — always eligible
            if m.status == "running":
                pass
            # Not running — only if scheduled within lookahead window
            elif m.scheduled_at > 0 and m.scheduled_at > now + LOOKAHEAD_SEC:
                continue
            vol = m.gamma.get("volume", 0) if m.gamma else 0
            candidates.append((vol, m.ps_match_id, m))
        candidates.sort(key=lambda x: -x[0])

        target_ids = set(sticky_ids)
        for _, mid, m in candidates[:max(free_slots, 0)]:
            target_ids.add(mid)

        for mid in target_ids:
            if mid not in self._llf_tasks or self._llf_tasks[mid].done():
                m = self.matches.get(mid)
                if m:
                    task = asyncio.create_task(self._llf_listener(m))
                    self._llf_tasks[mid] = task
                    vol = m.gamma.get("volume", 0) if m.gamma else 0
                    starts_in = ""
                    if m.scheduled_at > 0:
                        mins = max(0, (m.scheduled_at - now) / 60)
                        starts_in = f" starts={mins:.0f}m" if mins > 0 else " LIVE"
                    log.info("LLF CONNECT: %s (vol=$%.0f%s%s)", m.name, vol,
                             starts_in, " STICKY" if mid in sticky_ids else "")

        for mid, task in list(self._llf_tasks.items()):
            if mid not in target_ids and not task.done():
                m = self.matches.get(mid)
                name = m.name if m else mid
                task.cancel()
                del self._llf_tasks[mid]
                log.info("LLF DISCONNECT: %s (outprioritized)", name)

    # ── Match discovery (PandaScore) ────────────────────────────────────

    async def _discover_matches(self):
        for endpoint in ["running", "upcoming"]:
            try:
                r = await self._http.get(
                    f"{cfg.PS_BASE}/lol/matches/{endpoint}",
                    headers={"Authorization": f"Bearer {PS_KEY}"},
                    params={"filter[low_latency_feed]": "true", "sort": "scheduled_at", "per_page": 20},
                )
                if r.status_code != 200:
                    continue
                for m in r.json():
                    mid = m["id"]
                    if mid in self.matches:
                        continue
                    llf = m.get("low_latency_feed", {})
                    if not llf.get("supported") or not llf.get("url"):
                        continue
                    opponents = m.get("opponents", [])
                    if len(opponents) < 2:
                        continue
                    ta = opponents[0].get("opponent", {})
                    tb = opponents[1].get("opponent", {})
                    match = LiveMatch(
                        ps_match_id=mid,
                        name=m.get("name", f"{ta.get('name','?')} vs {tb.get('name','?')}"),
                        llf_url=llf["url"],
                        team_a=ta.get("name", "?"),
                        team_b=tb.get("name", "?"),
                        team_a_id=ta.get("id", 0),
                        team_b_id=tb.get("id", 0),
                    )
                    match.signal_model = SignalModel(match.team_a_id, match.team_b_id)
                    match.league = m.get("league", {}).get("name", "?")
                    match.status = m.get("status", "?")
                    sched = m.get("scheduled_at") or m.get("begin_at") or ""
                    if sched:
                        try:
                            from datetime import datetime, timezone
                            match.scheduled_at = datetime.fromisoformat(sched.replace("Z", "+00:00")).timestamp()
                        except Exception:
                            pass
                    self.matches[mid] = match
                    log.info("MATCH: #%d %s [%s] status=%s",
                             mid, match.name, match.league, match.status)
            except Exception as e:
                log.warning("Match discovery error (%s): %s", endpoint, e)

    # ── Market discovery (Polymarket Gamma) ─────────────────────────────

    async def _discover_markets(self):
        """Find ALL Polymarket markets for each match:
        series winner, game 1 winner, game 2 winner, etc.
        Subscribe to WS prices for all of them.
        The active market is chosen based on which game is currently live.
        """
        import re
        try:
            r = await self._http.get(f"{cfg.GAMMA_API}/events", params={
                "tag_id": cfg.ESPORTS_TAG_ID, "active": "true", "closed": "false", "limit": 500,
            })
            if r.status_code != 200:
                return
            for ev in r.json():
                title = (ev.get("title") or "").lower()
                if "lol:" not in title and "league of legends" not in title:
                    continue
                for match in self.matches.values():
                    if match.all_markets:
                        continue
                    ta_words = [w for w in match.team_a.lower().split()
                                if w not in ("team", "esports", "gaming")]
                    tb_words = [w for w in match.team_b.lower().split()
                                if w not in ("team", "esports", "gaming")]
                    ta_key = max(ta_words, key=len) if ta_words else match.team_a.lower()
                    tb_key = max(tb_words, key=len) if tb_words else match.team_b.lower()
                    if ta_key not in title or tb_key not in title:
                        continue

                    found_markets: list[MarketSlot] = []
                    for mkt in ev.get("markets", []):
                        q = mkt.get("question", "")
                        ql = q.lower()

                        # ONLY accept "winner" markets and series moneyline
                        is_winner = "winner" in ql
                        is_series = "(bo" in ql and "winner" not in ql  # "Team A vs Team B (BO3)"
                        if not is_winner and not is_series:
                            continue

                        clob_ids = mkt.get("clobTokenIds", "[]")
                        if isinstance(clob_ids, str):
                            clob_ids = json.loads(clob_ids)
                        if len(clob_ids) < 2:
                            continue
                        if not mkt.get("active") or mkt.get("closed"):
                            continue

                        game_match = re.search(r'game\s*(\d+)', ql)
                        if game_match:
                            gn = int(game_match.group(1))
                            mtype = f"game_{gn}"
                        elif "(bo" in ql or "match" in ql or ("(bo3)" in ql or "(bo5)" in ql):
                            gn = 0
                            mtype = "series"
                        else:
                            gn = 0
                            mtype = "series"

                        slot = MarketSlot(
                            question=q,
                            market_type=mtype,
                            game_number=gn,
                            token_a=clob_ids[0],
                            token_b=clob_ids[1],
                            condition_id=mkt.get("conditionId", ""),
                            neg_risk=bool(mkt.get("negRisk")),
                        )
                        found_markets.append(slot)

                    if found_markets:
                        match.all_markets = found_markets
                        game_mkts = [m for m in found_markets if m.game_number > 0]
                        series_mkts = [m for m in found_markets if m.game_number == 0]

                        meta = ev.get("eventMetadata", {})
                        if isinstance(meta, str):
                            try: meta = json.loads(meta)
                            except: meta = {}
                        match.gamma = {
                            "title": ev.get("title", ""),
                            "live": ev.get("live", False),
                            "score": ev.get("score", ""),
                            "period": ev.get("period", ""),
                            "volume": ev.get("volume", 0),
                            "liquidity": ev.get("liquidity", 0),
                            "open_interest": ev.get("openInterest", 0),
                            "start_time": ev.get("startTime", ""),
                            "end_date": ev.get("endDate", ""),
                            "description": ev.get("description", ""),
                            "resolution_source": ev.get("resolutionSource", ""),
                            "competitive": ev.get("competitive", 0),
                            "league": meta.get("league", ""),
                            "league_tier": meta.get("leagueTier", ""),
                            "context": meta.get("context_description", ""),
                            "teams": ev.get("teams", []),
                            "icon": ev.get("icon", ""),
                        }

                        # Only subscribe WS to series + active game market (not all games)
                        for slot in found_markets:
                            if slot.market_type == "series":
                                self.ws_prices.subscribe(slot.token_a)
                                self.ws_prices.subscribe(slot.token_b)
                        active = match.active_market
                        if active and active.market_type != "series":
                            self.ws_prices.subscribe(active.token_a)
                            self.ws_prices.subscribe(active.token_b)

                        self._rebuild_token_map()
                        log.info("MARKETS: %s → %d game + %d series | vol=$%.0f liq=$%.0f score=%s",
                                 match.name, len(game_mkts), len(series_mkts),
                                 ev.get("volume", 0), ev.get("liquidity", 0),
                                 ev.get("score", "?"))
        except Exception as e:
            log.warning("Market discovery error: %s", e)

    async def _market_refresh_loop(self):
        while self._running:
            await asyncio.sleep(30)
            await self._discover_matches()
            await self._discover_markets()
            await self._check_finished_matches()
            self._start_llf_for_priority_matches()

    async def _check_finished_matches(self):
        """Check PandaScore for finished matches and mark them inactive.
        Also detect resolved Polymarket markets (price near 0 or 1).
        Matches stay in self.matches for history — just flagged inactive.
        """
        now = time.time()
        for mid, m in self.matches.items():
            if not m.active:
                continue

            # If current market is near-resolved, check if it's a game market
            # that finished while the series continues. If so, switch market — don't kill match.
            book = self._get_book(m)
            if book and book.has_book and (book.mid < 0.005 or book.mid > 0.995):
                active_mkt = m.active_market
                if active_mkt and active_mkt.game_number > 0:
                    # Game market resolved — switch to series or next game
                    series_mkt = next((mk for mk in m.all_markets if mk.market_type == "series"), None)
                    if series_mkt and series_mkt.token_a != active_mkt.token_a:
                        m._current_game_num = 0  # Reset to series
                        self.ws_prices.subscribe(series_mkt.token_a)
                        self.ws_prices.subscribe(series_mkt.token_b)
                        log.info("GAME RESOLVED: %s %s → switching to series market",
                                 m.name, active_mkt.market_type)
                        for pos in self.risk.open_positions:
                            if pos.match_id == mid and pos.token_id == active_mkt.token_a:
                                resolved_price = 1.0 if book.mid > 0.5 else 0.0
                                self.risk.resolve_position(pos, resolved_price)
                        m.finished_at = 0
                        continue
                # Series market or no game market — check if truly finished
                if m.finished_at == 0:
                    m.finished_at = now
                elif now - m.finished_at > 120:
                    if m.status == "finished":
                        m.active = False
                        resolved_price = 1.0 if book.mid > 0.5 else 0.0
                        for pos in self.risk.open_positions:
                            if pos.match_id == mid:
                                self.risk.resolve_position(pos, resolved_price)
                        log.info("MATCH RESOLVED: %s (price=%.3f)", m.name, book.mid)
                    else:
                        m.finished_at = 0  # PandaScore says still running
            else:
                m.finished_at = 0

        # Check PandaScore for status updates on ALL active matches
        try:
            active_ids = [str(mid) for mid in self.matches if self.matches[mid].active]
            if active_ids:
                r = await self._http.get(
                    f"{cfg.PS_BASE}/lol/matches",
                    headers={"Authorization": f"Bearer {PS_KEY}"},
                    params={"filter[id]": ",".join(active_ids), "per_page": 50},
                )
                if r.status_code == 200:
                    by_id = {m["id"]: m for m in r.json()}
                    for mid, m in self.matches.items():
                        if not m.active:
                            continue
                        ps = by_id.get(mid)
                        if not ps:
                            continue
                        new_status = ps.get("status", m.status)
                        if new_status != m.status:
                            log.info("MATCH STATUS: %s %s → %s", m.name, m.status, new_status)
                            m.status = new_status
                        if new_status == "finished":
                            m.active = False
                            m.finished_at = now
                            for res in ps.get("results", []):
                                m.series_score[res["team_id"]] = res.get("score", 0)
                            log.info("MATCH FINISHED: %s", m.name)
        except Exception as e:
            log.warning("Match status check error: %s", e)

        # Refresh Gamma live flag for matches with markets
        try:
            r = await self._http.get(f"{cfg.GAMMA_API}/events", params={
                "tag_id": cfg.ESPORTS_TAG_ID, "active": "true", "closed": "false", "limit": 500,
            })
            if r.status_code == 200:
                for ev in r.json():
                    title = (ev.get("title") or "").lower()
                    for m in self.matches.values():
                        if not m.gamma or not m.all_markets:
                            continue
                        if m.gamma.get("title", "").lower() == title or ev.get("id") == m.gamma.get("id"):
                            m.gamma["live"] = ev.get("live", False)
                            m.gamma["score"] = ev.get("score", m.gamma.get("score", ""))
                            m.gamma["volume"] = ev.get("volume", m.gamma.get("volume", 0))
                            m.gamma["liquidity"] = ev.get("liquidity", m.gamma.get("liquidity", 0))
                            m.gamma["period"] = ev.get("period", m.gamma.get("period", ""))
                            break
        except Exception as e:
            log.warning("Gamma refresh error: %s", e)

    # ── Price callback ──────────────────────────────────────────────────

    def _on_price_update(self, token_id: str, book: BookState):
        if not book.has_book:
            return
        now = time.time()
        m = self._token_match_map.get(token_id)
        if m:
            # Only record if this is the active market's token (not stale series for game market)
            active = m.active_market
            is_active_token = active and active.token_a == token_id
            is_series = any(s.token_a == token_id and s.market_type == "series" for s in m.all_markets)
            if is_active_token or (is_series and not active):
                m._price_log.append((now, book.mid))
                if len(m._price_log) > 5000:
                    m._price_log = m._price_log[-2500:]

    def _get_book(self, match: LiveMatch) -> BookState | None:
        """Get book for active market. REST fallback if WS stale >3s."""
        if not match.token_a:
            return None
        book = self.ws_prices.get_book(match.token_a)
        if book and book.has_book and (time.time() - book.last_update) < 3.0:
            return book
        # REST fallback
        return self._rest_book_cache.get(match.token_a, book)

    def _get_any_book(self, match: LiveMatch) -> BookState | None:
        """Get any available book — active market first, then series fallback.
        Used for price logging only, not for display or trading."""
        book = self._get_book(match)
        if book and book.has_book:
            return book
        for slot in match.all_markets:
            if slot.market_type == "series":
                book = self.ws_prices.get_book(slot.token_a)
                if book and book.has_book:
                    return book
        return self._get_book(match)

    # ── LLF listener ────────────────────────────────────────────────────

    async def _llf_listener(self, match: LiveMatch):
        url = f"{match.llf_url}?token={PS_KEY}"
        tag = f"[LLF {match.name}]"
        ws = None

        try:
            while self._running:
                try:
                    ws = await websockets.connect(url, ping_interval=20, ping_timeout=30)
                    log.info("%s Connected", tag)
                    match.llf_status = "connected"
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=cfg.LLF_RECV_TIMEOUT)
                        except asyncio.TimeoutError:
                            log.warning("%s Timeout (%ds no data) — reconnecting", tag, cfg.LLF_RECV_TIMEOUT)
                            match.llf_status = "timeout"
                            break
                        try:
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, ValueError):
                            continue

                        mtype = msg.get("type", "")
                        match.llf_last_msg_at = time.time()
                        match.llf_last_msg_type = mtype
                        match.llf_msg_count += 1

                        if mtype == "hello":
                            p = msg.get("payload", {})
                            match.llf_status = f"hello:{p.get('status', '?')}"
                            log.info("%s Hello: status=%s", tag, p.get("status", "?"))
                            if p.get("status") == "closing":
                                match.llf_status = "closing"
                                return
                            continue

                        if mtype == "scoreboard":
                            match.llf_status = "scoreboard"
                        elif mtype == "update":
                            match.llf_status = "streaming"

                        games = []
                        if mtype == "scoreboard":
                            games = msg.get("scoreboard", {}).get("games", [])
                        elif mtype == "update":
                            games = msg.get("payload", {}).get("games", [])

                        if games:
                            match.games = games
                            match.llf_scoreboard_updated_at = time.time()
                            for g in games:
                                await self._process_game_update(match, g)

                except websockets.exceptions.ConnectionClosed as e:
                    code = getattr(e, "code", 0) or 0
                    reason = str(e).lower()
                    if code == 1000 or "finished" in reason or "closing" in reason:
                        match.llf_status = "ended"
                        log.info("%s Match ended", tag)
                        return
                    elif code == 4004 or "unavailable" in reason:
                        match.llf_status = f"not_open(retry {cfg.LLF_NOT_OPEN_DELAY}s)"
                        log.info("%s LLF not open — retrying %ds", tag, cfg.LLF_NOT_OPEN_DELAY)
                        await asyncio.sleep(cfg.LLF_NOT_OPEN_DELAY)
                    else:
                        match.llf_status = f"closed({code})"
                        log.warning("%s Closed (%d) — retrying %ds", tag, code, cfg.LLF_RECONNECT_DELAY)
                        await asyncio.sleep(cfg.LLF_RECONNECT_DELAY)
                except Exception as e:
                    match.llf_status = f"error:{type(e).__name__}"
                    log.warning("%s Error: %s — retrying 10s", tag, e)
                    await asyncio.sleep(10)
                finally:
                    if ws:
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        ws = None
        except asyncio.CancelledError:
            match.llf_status = "cancelled"
            log.info("%s Cancelled — closing WS", tag)
        finally:
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
                log.info("%s WS closed", tag)

    # ── Live game clock + periodic model re-score ───────────────────────

    def _compute_live_game_sec(self, timer_obj: dict) -> int:
        """Current in-game seconds. Advances past the last LLF timer payload
        when the clock is running, so game_minute keeps ticking between
        scoreboard updates."""
        base_t = timer_obj.get("timer", 0) or 0
        if not timer_obj.get("paused", True) and timer_obj.get("issued_at"):
            try:
                dt = datetime.fromisoformat(timer_obj["issued_at"].replace("Z", "+00:00"))
                base_t += (datetime.now(timezone.utc) - dt).total_seconds()
            except Exception:
                pass
        return max(0, int(base_t))

    def _log_match_model_prob(
        self,
        match: "LiveMatch",
        curr_teams: dict[int, dict],
        game_sec: int,
    ) -> None:
        """Score the win-prob model on the current game state and append
        (now, p_a) to match._model_prob_log. De-duped so rapid back-to-back
        calls (LLF update + periodic tick) don't stack near-identical points.
        Passes state_history + champion picks so v2 momentum/comp features
        see the full runtime context; v1 model ignores them via is_v2=False.
        """
        if not match.team_a_id or not match.team_b_id:
            return
        ta_st = curr_teams.get(match.team_a_id)
        tb_st = curr_teams.get(match.team_b_id)
        if not ta_st or not tb_st:
            return
        now = time.time()
        if match._model_prob_log and (now - match._model_prob_log[-1][0]) < cfg.MODEL_RESCORE_DEDUP_SEC:
            return
        try:
            is_a_blue = str(ta_st.get("side", "")).lower().startswith("blu")
            gid = next((g.get("id", 0) for g in match.games
                        if g.get("status") == "running"), 0) or next(iter(match._state_history), 0)
            history = match._state_history.get(gid)
            picks = match._champion_picks.get(gid, {})
            ta_champs = picks.get(match.team_a_id)
            tb_champs = picks.get(match.team_b_id)
            # safe_predict_win_prob handles smoothing internally via
            # MODEL_SMOOTH_WINDOW_SEC — so rescore line, event scoring, and
            # trade-decision impacts all share the same smoothing policy.
            p_a = safe_predict_win_prob(
                game_sec / 60.0,
                _norm_llf(ta_st), _norm_llf(tb_st),
                is_blue=is_a_blue,
                state_history=history, current_ts=now,
                team_champs=ta_champs, opp_champs=tb_champs,
            )
        except Exception as exc:
            log.debug("[MODEL] rescore failed for %s: %s", match.name, exc)
            return
        match._model_prob_log.append((now, round(p_a, 4)))
        if len(match._model_prob_log) > 5000:
            match._model_prob_log = match._model_prob_log[-2500:]

    async def _model_rescore_loop(self):
        """Re-score the win-prob model every MODEL_RESCORE_SEC for each
        running game so P(win) drifts with game_minute even when LLF sends
        no updates (e.g. slow lane phase between kills).
        Time is itself a signal: 5-0 at min 1 is not 5-0 at min 30.
        """
        while self._running:
            await asyncio.sleep(cfg.MODEL_RESCORE_SEC)
            for m in self.matches.values():
                if not m.active or not m.games:
                    continue
                running = next((g for g in m.games if g.get("status") == "running"), None)
                if not running:
                    continue
                timer_obj = running.get("timer", {})
                if timer_obj.get("paused", True):
                    continue
                gid = running.get("id", 0)
                curr = m._prev_teams.get(gid)
                if not curr:
                    continue
                game_sec = self._compute_live_game_sec(timer_obj)
                self._log_match_model_prob(m, curr, game_sec)

    # ── Game state diffing ──────────────────────────────────────────────

    async def _process_game_update(self, match: LiveMatch, game: dict):
        gid = game.get("id", 0)
        pos = game.get("position", 0)
        teams = game.get("teams", [])
        status = game.get("status", "?")
        timer_obj = game.get("timer", {})

        if status == "running" and pos > 0 and pos != match._current_game_num:
            old_gn = match._current_game_num
            match._current_game_num = pos
            new_mkt = match.active_market
            if new_mkt:
                self.ws_prices.subscribe(new_mkt.token_a)
                self.ws_prices.subscribe(new_mkt.token_b)
                self._rebuild_token_map()
                log.info("[MARKET SWITCH] %s: game %d → %d | now=%s | subscribed %s/%s",
                         match.name, old_gn, pos, new_mkt.market_type,
                         new_mkt.token_a[:12], new_mkt.token_b[:12])

        game_sec = self._compute_live_game_sec(timer_obj)

        curr = {t["id"]: {
            "id": t.get("id", 0), "side": t.get("side", "?"),
            "kills": t.get("kills", 0) or 0, "towers": t.get("towers", 0) or 0,
            "drakes": t.get("drakes", 0) or 0, "nashors": t.get("nashors", 0) or 0,
            "inhibitors": t.get("inhibitors", 0) or 0,
        } for t in teams}

        prev = match._prev_teams.get(gid)
        match._prev_teams[gid] = curr

        # Append to state history (for v2 momentum features). Keyed by game_id.
        # Store in blue/red keys (not team_id) so FeatureVectorBuilder can use
        # it directly. team side string "blue"/"red" comes from LLF teams[].
        if match.team_a_id and match.team_b_id:
            ta = curr.get(match.team_a_id)
            tb = curr.get(match.team_b_id)
            if ta and tb:
                ta_blue = str(ta.get("side", "")).lower().startswith("blu")
                blue_st = ta if ta_blue else tb
                red_st = tb if ta_blue else ta
                hist = match._state_history.setdefault(gid, [])
                # De-dup: skip if nothing changed vs last entry (avoids bloat
                # from rapid LLF updates that don't change the scoreboard).
                if not hist or _scoreboard_changed(hist[-1][1], blue_st, red_st):
                    hist.append((time.time(), {"blue": blue_st, "red": red_st}))
                    # Trim: keep last ~15 min at ≤ 1 entry per 5s → cap at 200
                    if len(hist) > 200:
                        match._state_history[gid] = hist[-200:]

        # Populate champion picks on first scoreboard with complete draft.
        if gid not in match._champion_picks:
            draft = (game.get("draft") or {}).get("picks") or []
            if len(draft) >= 10:
                by_team: dict[int, list[str]] = {}
                for p in draft:
                    by_team.setdefault(p.get("team_id", 0), []).append(p.get("champion_slug", ""))
                # Only commit if both teams have 5 picks each
                if (len(by_team.get(match.team_a_id, [])) == 5
                        and len(by_team.get(match.team_b_id, [])) == 5):
                    match._champion_picks[gid] = by_team
                    log.info("[DRAFT] %s G%d: A=%s B=%s", match.name, pos,
                             by_team[match.team_a_id], by_team[match.team_b_id])

        if status == "running":
            self._log_match_model_prob(match, curr, game_sec)

        if prev is None:
            # Only log INIT for the currently running game, skip finished ones
            if status != "running":
                return
            t_strs = []
            for t in teams:
                side = (t.get("side") or "?")[:3]
                tid = t.get("id", 0)
                team_name = match.team_a if tid == match.team_a_id else match.team_b
                t_strs.append(f"{team_name}({side}): K{t.get('kills',0)} T{t.get('towers',0)} D{t.get('drakes',0)} B{t.get('nashors',0)} I{t.get('inhibitors',0)}")
            log.info("[LLF INIT] %s G%d [%d:%02d] %s",
                     match.name, pos, game_sec // 60, game_sec % 60, " | ".join(t_strs))
            book = self._get_book(match)
            self._recent_events.append({
                "ts": time.time(),
                "time": datetime.now().strftime("%H:%M:%S.") + f"{time.time() % 1:.3f}"[2:],
                "match": match.name, "match_id": match.ps_match_id,
                "etype": "init", "team": "", "game": pos,
                "clock": f"{game_sec // 60}:{game_sec % 60:02d}",
                "desc": " | ".join(t_strs),
                "action": "INIT", "signal_dir": None, "signal_size": None,
                "signal_reason": None, "signal_impact": None, "signal_confidence": None,
                "mid": book.mid if book and book.has_book else 0,
                "bid": 0, "ask": 0, "spread": 0, "buy_price_a": 0, "buy_price_b": 0,
                "recent_move_2s": 0, "holding": None, "market_type": "",
                "book_snapshot": {"bids": [], "asks": []},
            })
            return

        events: list[LolEvent] = []
        for tid, stats in curr.items():
            old = prev.get(tid)
            if not old:
                continue
            side = (stats.get("side") or "?")[:3].upper()
            for key, etype in [
                ("kills", EventType.KILL), ("towers", EventType.TOWER),
                ("drakes", EventType.DRAKE), ("nashors", EventType.BARON),
                ("inhibitors", EventType.INHIBITOR),
            ]:
                ov = old.get(key, 0)
                nv = stats.get(key, 0)
                if nv != ov:
                    events.append(LolEvent(
                        ts=time.time(), etype=etype, team_id=tid,
                        side=side, delta=nv - ov, game_position=pos,
                        game_timer_sec=game_sec, new_value=nv, old_value=ov,
                    ))

        old_status = match._prev_status.get(gid)
        match._prev_status[gid] = status
        if old_status and old_status != status:
            events.append(LolEvent(
                ts=time.time(), etype=EventType.STATUS, team_id=0,
                side="", delta=0, game_position=pos,
                game_timer_sec=game_sec, new_value=0, old_value=0,
            ))

        for ev in events:
            await self._process_event(match, ev, prev_teams=prev)

    # ── Signal processing ───────────────────────────────────────────────

    async def _process_event(self, match: LiveMatch, event: LolEvent,
                             prev_teams: dict | None = None):
        if not match.token_a or not match.signal_model:
            return

        book = self._get_book(match)
        if not book or not book.has_book:
            return

        mid_a = book.mid
        bid_a = book.best_bid
        ask_a = book.best_ask
        spread = book.spread

        depth_usd, _ = book.available_depth("buy", max_slippage_c=0.03)

        recent_move = book.recent_move(cfg.PRE_EVENT_WINDOW_SEC)

        # Look up v2 context for this match+game so momentum + comp features
        # can evaluate. signal_model passes these into event_impact.
        _gid_for_sig = next((g.get("id", 0) for g in match.games
                             if g.get("status") == "running"), 0)
        _sig_history = match._state_history.get(_gid_for_sig)
        _sig_picks = match._champion_picks.get(_gid_for_sig, {})
        _sig_opp_id = match.team_b_id if event.team_id == match.team_a_id else match.team_a_id

        signal, reason = match.signal_model.on_event(
            event=event,
            mid_a=mid_a,
            bid_a=bid_a,
            ask_a=ask_a,
            spread=spread,
            prev_teams=prev_teams,
            state_history=_sig_history,
            acting_champs=_sig_picks.get(event.team_id),
            opp_champs=_sig_picks.get(_sig_opp_id),
        )

        team_name = match.team_a if event.team_id == match.team_a_id else match.team_b
        game_min = event.game_timer_sec // 60
        game_sec = event.game_timer_sec % 60

        now_ts = time.time()
        now_ms = datetime.now().strftime("%H:%M:%S.") + f"{now_ts % 1:.3f}"[2:]

        buy_price_a = round(ask_a, 4)
        buy_price_b = round(1.0 - bid_a, 4) if bid_a > 0 else 0

        # Score the win-prob model for EVERY tradeable event — including ones
        # that will get gated on spread/price-band/near-resolved — so the
        # dashboard can show the model's before→after delta regardless of
        # whether the bot traded. Time + state both move the line, so this
        # is informative even when no BUY fires.
        model_view: dict | None = None
        if event.etype in _STAT_KEY_MAP and prev_teams:
            acting_before = prev_teams.get(event.team_id)
            opp_id = match.team_b_id if event.team_id == match.team_a_id else match.team_a_id
            opp_before = prev_teams.get(opp_id)
            if acting_before and opp_before:
                stat_key = _STAT_KEY_MAP[event.etype]
                acting_after = {**acting_before, stat_key: event.new_value}
                opp_after = {**opp_before}
                is_blue = str(acting_before.get("side", "")).lower().startswith("blu")
                try:
                    gid_for_event = next((g.get("id", 0) for g in match.games
                                          if g.get("status") == "running"), 0)
                    history = match._state_history.get(gid_for_event)
                    picks = match._champion_picks.get(gid_for_event, {})
                    acting_champs = picks.get(event.team_id)
                    opp_champs = picks.get(opp_id)
                    impact, _p_b, _p_a = safe_predict_impact_from_llf(
                        game_minute=event.game_timer_sec / 60.0,
                        team_before=acting_before, team_after=acting_after,
                        opp_before=opp_before, opp_after=opp_after,
                        is_blue=is_blue,
                        state_history=history, current_ts=event.ts,
                        team_champs=acting_champs, opp_champs=opp_champs,
                    )
                    model_dir = "buy_a" if event.team_id == match.team_a_id else "buy_b"
                    pre_event_mid_a = mid_a - recent_move
                    pre_event_mid = pre_event_mid_a if model_dir == "buy_a" else 1.0 - pre_event_mid_a
                    p_fair_view = max(0.02, min(0.98, pre_event_mid + impact))
                    model_view = {
                        "model_dir": model_dir,
                        "signal_impact": round(impact, 4),
                        "pre_event_mid": round(pre_event_mid, 4),
                        "p_fair": round(p_fair_view, 4),
                    }
                except Exception as exc:
                    log.debug("[MODEL] event-scoring failed for %s %s: %s",
                              match.name, event.etype.value, exc)

        active_mkt_info = match.active_market
        mkt_type = active_mkt_info.market_type if active_mkt_info else "none"

        snap_bids = sorted(
            [{"p": float(l["price"]), "s": float(l.get("size", 0))} for l in book.raw_bids],
            key=lambda x: -x["p"]
        )[:6]
        snap_asks = sorted(
            [{"p": float(l["price"]), "s": float(l.get("size", 0))} for l in book.raw_asks],
            key=lambda x: x["p"]
        )[:6]
        book_snap = {"bids": snap_bids, "asks": snap_asks}

        ev_record = {
            "ts": now_ts,
            "time": now_ms,
            "match": match.name,
            "match_id": match.ps_match_id,
            "etype": event.etype.value,
            "team": team_name,
            "game": event.game_position,
            "clock": f"{game_min}:{game_sec:02d}",
            "desc": f"{team_name} ({event.side}): {event.old_value}→{event.new_value} (+{event.delta})",
            "delta": int(event.delta),
            "new_value": int(event.new_value),
            "old_value": int(event.old_value),
            "action": reason if signal is None else "TRADE",
            "signal_dir": signal.direction if signal else None,
            "signal_size": None,
            "signal_reason": signal.reason if signal else None,
            "signal_impact": signal.expected_impact if signal else (model_view["signal_impact"] if model_view else None),
            "signal_confidence": signal.confidence if signal else None,
            "model_dir": model_view["model_dir"] if model_view else None,
            "pre_event_mid": model_view["pre_event_mid"] if model_view else None,
            "p_fair": model_view["p_fair"] if model_view else None,
            "mid": round(mid_a, 4),
            "bid": round(bid_a, 4),
            "ask": round(ask_a, 4),
            "spread": round(spread, 4),
            "buy_price_a": buy_price_a,
            "buy_price_b": buy_price_b,
            "recent_move_2s": round(recent_move, 4),
            "holding": None,
            "market_type": mkt_type,
            "book_snapshot": book_snap,
        }
        self._recent_events.append(ev_record)
        # Keep a deep buffer so users can scroll back through history for a
        # live match without losing context. ~1500 events × ~1KB = 1.5 MB max
        # in memory — trivial. Per-match view still filters via match_id.
        if len(self._recent_events) > 1500:
            self._recent_events = self._recent_events[-1000:]

        if signal is None:
            if reason not in ("TOWER_SKIP", "STATUS_SKIP", "NOT_TRADEABLE_status"):
                log.info("[SKIP] G%d [%d:%02d] %s %s(%s) %d→%d — %s | mid=%.1fc spread=%.1fc",
                         event.game_position, game_min, game_sec,
                         event.etype.value.upper(), team_name,
                         event.side, event.old_value, event.new_value,
                         reason, mid_a * 100, spread * 100)
            return

        # ── Edge calculation: market prior + model delta ──────────────
        model_impact = signal.expected_impact
        token_id = match.token_a if signal.direction == "buy_a" else match.token_b
        buy_price = ask_a if signal.direction == "buy_a" else round(1.0 - bid_a, 2)

        pre_event_mid_a = mid_a - recent_move
        if signal.direction == "buy_a":
            pre_event_mid = pre_event_mid_a
        else:
            pre_event_mid = 1.0 - pre_event_mid_a

        p_fair = max(0.02, min(0.98, pre_event_mid + model_impact))
        ask_eff = buy_price * (1.0 + cfg.TAKER_FEE)
        edge = p_fair - ask_eff

        ev_record["p_fair"] = round(p_fair, 4)
        ev_record["edge"] = round(edge, 4)
        ev_record["pre_event_mid"] = round(pre_event_mid, 4)

        if edge < cfg.MIN_EDGE:
            reason_str = f"LOW_EDGE_{edge:.4f}<{cfg.MIN_EDGE}"
            log.info("[SKIP] G%d [%d:%02d] %s %s — %s | impact=%.3f p_fair=%.3f ask_eff=%.3f edge=%.4f",
                     event.game_position, game_min, game_sec,
                     event.etype.value.upper(), team_name,
                     reason_str, model_impact, p_fair, ask_eff, edge)
            ev_record["action"] = reason_str
            return

        # ── Kelly sizing ──────────────────────────────────────────────
        if ask_eff >= 1.0 or ask_eff <= 0.0:
            return
        b = (1.0 / ask_eff) - 1.0
        q = 1.0 - p_fair
        f_star = (p_fair * b - q) / b if b > 0 else 0.0
        f_star = max(0.0, f_star)

        size_usd = cfg.BANKROLL * f_star * cfg.KELLY_FRACTION
        depth_cap = depth_usd * cfg.MAX_BOOK_PARTICIPATION
        size_usd = max(cfg.MIN_BET, min(size_usd, cfg.MAX_SINGLE_BET, depth_cap))

        if size_usd < cfg.MIN_BET:
            ev_record["action"] = f"SIZE_TOO_SMALL_{size_usd:.2f}"
            return

        signal.size_usd = round(size_usd, 2)
        ev_record["signal_size"] = signal.size_usd

        # ── Slippage budget: proportional to edge, capped at 2c ──────
        slippage = min(edge * 0.25, 0.02)
        limit_price = buy_price + slippage

        active_mkt = match.active_market
        mkt_label = active_mkt.market_type if active_mkt else "none"
        log.info("[TRADE] G%d [%d:%02d] %s %s — %s dir=%s $%.0f | impact=%.3f p_fair=%.3f edge=%.3f kelly=%.3f market=%s",
                 event.game_position, game_min, game_sec,
                 event.etype.value.upper(), team_name,
                 signal.reason, signal.direction, signal.size_usd,
                 model_impact, p_fair, edge, f_star, mkt_label)

        ev_record["attempt_ref_px"] = round(buy_price, 4)
        ev_record["attempt_limit_price"] = round(limit_price, 4)

        # ── Tape: record book around this signal for post-analysis ────
        before_ticks = [t for t in book.tick_buffer if t["ts"] >= now_ts - 3.0]
        game_state = None
        if prev_teams:
            game_state = {}
            for tid, st in prev_teams.items():
                tname = match.team_a if tid == match.team_a_id else match.team_b
                game_state[tname] = {k: v for k, v in st.items() if k != "id"}
        asyncio.create_task(self._record_trade_tape(
            match=match, event=event, signal=signal, book=book,
            ev_record=ev_record, before_ticks=before_ticks,
            signal_ts=now_ts, f_star=f_star,
            pre_event_mid=pre_event_mid, p_fair=p_fair, edge=edge,
            game_state=game_state,
        ))

        if depth_usd < cfg.MIN_BOOK_DEPTH:
            log.info("[GATE] Thin book: $%.0f depth < $%d min", depth_usd, cfg.MIN_BOOK_DEPTH)
            ev_record["action"] = "GATED"
            ev_record["trade_exec"] = "gated"
            ev_record["gate_reason"] = f"THIN_BOOK_${depth_usd:.0f}<${cfg.MIN_BOOK_DEPTH}"
            ev_record["exec_story"] = f"Signal fired but book too thin: ${depth_usd:.0f} within 3¢ of best ask (need ${cfg.MIN_BOOK_DEPTH})."
            return

        ok, gate_reason = self.risk.check_entry(token_id, match.ps_match_id, signal.size_usd)
        if not ok:
            log.info("[GATE] %s", gate_reason)
            ev_record["action"] = "GATED"
            ev_record["trade_exec"] = "gated"
            ev_record["gate_reason"] = gate_reason
            ev_record["exec_story"] = f"Would have bought but risk gate blocked:\n{gate_reason}"
            return

        await self._execute_entry(match, signal, token_id, buy_price, event, ev_record,
                                  limit_price=limit_price)

    # ── Trade tape recorder ────────────────────────────────────────────

    @staticmethod
    def _snapshot_book(book: BookState) -> dict:
        bids = [{"p": round(float(l["price"]), 4), "s": round(float(l.get("size", 0)), 2)}
                for l in book.raw_bids[:5]]
        asks = [{"p": round(float(l["price"]), 4), "s": round(float(l.get("size", 0)), 2)}
                for l in book.raw_asks[:5]]
        depth_buy, _ = book.available_depth("buy", max_slippage_c=0.03)
        depth_sell, _ = book.available_depth("sell", max_slippage_c=0.03)
        return {
            "mid": round(book.mid, 4),
            "bid": round(book.best_bid, 4),
            "ask": round(book.best_ask, 4),
            "spread": round(book.spread, 4),
            "depth_buy": round(depth_buy, 1),
            "depth_sell": round(depth_sell, 1),
            "bids": bids,
            "asks": asks,
        }

    async def _record_trade_tape(
        self, match: LiveMatch, event: LolEvent, signal: Signal,
        book: BookState, ev_record: dict,
        before_ticks: list[dict], signal_ts: float,
        f_star: float, pre_event_mid: float, p_fair: float, edge: float,
        game_state: dict | None = None,
    ):
        try:
            # Milestone offsets surfaced to the dashboard. ev_record is shared
            # by reference with _recent_events, so every milestone update
            # becomes visible on the next /api/state poll.
            MILESTONES = [5, 10, 30, 60]
            milestone_idx = 0
            # Structured trail for future UI work.
            post_trade_prices: list[dict] = []
            ev_record["post_trade_prices"] = post_trade_prices

            direction = signal.direction  # "buy_a" or "buy_b"

            after_ticks = []
            for i in range(61):
                if not book.has_book:
                    break
                snap = self._snapshot_book(book)
                snap["ts_offset"] = round(time.time() - signal_ts, 2)
                after_ticks.append(snap)

                # Record every milestone we've crossed. Usually exactly one
                # crosses per iteration, but fast-forward if a pause dropped us.
                while milestone_idx < len(MILESTONES) and snap["ts_offset"] >= MILESTONES[milestone_idx]:
                    off = MILESTONES[milestone_idx]
                    mid = float(snap["mid"])
                    our_px = mid if direction == "buy_a" else (1.0 - mid)
                    # Re-read fill_price each milestone — fill confirmation is
                    # async and may land AFTER the tape task starts. If it
                    # still isn't set by this milestone, record delta_c=None
                    # so UI shows "—" instead of a misleading 0.0c.
                    fill_px_now = float(ev_record.get("fill_price") or 0)
                    if fill_px_now > 0:
                        delta_c = round((our_px - fill_px_now) * 100, 2)
                    else:
                        delta_c = None
                    post_trade_prices.append({
                        "offset_sec": off,
                        "mid": round(mid, 4),
                        "our_px": round(our_px, 4),
                        "delta_c": delta_c,
                    })
                    story = ev_record.get("exec_story") or ""
                    if delta_c is None:
                        ev_record["exec_story"] = story + (
                            f"\n+{off}s post-fill: {direction.upper()} mark {our_px*100:.1f}¢ (fill px unknown)"
                        )
                    else:
                        sign = "+" if delta_c >= 0 else ""
                        ev_record["exec_story"] = story + (
                            f"\n+{off}s post-fill: {direction.upper()} mark {our_px*100:.1f}¢ "
                            f"({sign}{delta_c:.1f}¢ vs fill @ {fill_px_now*100:.1f}¢)"
                        )
                    milestone_idx += 1

                if i < 60:
                    await asyncio.sleep(1.0)

            team_name = match.team_a if event.team_id == match.team_a_id else match.team_b
            tape = {
                "signal_ts": signal_ts,
                "signal_time": datetime.fromtimestamp(signal_ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "match": match.name,
                "match_id": match.ps_match_id,
                "team_a": match.team_a,
                "team_b": match.team_b,
                "game_state": game_state,
                "event": {
                    "type": event.etype.value,
                    "team": team_name,
                    "clock": f"{event.game_timer_sec // 60}:{event.game_timer_sec % 60:02d}",
                    "delta": event.delta,
                    "old_value": event.old_value,
                    "new_value": event.new_value,
                },
                "signal": {
                    "direction": signal.direction,
                    "size_usd": signal.size_usd,
                    "model_impact": signal.expected_impact,
                    "p_fair": round(p_fair, 4),
                    "edge": round(edge, 4),
                    "pre_event_mid": round(pre_event_mid, 4),
                    "reason": signal.reason,
                    "kelly_f": round(f_star, 4),
                },
                "execution": {
                    "action": ev_record.get("action"),
                    "buy_price": ev_record.get("attempt_ref_px"),
                    "limit_price": ev_record.get("attempt_limit_price"),
                    "trade_exec": ev_record.get("trade_exec"),
                    "fill_price": ev_record.get("fill_price"),
                    "fill_shares": ev_record.get("fill_reported_shares"),
                    "gate_reason": ev_record.get("gate_reason"),
                },
                "ticks_before": [
                    {
                        "ts_offset": round(t["ts"] - signal_ts, 3),
                        "mid": round(t["mid"], 4),
                        "bid": round(t["bid"], 4),
                        "ask": round(t["ask"], 4),
                        "spread": round(t["spread"], 4),
                    }
                    for t in before_ticks
                ],
                "ticks_after": after_ticks,
            }

            outdir = Path(__file__).resolve().parent.parent / "logs" / "trade_tapes"
            outdir.mkdir(parents=True, exist_ok=True)
            dt = datetime.fromtimestamp(signal_ts)
            safe_name = match.name.replace(" ", "_").replace("/", "-")
            fname = f"{dt.strftime('%Y%m%d_%H%M%S')}_{safe_name}_{event.etype.value}.json"
            with open(outdir / fname, "w") as f:
                json.dump(tape, f, indent=2, default=str)
            log.info("[TAPE] Saved %s (%d before + %d after ticks)", fname, len(before_ticks), len(after_ticks))
        except Exception as exc:
            log.warning("[TAPE] Recording failed: %s", exc)

    # ── Entry execution ─────────────────────────────────────────────────

    async def _execute_entry(
        self, match: LiveMatch, signal: Signal,
        token_id: str, buy_price: float, event: LolEvent,
        ev_record: dict, limit_price: float = 0.0,
    ):
        if limit_price <= 0:
            limit_price = buy_price + 0.01

        shares = round(signal.size_usd / buy_price, 1) if buy_price > 0 else 0
        if shares < 1:
            log.info("[SKIP] shares < 1 at price %.3f", buy_price)
            ev_record["action"] = "SKIP_SIZE"
            ev_record["trade_exec"] = "shares_skip"
            ev_record["exec_story"] = f"Order not sent: size < 1 share at {buy_price:.3f} (need higher $ or price)."
            return
        story: list[str] = [
            f"1) Entry intent: {signal.direction} ~{shares:.1f} sh @ ≤{limit_price:.3f} (≈${signal.size_usd:.2f} notional).",
        ]

        if self.dry_run:
            log.info("[DRY] BUY %s %.1f shares @ %.3f ($%.2f) — %s",
                     signal.direction, shares, buy_price, signal.size_usd, signal.reason)
            pos = Position(
                match_id=match.ps_match_id, match_name=match.name,
                direction=signal.direction, token_id=token_id,
                entry_price=buy_price, size=shares,
                cost_usd=round(buy_price * shares, 2),
                entry_time=time.time(),
                entry_game_min=event.game_timer_sec // 60,
                signal_reason=signal.reason, neg_risk=match.neg_risk,
            )
            self.risk.record_entry(pos)
            ev_record["trade_exec"] = "dry_run"
            story.append("2) DRY RUN — no CLOB call; paper position only.")
            story.append("3) Polymarket will show nothing — this is simulated.")
            ev_record["exec_story"] = "\n".join(story)
            return

        try:
            bal_before = await asyncio.get_event_loop().run_in_executor(
                self._executor, lambda: poly_client.get_token_balance(token_id),
            )

            placed_ts = int(time.time())
            resp = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                lambda: poly_client.buy_fak(token_id, limit_price, shares, match.neg_risk),
            )
            order_id = resp.get("orderID", "")
            status = str(resp.get("status", ""))
            if not order_id:
                log.error("[ENTRY] No orderID returned: %s", resp)
                ev_record["action"] = "ORDER_FAIL"
                ev_record["trade_exec"] = "no_order_id"
                story.append("2) buy_fak returned no orderID — request may have been rejected.")
                ev_record["exec_story"] = "\n".join(story)
                return

            story.append(f"2) buy_fak accepted → order_id={order_id[:20]}… status={status!r}.")
            ev_record["clob_order_id"] = order_id

            # Phase 1: quick get_trades polls (~3s) — catches instant fills with exact data
            fill = None
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(1.0)
                fill = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    lambda oid=order_id, ts=placed_ts: poly_client.verify_buy_fill(oid, ts),
                )
                if fill:
                    log.info("[ENTRY] Fill found via get_trades attempt %d", attempt + 1)
                    break

            # Phase 2: balance-delta fallback — on-chain balance updates faster than get_trades
            fill_via_balance = False
            if not fill:
                for bal_attempt in range(3):
                    await asyncio.sleep(2.0)
                    bal_after = await asyncio.get_event_loop().run_in_executor(
                        self._executor, lambda: poly_client.get_token_balance(token_id),
                    )
                    delta = round(bal_after - bal_before, 2)
                    log.info("[ENTRY] Balance check %d: before=%.2f after=%.2f delta=%.2f",
                             bal_attempt + 1, bal_before, bal_after, delta)
                    if delta >= 0.5:
                        fill_via_balance = True
                        fill = {"_balance_delta": True}
                        break
                    fill = await asyncio.get_event_loop().run_in_executor(
                        self._executor,
                        lambda oid=order_id, ts=placed_ts: poly_client.verify_buy_fill(oid, ts),
                    )
                    if fill:
                        log.info("[ENTRY] Fill found via get_trades (late) attempt %d", bal_attempt + 1)
                        break

            if not fill:
                log.warning("[ENTRY] No fill confirmed (~9s) for order %s", order_id[:20])
                story.append("3) Polled get_trades + balance delta (~9s): no confirmation.")
                story.append("4) **Did not open a position** — fill not confirmed (check Polymarket Activity).")
                ev_record["trade_exec"] = "no_fill_confirmed"
                ev_record["exec_story"] = "\n".join(story)
                return

            if fill_via_balance:
                fill_size = delta
                fill_price = buy_price
                story.append(f"3) Fill confirmed via balance delta: +{delta:.2f} sh (before={bal_before:.2f} after={bal_after:.2f}). Price ≈ {fill_price:.4f}.")
                log.info("[FILL] Confirmed via balance delta: +%.2f sh @ ~%.3f", fill_size, fill_price)
            else:
                fill_price = float(fill.get("price", buy_price) or buy_price)
                fill_size, scale_note = _coerce_trade_fill_shares(fill, shares)
                if scale_note:
                    story.append(f"3) Raw trade row parsed ({scale_note}): {fill_size:.2f} sh @ {fill_price:.4f}.")
                else:
                    story.append(f"3) Trade matched: {fill_size:.2f} sh @ {fill_price:.4f}.")

            # Persist on ev_record so downstream (trade tape + dashboard) can
            # compute post-fill Δc vs the actual fill, not vs a zero default.
            ev_record["fill_price"] = round(fill_price, 4)
            ev_record["fill_reported_shares"] = round(fill_size, 4)

            if not _fill_size_plausible(fill_size, shares):
                log.error("[ENTRY] Rejecting fill: size=%.4f vs expected ~%.1f", fill_size, shares)
                story.append(f"4) **Rejected** — reported size looks wrong vs ~{shares:.1f} sh (bad scaling or partial fill data).")
                story.append("5) **No position opened** — reconcile manually on Polymarket if you see inventory.")
                ev_record["trade_exec"] = "fill_rejected"
                ev_record["fill_reported_shares"] = round(fill_size, 4)
                ev_record["exec_story"] = "\n".join(story)
                return

            log.info("[FILL] BUY confirmed: %.1f shares @ %.3f", fill_size, fill_price)
            story.append("4) Position opened in bot — exit loop will place sell after hold period.")

            pos = Position(
                match_id=match.ps_match_id, match_name=match.name,
                direction=signal.direction, token_id=token_id,
                entry_price=fill_price, size=fill_size,
                cost_usd=round(fill_price * fill_size, 2),
                entry_time=time.time(),
                entry_game_min=event.game_timer_sec // 60,
                signal_reason=signal.reason, neg_risk=match.neg_risk,
            )
            self.risk.record_entry(pos)
            ev_record["trade_exec"] = "polymarket_ok"
            ev_record["exec_story"] = "\n".join(story)

        except Exception as e:
            log.error("[ENTRY] Order failed: %s", e)
            ev_record["action"] = "ORDER_FAIL"
            ev_record["trade_exec"] = "order_error"
            ev_record["order_error"] = str(e)[:200]
            story.append(f"2) Exception before/while trading: {ev_record['order_error']}")
            ev_record["exec_story"] = "\n".join(story)

    # ── Resolution loop (hold to resolution — no active selling) ───────

    async def _exit_loop(self):
        """Hold all positions until market resolution. No GTC/FAK sells.
        Polymarket redeems winning shares at $1.00 automatically.
        Resolution is detected by _check_finished_matches → risk.resolve_position.
        This loop just logs position status periodically for debugging.
        """
        while self._running:
            await asyncio.sleep(10)
            open_pos = self.risk.open_positions
            if not open_pos:
                continue
            for pos in open_pos:
                match = self.matches.get(pos.match_id)
                book = self._get_book(match) if match else None
                current = book.mid if book and book.has_book else 0
                age = time.time() - pos.entry_time
                if pos.direction == "buy_a":
                    unrealized = (current - pos.entry_price) * pos.size if current > 0 else 0
                else:
                    unrealized = ((1 - current) - pos.entry_price) * pos.size if current > 0 else 0
                if not pos.exit_story:
                    pos.exit_story.append("Strategy: HOLD TO RESOLUTION — no active sell orders.")
                if age > 60 and int(age) % 60 < 10:
                    log.info("[HOLD] %s %s %.1f sh @ %.3f → now %.3f (unreal $%.2f, %ds)",
                             pos.direction, pos.match_name, pos.size,
                             pos.entry_price, current, unrealized, int(age))


    # ── Dashboard API ──────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Serialize full state for dashboard /api/state."""
        now = time.time()
        matches = {}
        for mid, m in self.matches.items():
            book = self._get_book(m)
            match_events = [e for e in self._recent_events if e.get("match_id") == mid]
            match_trades = [t.__dict__ if hasattr(t, '__dict__') else t
                            for t in self.risk.trades if hasattr(t, 'match_name') and m.name in str(getattr(t, 'match_name', ''))]
            matches[str(mid)] = {
                "match_id": mid,
                "name": m.name,
                "team_a": m.team_a,
                "team_b": m.team_b,
                "team_a_id": m.team_a_id,
                "team_b_id": m.team_b_id,
                "volume": m.gamma.get("volume", 0) if m.gamma else 0,
                "liquidity": m.gamma.get("liquidity", 0) if m.gamma else 0,
                "has_market": bool(m.all_markets),
                "market_question": m.market_question,
                "active_market_type": m.active_market.market_type if m.active_market else "",
                "current_game_num": m._current_game_num,
                "total_markets": len(m.all_markets),
                "token_a": m.token_a,
                "token_b": m.token_b,
                "games": m.games,
                "series_score": {str(k): v for k, v in m.series_score.items()},
                "mid": book.mid if book and book.has_book else 0,
                "bid": book.best_bid if book else 0,
                "ask": book.best_ask if book else 0,
                "spread": book.spread if book else 1,
                "has_book": book.has_book if book else False,
                "book_bids": sorted(
                    [{"p": float(l["price"]), "s": float(l.get("size", 0))} for l in (book.raw_bids[:20] if book else [])],
                    key=lambda x: -x["p"]
                )[:8],
                "book_asks": sorted(
                    [{"p": float(l["price"]), "s": float(l.get("size", 0))} for l in (book.raw_asks[:20] if book else [])],
                    key=lambda x: x["p"]
                )[:8],
                "price_history": m._price_log[-1000:],
                "model_prob_history": m._model_prob_log[-1000:],
                "active": m.active,
                "finished_at": m.finished_at,
                "league": m.league,
                "status": m.status,
                "event_count": len(match_events),
                "match_events": match_events[-200:],
                "llf_connected": m.ps_match_id in self._llf_tasks and not self._llf_tasks[m.ps_match_id].done() if hasattr(self, '_llf_tasks') else False,
                "llf_status": m.llf_status,
                "llf_last_msg_age": round(now - m.llf_last_msg_at, 1) if m.llf_last_msg_at > 0 else -1,
                "llf_last_msg_type": m.llf_last_msg_type,
                "llf_msg_count": m.llf_msg_count,
                "llf_scoreboard_updated_at": m.llf_scoreboard_updated_at,
                "gamma": m.gamma,
            }

        positions = []
        for p in self.risk.positions:
            book = None
            m = self.matches.get(p.match_id)
            if m:
                book = self._get_book(m)
            current_mid = book.mid if book and book.has_book else 0
            if p.direction == "buy_a":
                current_price = book.best_bid if book else 0
            else:
                current_price = round(1.0 - (book.best_ask if book else 1), 2)
            unrealized = (current_price - p.entry_price) * p.size if current_price > 0 else 0

            positions.append({
                "match_id": p.match_id,
                "match_name": p.match_name,
                "direction": p.direction,
                "entry_price": p.entry_price,
                "size": p.size,
                "cost_usd": p.cost_usd,
                "current_price": current_price,
                "unrealized_pnl": round(unrealized, 4),
                "age_sec": round(now - p.entry_time, 1),
                "signal_reason": p.signal_reason,
                "closed": p.closed,
                "exit_pnl": p.exit_pnl,
                "sell_order_id": p.sell_order_id,
                "exit_story": "\n".join(p.exit_story) if p.exit_story else None,
            })

        trades = []
        for t in self.risk.trades[-50:]:
            trades.append({
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

        return {
            "ts": now,
            "dry_run": self.dry_run,
            "uptime_sec": round(now - self._start_time, 0),
            "capital": self.risk.capital,
            "bankroll": self.risk.bankroll,
            "daily_pnl": round(self.risk._daily_pnl, 2),
            "total_trades": len(self.risk.trades),
            "win_rate": sum(1 for t in self.risk.trades if t.pnl > 0) / max(len(self.risk.trades), 1),
            "exposure": round(self.risk.total_exposure, 2),
            "circuit_active": False,
            "circuit_seconds_left": 0,
            "circuit_reason": "",
            "consecutive_losses": 0,
            "poly_ws": self.ws_prices.health(),
            "llf_ws": {
                "active": sum(1 for t in getattr(self, '_llf_tasks', {}).values() if not t.done()),
                "max": 3,
                "matches": [
                    self.matches[mid].name
                    for mid in getattr(self, '_llf_tasks', {})
                    if not getattr(self, '_llf_tasks', {})[mid].done() and mid in self.matches
                ],
            },
            "matches": matches,
            "positions": positions,
            "trades": trades,
            "events": self._recent_events[-100:],
        }


# ── Dashboard HTTP server ───────────────────────────────────────────────

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8422"))

_trader_ref: LoLTrader | None = None


_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


async def _handle_state(request):
    if _trader_ref is None:
        return web.json_response({"error": "not ready"}, status=503, headers=_CORS)
    return web.json_response(_trader_ref.get_state(), headers=_CORS)


async def _handle_debug(request):
    if _trader_ref is None:
        return web.Response(text="not ready", status=503, headers=_CORS)
    try:
        from lol_debug import dump
        text = dump(f"http://127.0.0.1:{DASHBOARD_PORT}")
        return web.Response(text=text, content_type="text/plain", headers=_CORS)
    except Exception as e:
        return web.Response(text=f"debug error: {e}", status=500, headers=_CORS)


async def _handle_options(request):
    return web.Response(status=200, headers=_CORS)


async def _handle_index(request):
    dist_dir = Path(__file__).resolve().parent.parent / "dashboard" / "dist"
    index = dist_dir / "index.html"
    if index.exists():
        return web.FileResponse(index, headers=_CORS)
    return web.Response(text="Dashboard not built. Run: cd dashboard && npm run build", status=404, headers=_CORS)


async def start_dashboard(trader: LoLTrader):
    global _trader_ref
    _trader_ref = trader

    from aiohttp import web as aio_web
    app = aio_web.Application()
    app.router.add_get("/api/state", _handle_state)
    app.router.add_get("/api/debug", _handle_debug)
    app.router.add_route("OPTIONS", "/api/{path:.*}", _handle_options)

    dist_dir = Path(__file__).resolve().parent.parent / "dashboard" / "dist"
    if dist_dir.exists():
        app.router.add_get("/", _handle_index)
        app.router.add_static("/assets", dist_dir / "assets", show_index=False)
        log.info("Serving dashboard from %s", dist_dir)

    runner = aio_web.AppRunner(app)
    await runner.setup()
    site = aio_web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT)
    await site.start()
    log.info("Dashboard: http://0.0.0.0:%d | API: /api/state | /api/debug", DASHBOARD_PORT)


# ── Main ────────────────────────────────────────────────────────────────

async def main():
    import argparse
    p = argparse.ArgumentParser(description="LoL Polymarket Trading Bot")
    p.add_argument("--live", action="store_true", help="Enable live trading (default: dry run)")
    p.add_argument("--bankroll", type=float, default=500.0)
    args = p.parse_args()

    if not PS_KEY:
        print("PANDASCORE_API_KEY not set in .env")
        return

    trader = LoLTrader(dry_run=not args.live, bankroll=args.bankroll)
    await start_dashboard(trader)
    await trader.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown.")
