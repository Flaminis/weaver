"""
Polymarket CLOB client — adapted from Oracle-Dota's poly-dota/polymarket/client.py.

Key differences from Oracle:
- Sells via GTC limit orders (maker), not FAK (taker)
- Sell verification polls order status instead of get_trades
- Explicit error handling for order rejections
- CLOB imports are optional (dry-run works without py-clob-client)
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

from polymarket.config import settings
from polymarket.logger import get_logger

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        AssetType,
        BalanceAllowanceParams,
        MarketOrderArgs,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        TradeParams,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False
    ClobClient = None

log = get_logger("poly.client")


class PolyClient:
    def __init__(self):
        self._clob: ClobClient | None = None
        self._http = httpx.AsyncClient(base_url=settings.poly.gamma_url)
        self._cfg = settings.poly
        self._ready = False

    # ── Connection ──────────────────────────────────────────────────────

    async def connect(self):
        if not HAS_CLOB:
            log.warning("py-clob-client not installed — read-only mode (pip install py-clob-client requires Python >=3.9.10)")
            return
        if not self._cfg.private_key:
            log.warning("No POLY_PRIVATE_KEY — read-only mode")
            return
        sig_type = 1 if self._cfg.funder_address else 0
        kwargs: dict = dict(
            host=self._cfg.clob_url,
            key=self._cfg.private_key,
            chain_id=self._cfg.chain_id,
            signature_type=sig_type,
        )
        if self._cfg.funder_address:
            kwargs["funder"] = self._cfg.funder_address
        self._clob = ClobClient(**kwargs)

        if self._cfg.api_key and self._cfg.api_secret and self._cfg.api_passphrase:
            creds = ApiCreds(
                api_key=self._cfg.api_key,
                api_secret=self._cfg.api_secret,
                api_passphrase=self._cfg.api_passphrase,
            )
            self._clob.set_api_creds(creds)
            log.info("Using explicit CLOB API creds (sig_type=%d)", sig_type)
        else:
            self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
            log.info("Derived CLOB API creds on-chain (sig_type=%d)", sig_type)
        self._ready = True

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def clob(self) -> ClobClient:
        if not self._clob or not self._ready:
            raise RuntimeError("Call connect() first")
        return self._clob

    # ── Balance ─────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self._clob.get_balance_allowance(params)
            raw_bal = float(resp.get("balance", 0))
            allowances = resp.get("allowances", {})
            raw_allow = max(float(v) for v in allowances.values()) if allowances else float(resp.get("allowance", 0))
            return {"balance": raw_bal / 1e6, "allowance": raw_allow / 1e6}
        except Exception as e:
            log.warning("Balance fetch failed: %s", e)
            return {"balance": 0.0, "allowance": 0.0}

    def get_token_balance(self, token_id: str) -> float:
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
            resp = self._clob.get_balance_allowance(params)
            return float(resp.get("balance", 0)) / 1e6
        except Exception as e:
            log.warning("get_token_balance(%s) failed: %s", token_id[:16], e)
            return 0.0

    # ── Tick size ───────────────────────────────────────────────────────

    _tick_cache: dict[str, str] = {}

    def _tick_size(self, token_id: str) -> str:
        if token_id in self._tick_cache:
            return self._tick_cache[token_id]
        try:
            ts = self.clob.get_tick_size(token_id)
            self._tick_cache[token_id] = ts
            return ts
        except Exception as e:
            log.warning("get_tick_size failed for %s: %s — defaulting 0.01", token_id[:16], e)
            return "0.01"

    def _align_price(self, price: float, tick_size: str) -> float:
        tv = float(tick_size)
        return round(round(price / tv) * tv, 4)

    # ── Order book ──────────────────────────────────────────────────────

    def get_order_book(self, token_id: str):
        return self.clob.get_order_book(token_id)

    def get_midpoint(self, token_id: str) -> float:
        resp = self.clob.get_midpoint(token_id)
        if isinstance(resp, dict):
            return float(resp.get("mid", resp.get("price", 0.5)))
        return float(resp)

    # ── Entry: FAK buy (taker) ──────────────────────────────────────────

    def buy_fak(
        self,
        token_id: str,
        price: float,
        size: float,
        neg_risk: bool = False,
    ) -> dict:
        """Place a FAK buy order. Takes liquidity up to limit price.
        size = number of shares to buy.
        price = max price willing to pay per share.
        """
        tick_size = self._tick_size(token_id)
        aligned = self._align_price(price, tick_size)
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

        usdc_amount = round(size * aligned, 2)
        args = MarketOrderArgs(
            token_id=token_id,
            amount=usdc_amount,
            side=BUY,
            price=aligned,
        )
        signed = self.clob.create_market_order(args, options)
        resp = self.clob.post_order(signed, OrderType.FAK)

        order_id = resp.get("orderID", "")
        status = resp.get("status", "")
        log.info("BUY FAK %s @ %.4f x %.1f → id=%s status=%s",
                 token_id[:16], aligned, size, order_id[:16] if order_id else "?", status)

        if not order_id:
            log.error("BUY FAK returned no orderID: %s", resp)
        return resp

    # ── Exit: GTC limit sell (maker) ────────────────────────────────────

    def sell_limit(
        self,
        token_id: str,
        price: float,
        size: float,
        neg_risk: bool = False,
    ) -> dict:
        """Place a GTC limit sell order. Provides liquidity (maker).
        Posts at the specified price and waits for a taker to fill.
        """
        tick_size = self._tick_size(token_id)
        aligned = self._align_price(price, tick_size)
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

        args = OrderArgs(
            price=aligned,
            size=size,
            side=SELL,
            token_id=token_id,
        )
        signed = self.clob.create_order(args, options)
        resp = self.clob.post_order(signed, OrderType.GTC)

        order_id = resp.get("orderID", "")
        log.info("SELL GTC %s @ %.4f x %.1f → id=%s",
                 token_id[:16], aligned, size, order_id[:16] if order_id else "?")

        if not order_id:
            log.error("SELL GTC returned no orderID: %s", resp)
        return resp

    # ── Emergency exit: FAK sell (taker, for timeout fallback) ──────────

    def sell_fak(
        self,
        token_id: str,
        price: float,
        size: float,
        neg_risk: bool = False,
    ) -> dict:
        """Emergency FAK sell when GTC didn't fill. Crosses the spread."""
        tick_size = self._tick_size(token_id)
        aligned = self._align_price(price, tick_size)
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

        args = MarketOrderArgs(
            token_id=token_id,
            amount=size,
            side=SELL,
            price=aligned,
        )
        signed = self.clob.create_market_order(args, options)
        resp = self.clob.post_order(signed, OrderType.FAK)

        order_id = resp.get("orderID", "")
        log.info("SELL FAK (emergency) %s @ %.4f x %.1f → id=%s",
                 token_id[:16], aligned, size, order_id[:16] if order_id else "?")
        return resp

    # ── Fill verification ───────────────────────────────────────────────

    def verify_buy_fill(self, order_id: str, placed_ts: int) -> Optional[dict]:
        """FAK fills are purged from CLOB. Check get_trades for the fill."""
        try:
            params = TradeParams(after=placed_ts - 10)
            trades = self.clob.get_trades(params)
            n = len(trades) if trades else 0
            for t in trades:
                if t.get("taker_order_id") == order_id:
                    log.info("verify_buy_fill: MATCH in %d trades (status=%s)", n, t.get("status"))
                    return t
            log.debug("verify_buy_fill: %d trades, no match for %s", n, order_id[:16] if order_id else "?")
            return None
        except Exception as e:
            log.warning("verify_buy_fill(%s) failed: %s", order_id[:16] if order_id else "?", e)
            return None

    def check_sell_order(self, order_id: str) -> Optional[dict]:
        """Check GTC sell order status. Returns order dict or None."""
        try:
            return self.clob.get_order(order_id)
        except Exception as e:
            log.warning("check_sell_order(%s) failed: %s", order_id[:16] if order_id else "?", e)
            return None

    # ── Cancel ──────────────────────────────────────────────────────────

    def cancel_order(self, order_id: str):
        try:
            return self.clob.cancel(order_id)
        except Exception as e:
            log.warning("cancel_order(%s) failed: %s", order_id[:16] if order_id else "?", e)
            return None

    def cancel_all(self):
        try:
            return self.clob.cancel_all()
        except Exception as e:
            log.warning("cancel_all failed: %s", e)
            return None

    def get_open_orders(self):
        try:
            return self.clob.get_orders()
        except Exception as e:
            log.warning("get_open_orders failed: %s", e)
            return []

    def get_trades(self, after_ts: int = 0) -> list[dict]:
        try:
            return self.clob.get_trades(TradeParams(after=after_ts)) or []
        except Exception as e:
            log.warning("get_trades failed: %s", e)
            return []

    async def close(self):
        await self._http.aclose()


poly_client = PolyClient()
