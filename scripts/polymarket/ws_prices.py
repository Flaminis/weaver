"""
Polymarket Market WebSocket — real-time CLOB price streaming.
Adapted from Oracle-Dota's ws_price_stream.py.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import websockets

try:
    import orjson
    def _loads(s): return orjson.loads(s)
    def _dumps(d): return orjson.dumps(d)
except ImportError:
    import json
    def _loads(s): return json.loads(s)
    def _dumps(d): return json.dumps(d)

from polymarket.logger import get_logger

log = get_logger("poly.ws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_DELAY = 2.0
MAX_RECONNECT_DELAY = 30.0
STALE_DATA_TIMEOUT = 60  # Force reconnect if no message for 60s
WATCHDOG_INTERVAL = 15   # Check every 15s


@dataclass
class BookState:
    token_id: str
    best_bid: float = 0.0
    best_ask: float = 1.0
    spread: float = 1.0
    mid: float = 0.5
    last_trade_price: float = 0.5
    last_trade_side: str = ""
    last_update: float = 0.0
    last_trade_ts: float = 0.0
    has_book: bool = False
    raw_bids: list = field(default_factory=list)
    raw_asks: list = field(default_factory=list)
    price_history: list[tuple[float, float]] = field(default_factory=list)

    def update_from_book(self, bids: list, asks: list):
        sorted_bids = sorted(bids, key=lambda x: -float(x["price"]))
        sorted_asks = sorted(asks, key=lambda x: float(x["price"]))
        self.best_bid = float(sorted_bids[0]["price"]) if sorted_bids else 0.0
        self.best_ask = float(sorted_asks[0]["price"]) if sorted_asks else 1.0
        self.spread = round(self.best_ask - self.best_bid, 4)
        self.has_book = bool(sorted_bids and sorted_asks and self.spread < 0.50)
        if self.has_book:
            self.mid = (self.best_bid + self.best_ask) / 2.0
        self.raw_bids = sorted_bids[:20]
        self.raw_asks = sorted_asks[:20]
        now = time.time()
        self.last_update = now
        self._record_mid(now)

    def update_from_bba(self, best_bid: str, best_ask: str):
        self.best_bid = float(best_bid) if best_bid else self.best_bid
        self.best_ask = float(best_ask) if best_ask else self.best_ask
        self.spread = round(self.best_ask - self.best_bid, 4)
        self.has_book = self.spread < 0.50
        if self.has_book:
            self.mid = (self.best_bid + self.best_ask) / 2.0
        now = time.time()
        self.last_update = now
        self._record_mid(now)

    def update_from_trade(self, price: str, side: str):
        self.last_trade_price = float(price)
        self.last_trade_side = side
        now = time.time()
        self.last_update = now
        self.last_trade_ts = now

    def _record_mid(self, ts: float):
        self.price_history.append((ts, self.mid))
        if len(self.price_history) > 3000:
            self.price_history = self.price_history[-1500:]

    def recent_move(self, window_sec: float = 2.0) -> float:
        """How much mid moved in the last window_sec. Positive = up."""
        if len(self.price_history) < 2:
            return 0.0
        now = self.price_history[-1][0]
        cutoff = now - window_sec
        oldest_in_window = self.price_history[-1][1]
        for ts, mid in self.price_history:
            if ts >= cutoff:
                oldest_in_window = mid
                break
        return self.price_history[-1][1] - oldest_in_window

    def available_depth(self, side: str = "buy", max_slippage_c: float = 0.03) -> tuple[float, float]:
        orders = self.raw_asks if side == "buy" else self.raw_bids
        if not orders:
            return 0.0, 0.0
        best = float(orders[0]["price"])
        total_usd = 0.0
        total_shares = 0.0
        for level in orders:
            price = float(level["price"])
            size = float(level.get("size", 0))
            if side == "buy" and price > best + max_slippage_c:
                break
            if side == "sell" and price < best - max_slippage_c:
                break
            total_usd += size * price
            total_shares += size
        avg_price = total_usd / total_shares if total_shares > 0 else best
        return total_usd, avg_price


class MarketWebSocket:
    def __init__(self, on_price_update: Optional[Callable] = None):
        self._subscribed: dict[str, BookState] = {}
        self._ws = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._on_price_update = on_price_update
        self._reconnect_delay = RECONNECT_DELAY
        self._pending_subs: list[str] = []
        self._last_msg_at: float = 0

    def get_book(self, token_id: str) -> Optional[BookState]:
        return self._subscribed.get(token_id)

    def subscribe(self, token_id: str):
        if token_id in self._subscribed:
            return
        self._subscribed[token_id] = BookState(token_id=token_id)
        self._pending_subs.append(token_id)
        if self._ws:
            asyncio.ensure_future(self._send_subscribe([token_id]))

    def unsubscribe(self, token_id: str):
        self._subscribed.pop(token_id, None)

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
        if self._watchdog_task:
            self._watchdog_task.cancel()

    async def _watchdog(self):
        """Force reconnect if no WS message received for STALE_DATA_TIMEOUT seconds."""
        while self._running:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if self._last_msg_at > 0 and self._ws:
                age = time.time() - self._last_msg_at
                if age > STALE_DATA_TIMEOUT:
                    log.warning("WS watchdog: no message for %.0fs — forcing reconnect", age)
                    try:
                        await self._ws.close()
                    except Exception:
                        pass

    async def _send_subscribe(self, token_ids: list[str]):
        if not self._ws or not token_ids:
            return
        msg = {"assets_ids": token_ids, "type": "market", "custom_feature_enabled": True}
        try:
            await self._ws.send(_dumps(msg))
            log.info("WS subscribed to %d tokens", len(token_ids))
        except Exception as e:
            log.warning("WS subscribe failed: %s", e)

    async def _run_loop(self):
        while self._running:
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=20, ping_timeout=10, close_timeout=5,
                    max_size=2**22, max_queue=512,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = RECONNECT_DELAY
                    log.info("WS connected to Polymarket market channel")

                    all_tokens = list(self._subscribed.keys())
                    if all_tokens:
                        await self._send_subscribe(all_tokens)
                    self._pending_subs.clear()

                    async for raw in ws:
                        if not self._running:
                            break
                        self._last_msg_at = time.time()
                        try:
                            parsed = _loads(raw)
                            msgs = parsed if isinstance(parsed, list) else [parsed]
                            for msg in msgs:
                                if isinstance(msg, dict):
                                    self._handle_message(msg)
                        except Exception:
                            continue

            except websockets.ConnectionClosed as e:
                log.warning("WS closed: %s", e)
            except Exception as e:
                log.warning("WS error: %s", e)

            if self._running:
                log.info("WS reconnecting in %.0fs...", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, MAX_RECONNECT_DELAY)

        self._ws = None

    def _handle_message(self, msg: dict):
        event_type = msg.get("event_type", "")
        asset_id = msg.get("asset_id", "")

        if event_type == "book":
            book = self._subscribed.get(asset_id)
            if book:
                book.update_from_book(msg.get("bids", []), msg.get("asks", []))
                if self._on_price_update:
                    self._on_price_update(asset_id, book)

        elif event_type == "best_bid_ask":
            book = self._subscribed.get(asset_id)
            if book:
                book.update_from_bba(msg.get("best_bid", ""), msg.get("best_ask", ""))
                if self._on_price_update:
                    self._on_price_update(asset_id, book)

        elif event_type == "last_trade_price":
            book = self._subscribed.get(asset_id)
            if book:
                book.update_from_trade(msg.get("price", "0.5"), msg.get("side", ""))
                if self._on_price_update:
                    self._on_price_update(asset_id, book)

        elif event_type == "price_change":
            for pc in msg.get("price_changes", []):
                aid = pc.get("asset_id", "")
                book = self._subscribed.get(aid)
                if book:
                    bb = pc.get("best_bid", "")
                    ba = pc.get("best_ask", "")
                    if bb and ba:
                        book.update_from_bba(bb, ba)
                        if self._on_price_update:
                            self._on_price_update(aid, book)

        elif event_type == "market_resolved":
            winning = msg.get("winning_asset_id", "")
            for aid in msg.get("assets_ids", []):
                book = self._subscribed.get(aid)
                if book:
                    book.mid = 1.0 if aid == winning else 0.0
                    book.has_book = False
                    if self._on_price_update:
                        self._on_price_update(aid, book)
