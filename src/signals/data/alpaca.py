"""Alpaca DataSource adapter (primary feed).

- WebSocket: real-time trades/quotes/bars. Crypto (24/7) and stocks (IEX free feed)
  speak the same JSON protocol on different URLs. We drive `websockets` directly
  (per the brief) rather than alpaca-py's stream loop, so reconnect/backoff and
  parsing stay under our control and O(1) per message.
- REST backfill: via alpaca-py's historical client (cold path only, run in a
  thread; imports are function-local to keep the hot import path lean).

Heartbeats are websockets' built-in ping/pong (ping_interval); a peer that stops
answering trips ConnectionClosed and we reconnect with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone

import certifi
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from ..config import AlpacaConfig
from .backoff import ExponentialBackoff
from .base import DataSource
from .schema import Bar, MarketEvent, Quote, Side, Tick

log = logging.getLogger(__name__)

CRYPTO_WS_URL = "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
STOCK_WS_URL = "wss://stream.data.alpaca.markets/v2/{feed}"
HANDSHAKE_TIMEOUT_S = 10.0

_TAKER_SIDE = {"B": Side.BUY, "S": Side.SELL}


class AlpacaAuthError(RuntimeError):
    """Fatal: bad credentials. Never retried."""


class AlpacaProtocolError(RuntimeError):
    """Retryable: unexpected handshake response (e.g. connection limit)."""


def rfc3339_to_ns(ts: str) -> int:
    """Parse an RFC-3339 timestamp (up to ns precision) to epoch nanoseconds.

    datetime only handles microseconds, so the fractional part is split off and
    handled as an integer to preserve full nanosecond resolution.
    """
    if "." in ts:
        head, _, rest = ts.partition(".")
        digits = ""
        tz = ""
        for i, ch in enumerate(rest):
            if not ch.isdigit():
                digits, tz = rest[:i], rest[i:]
                break
        else:
            digits = rest
        ns = int(digits.ljust(9, "0")[:9])
        dt = datetime.fromisoformat(head + (tz or "+00:00"))
    else:
        ns = 0
        dt = datetime.fromisoformat(ts)
    return int(dt.timestamp()) * 1_000_000_000 + ns


def parse_message(msg: dict, recv_ns: int) -> MarketEvent | None:
    """Map one Alpaca stream message to a schema event. None for control messages."""
    kind = msg.get("T")
    if kind == "t":
        return Tick(
            symbol=msg["S"],
            ts_ns=rfc3339_to_ns(msg["t"]),
            price=float(msg["p"]),
            size=float(msg["s"]),
            side=_TAKER_SIDE.get(msg.get("tks", "")),  # crypto only; stocks: None
            recv_ns=recv_ns,
        )
    if kind == "q":
        return Quote(
            symbol=msg["S"],
            ts_ns=rfc3339_to_ns(msg["t"]),
            bid=float(msg["bp"]),
            ask=float(msg["ap"]),
            bid_size=float(msg["bs"]),
            ask_size=float(msg["as"]),
            recv_ns=recv_ns,
        )
    if kind == "b":
        return Bar(
            symbol=msg["S"],
            ts_ns=rfc3339_to_ns(msg["t"]),
            open=float(msg["o"]),
            high=float(msg["h"]),
            low=float(msg["l"]),
            close=float(msg["c"]),
            volume=float(msg["v"]),
            recv_ns=recv_ns,
        )
    return None  # success/subscription/error control messages


class AlpacaSource(DataSource):
    def __init__(
        self,
        config: AlpacaConfig,
        market: str = "crypto",  # "crypto" | "stocks"
        subscribe_trades: bool = True,
        subscribe_quotes: bool = False,
        subscribe_bars: bool = True,
        url: str | None = None,  # override for tests
        reconnect_initial_s: float = 1.0,
        reconnect_cap_s: float = 30.0,
    ) -> None:
        self.config = config
        self.market = market
        self._url = url or (
            CRYPTO_WS_URL if market == "crypto" else STOCK_WS_URL.format(feed=config.data_feed)
        )
        self._want = {
            "trades": subscribe_trades,
            "quotes": subscribe_quotes,
            "bars": subscribe_bars,
        }
        self._backoff = ExponentialBackoff(
            initial=reconnect_initial_s, cap=reconnect_cap_s, jitter=0.25
        )
        self._symbols: list[str] = []
        self._closed = False
        # certifi CA bundle: macOS Pythons often lack system certs in OpenSSL,
        # which breaks wss verification (REST via requests already uses certifi).
        self._ssl = (
            ssl.create_default_context(cafile=certifi.where())
            if self._url.startswith("wss")
            else None
        )
        self.connects = 0  # successful handshakes; reconnects = connects - 1

    @property
    def reconnects(self) -> int:
        return max(0, self.connects - 1)

    async def connect(self) -> None:
        return None  # connection is owned by stream(); nothing to do eagerly

    async def subscribe(self, symbols: Sequence[str]) -> None:
        self._symbols = list(symbols)

    async def stream(self) -> AsyncIterator[MarketEvent]:  # type: ignore[override]
        if not self._symbols:
            raise RuntimeError("subscribe() must be called before stream()")
        while not self._closed:
            try:
                async with connect(
                    self._url, ssl=self._ssl, ping_interval=20, ping_timeout=20
                ) as ws:
                    await asyncio.wait_for(self._handshake(ws), HANDSHAKE_TIMEOUT_S)
                    self.connects += 1
                    self._backoff.reset()
                    log.info("alpaca stream up: %s %s", self.market, self._symbols)
                    async for raw in ws:
                        recv_ns = time.time_ns()
                        payload = json.loads(raw)
                        msgs = payload if isinstance(payload, list) else [payload]
                        for msg in msgs:
                            event = parse_message(msg, recv_ns)
                            if event is not None:
                                yield event
                            elif msg.get("T") == "error":
                                log.warning("alpaca error message: %s", msg)
            except AlpacaAuthError:
                raise  # bad credentials — retrying can't help
            except (OSError, WebSocketException, TimeoutError, AlpacaProtocolError) as exc:
                if self._closed:
                    break
                delay = self._backoff.next()
                log.warning("alpaca stream down (%r); reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def _handshake(self, ws) -> None:  # type: ignore[no-untyped-def]
        """connected banner -> auth -> subscribe. Raises on auth failure."""
        banner = json.loads(await ws.recv())
        self._expect_success(banner, "connected")
        await ws.send(
            json.dumps(
                {"action": "auth", "key": self.config.api_key, "secret": self.config.secret_key}
            )
        )
        self._expect_success(json.loads(await ws.recv()), "authenticated")
        sub: dict = {"action": "subscribe"}
        for channel, wanted in self._want.items():
            if wanted:
                sub[channel] = self._symbols
        await ws.send(json.dumps(sub))
        # Subscription confirmation arrives asynchronously; no need to block on it.

    @staticmethod
    def _expect_success(payload: object, expected_msg: str) -> None:
        msgs = payload if isinstance(payload, list) else [payload]
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            if msg.get("T") == "success" and msg.get("msg") == expected_msg:
                return
            if msg.get("T") == "error":
                if msg.get("code") in (401, 402):  # not authenticated / auth failed
                    raise AlpacaAuthError(f"auth failed: {msg}")
                raise AlpacaProtocolError(f"handshake error: {msg}")
        raise AlpacaProtocolError(f"expected success '{expected_msg}', got: {payload}")

    async def backfill_bars(
        self, symbol: str, lookback_s: int, resolution_s: int = 60
    ) -> list[Bar]:
        if resolution_s % 60 != 0:
            raise ValueError("Alpaca historical bars are minute resolution at finest")
        return await asyncio.to_thread(self._fetch_bars_sync, symbol, lookback_s, resolution_s)

    def _fetch_bars_sync(self, symbol: str, lookback_s: int, resolution_s: int) -> list[Bar]:
        # Cold path: alpaca-py's historical stack (and its transitive pandas) is
        # imported here only, never at module import time.
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        start = datetime.now(timezone.utc) - timedelta(seconds=lookback_s)
        timeframe = TimeFrame(resolution_s // 60, TimeFrameUnit.Minute)
        if self.market == "crypto":
            crypto = CryptoHistoricalDataClient(self.config.api_key, self.config.secret_key)
            raw = crypto.get_crypto_bars(
                CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start)
            )
        else:
            stocks = StockHistoricalDataClient(self.config.api_key, self.config.secret_key)
            raw = stocks.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=timeframe,
                    start=start,
                    feed=self.config.data_feed,
                )
            )
        return [
            Bar(
                symbol=symbol,
                ts_ns=int(b.timestamp.timestamp() * 1e9),
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
            )
            for b in raw.data.get(symbol, [])
        ]

    async def close(self) -> None:
        self._closed = True
