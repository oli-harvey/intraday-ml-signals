"""AlpacaSource reconnect: a local mini-Alpaca server drops the first connection
after one trade; the client must back off, reconnect, re-auth, and keep streaming."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from signals.config import AlpacaConfig
from signals.data.alpaca import AlpacaAuthError, AlpacaSource
from signals.data.schema import Tick

CFG = AlpacaConfig(api_key="test-key", secret_key="test-secret")


def _trade(n: int) -> dict:
    return {
        "T": "t",
        "S": "BTC/USD",
        "p": 100.0 + n,
        "s": 1.0,
        "t": "2026-07-02T10:13:57.5Z",
        "tks": "B",
    }


async def _handshake_server_side(ws, auth_ok: bool = True) -> dict:
    await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
    auth = json.loads(await ws.recv())
    assert auth["action"] == "auth"
    if not auth_ok:
        await ws.send(json.dumps([{"T": "error", "code": 402, "msg": "auth failed"}]))
        return {}
    await ws.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
    sub = json.loads(await ws.recv())
    await ws.send(json.dumps([{"T": "subscription", "trades": sub.get("trades", [])}]))
    return sub


async def test_reconnects_after_server_drop() -> None:
    connections = 0

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        nonlocal connections
        connections += 1
        sub = await _handshake_server_side(ws)
        assert sub.get("trades") == ["BTC/USD"]
        await ws.send(json.dumps([_trade(connections)]))
        if connections == 1:
            await ws.close(code=1011)  # abnormal drop -> client must reconnect
        else:
            await asyncio.sleep(10)  # hold open; test cancels us

    async with serve(handler, "127.0.0.1", 0) as server:
        sockets = getattr(server, "sockets", None) or server.server.sockets
        port = sockets[0].getsockname()[1]
        source = AlpacaSource(
            CFG,
            url=f"ws://127.0.0.1:{port}",
            subscribe_bars=False,
            reconnect_initial_s=0.05,
        )
        await source.subscribe(["BTC/USD"])

        async def take_two() -> list[Tick]:
            got: list[Tick] = []
            async for event in source.stream():
                assert isinstance(event, Tick)
                got.append(event)
                if len(got) == 2:
                    break
            return got

        ticks = await asyncio.wait_for(take_two(), timeout=5)

    assert [t.price for t in ticks] == [101.0, 102.0]  # one trade per connection
    assert connections == 2
    assert source.reconnects == 1


async def test_auth_failure_is_fatal_not_retried() -> None:
    connections = 0

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        nonlocal connections
        connections += 1
        await _handshake_server_side(ws, auth_ok=False)

    async with serve(handler, "127.0.0.1", 0) as server:
        sockets = getattr(server, "sockets", None) or server.server.sockets
        port = sockets[0].getsockname()[1]
        source = AlpacaSource(CFG, url=f"ws://127.0.0.1:{port}", reconnect_initial_s=0.05)
        await source.subscribe(["BTC/USD"])
        with pytest.raises(AlpacaAuthError):
            async for _ in source.stream():
                pass

    assert connections == 1  # no retry on bad credentials
