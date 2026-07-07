"""Coinbase Exchange public WebSocket adapter (leader/experiment feed).

Why: Alpaca's crypto quotes come from its own small venue, but BTC price
discovery happens on the majors. Coinbase's market-data feed is free and
keyless — streaming it alongside Alpaca lets a follower engine consume
same-asset cross-venue leader features (see features/cross.py).

We subscribe to the `ticker` channel: it fires on every match and carries the
BBO, so each message yields a Quote (best bid/ask) then a Tick (the match,
with taker side). Symbols are emitted as "CB:BTC/USD" so they can never be
confused with Alpaca's "BTC/USD" downstream.

Reconnect/backoff mirrors AlpacaSource; auth is not required for market data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import AsyncIterator, Sequence

import certifi
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .alpaca import rfc3339_to_ns
from .backoff import ExponentialBackoff
from .base import DataSource
from .schema import Bar, MarketEvent, Quote, Side, Tick

log = logging.getLogger(__name__)

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
HANDSHAKE_TIMEOUT_S = 10.0

_TAKER_SIDE = {"buy": Side.BUY, "sell": Side.SELL}


def product_to_symbol(product_id: str) -> str:
    """"BTC-USD" -> "CB:BTC/USD" (venue-prefixed, never collides with Alpaca)."""
    return f"CB:{product_id.replace('-', '/')}"


def parse_ticker(msg: dict, recv_ns: int) -> list[MarketEvent]:
    """One ticker message -> [Quote, Tick] (BBO first: quotes are the clock)."""
    symbol = product_to_symbol(msg["product_id"])
    ts_ns = rfc3339_to_ns(msg["time"])
    events: list[MarketEvent] = []
    bid = float(msg.get("best_bid") or 0.0)
    ask = float(msg.get("best_ask") or 0.0)
    if bid > 0.0 and ask > 0.0:
        events.append(
            Quote(
                symbol=symbol,
                ts_ns=ts_ns,
                bid=bid,
                ask=ask,
                bid_size=float(msg.get("best_bid_size") or 0.0),
                ask_size=float(msg.get("best_ask_size") or 0.0),
                recv_ns=recv_ns,
            )
        )
    price = float(msg.get("price") or 0.0)
    if price > 0.0:
        events.append(
            Tick(
                symbol=symbol,
                ts_ns=ts_ns,
                price=price,
                size=float(msg.get("last_size") or 0.0),
                side=_TAKER_SIDE.get(msg.get("side", "")),
                recv_ns=recv_ns,
            )
        )
    return events


class CoinbaseSource(DataSource):
    def __init__(
        self,
        url: str | None = None,  # override for tests
        reconnect_initial_s: float = 1.0,
        reconnect_cap_s: float = 30.0,
    ) -> None:
        self._url = url or COINBASE_WS_URL
        self._products: list[str] = []
        self._closed = False
        self._backoff = ExponentialBackoff(
            initial=reconnect_initial_s, cap=reconnect_cap_s, jitter=0.25
        )
        self._ssl = (
            ssl.create_default_context(cafile=certifi.where())
            if self._url.startswith("wss")
            else None
        )
        self.connects = 0

    @property
    def reconnects(self) -> int:
        return max(0, self.connects - 1)

    async def connect(self) -> None:
        return None

    async def subscribe(self, symbols: Sequence[str]) -> None:
        """Accepts Coinbase product ids ("BTC-USD")."""
        self._products = list(symbols)

    async def stream(self) -> AsyncIterator[MarketEvent]:  # type: ignore[override]
        if not self._products:
            raise RuntimeError("subscribe() must be called before stream()")
        sub = json.dumps(
            {
                "type": "subscribe",
                "product_ids": self._products,
                "channels": ["ticker", "heartbeat"],
            }
        )
        while not self._closed:
            try:
                async with connect(
                    self._url, ssl=self._ssl, ping_interval=20, ping_timeout=20
                ) as ws:
                    await ws.send(sub)
                    ack = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT_S))
                    if ack.get("type") == "error":
                        raise RuntimeError(f"coinbase subscribe rejected: {ack}")
                    self.connects += 1
                    self._backoff.reset()
                    log.info("coinbase stream up: %s", self._products)
                    async for raw in ws:
                        recv_ns = time.time_ns()
                        msg = json.loads(raw)
                        if msg.get("type") == "ticker":
                            for event in parse_ticker(msg, recv_ns):
                                yield event
                        # heartbeat/subscriptions messages: keepalive only
            except (OSError, WebSocketException, TimeoutError, RuntimeError) as exc:
                if self._closed:
                    break
                delay = self._backoff.next()
                log.warning("coinbase stream down (%r); reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def backfill_bars(
        self, symbol: str, lookback_s: int, resolution_s: int = 60
    ) -> list[Bar]:
        return []  # experiment feed; not needed

    async def close(self) -> None:
        self._closed = True
