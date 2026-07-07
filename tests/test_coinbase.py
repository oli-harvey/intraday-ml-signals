"""CoinbaseSource message parsing + live subscribe against a fake server."""

import asyncio
import json

from websockets.asyncio.server import serve

from signals.data.coinbase import CoinbaseSource, parse_ticker, product_to_symbol
from signals.data.schema import Quote, Side, Tick

TICKER = {
    "type": "ticker",
    "product_id": "BTC-USD",
    "price": "61500.01",
    "best_bid": "61499.50",
    "best_ask": "61500.52",
    "best_bid_size": "0.5",
    "best_ask_size": "1.2",
    "side": "buy",
    "last_size": "0.02",
    "time": "2026-07-07T18:00:00.123456Z",
}


def test_product_symbol_mapping() -> None:
    assert product_to_symbol("BTC-USD") == "CB:BTC/USD"


def test_parse_ticker_yields_quote_then_tick() -> None:
    events = parse_ticker(TICKER, recv_ns=42)
    assert len(events) == 2
    quote, tick = events
    assert isinstance(quote, Quote) and isinstance(tick, Tick)
    assert quote.symbol == tick.symbol == "CB:BTC/USD"
    assert quote.bid == 61499.50 and quote.ask == 61500.52
    assert quote.mid == (61499.50 + 61500.52) / 2
    assert tick.price == 61500.01 and tick.side is Side.BUY
    assert quote.recv_ns == 42


def test_parse_ticker_missing_bbo_yields_tick_only() -> None:
    msg = dict(TICKER)
    del msg["best_bid"], msg["best_ask"]
    events = parse_ticker(msg, recv_ns=0)
    assert len(events) == 1 and isinstance(events[0], Tick)


async def test_stream_against_fake_server() -> None:
    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        sub = json.loads(await ws.recv())
        assert sub["type"] == "subscribe" and sub["product_ids"] == ["BTC-USD"]
        await ws.send(json.dumps({"type": "subscriptions", "channels": []}))
        await ws.send(json.dumps(TICKER))
        await asyncio.sleep(10)  # hold open; test cancels

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        source = CoinbaseSource(url=f"ws://127.0.0.1:{port}")
        await source.subscribe(["BTC-USD"])

        async def take_two():  # type: ignore[no-untyped-def]
            out = []
            async for event in source.stream():
                out.append(event)
                if len(out) == 2:
                    return out

        events = await asyncio.wait_for(take_two(), timeout=5)
    assert isinstance(events[0], Quote) and isinstance(events[1], Tick)
    assert source.connects == 1
