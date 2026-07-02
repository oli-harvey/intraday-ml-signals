"""Alpaca message parsing: RFC-3339 ns timestamps + stream message -> schema events."""

from datetime import UTC, datetime

from signals.data.alpaca import parse_message, rfc3339_to_ns
from signals.data.schema import Bar, Quote, Side, Tick


def _epoch_ns(y: int, mo: int, d: int, h: int, mi: int, s: int) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp()) * 1_000_000_000


def test_rfc3339_full_nanoseconds() -> None:
    assert (
        rfc3339_to_ns("2026-07-02T10:13:57.123456789Z")
        == _epoch_ns(2026, 7, 2, 10, 13, 57) + 123_456_789
    )


def test_rfc3339_short_fraction_pads_right() -> None:
    expected = _epoch_ns(2026, 7, 2, 10, 13, 57) + 500_000_000
    assert rfc3339_to_ns("2026-07-02T10:13:57.5Z") == expected


def test_rfc3339_no_fraction_and_offset_forms() -> None:
    expected = _epoch_ns(2026, 7, 2, 10, 13, 57)
    assert rfc3339_to_ns("2026-07-02T10:13:57Z") == expected
    assert rfc3339_to_ns("2026-07-02T10:13:57+00:00") == expected
    assert rfc3339_to_ns("2026-07-02T10:13:57.25+00:00") == expected + 250_000_000


def test_parse_crypto_trade() -> None:
    msg = {
        "T": "t",
        "S": "BTC/USD",
        "p": 65000.5,
        "s": 0.01,
        "t": "2026-07-02T10:13:57.5Z",
        "i": 12345,
        "tks": "B",
    }
    event = parse_message(msg, recv_ns=42)
    assert isinstance(event, Tick)
    assert event.symbol == "BTC/USD"
    assert event.price == 65000.5
    assert event.size == 0.01
    assert event.side is Side.BUY
    assert event.recv_ns == 42


def test_parse_stock_trade_has_no_side() -> None:
    msg = {"T": "t", "S": "AAPL", "p": 210.0, "s": 100, "t": "2026-07-02T14:30:00Z", "x": "V"}
    event = parse_message(msg, recv_ns=0)
    assert isinstance(event, Tick)
    assert event.side is None


def test_parse_quote() -> None:
    msg = {
        "T": "q",
        "S": "BTC/USD",
        "bp": 64999.0,
        "bs": 1.5,
        "ap": 65001.0,
        "as": 2.0,
        "t": "2026-07-02T10:13:57Z",
    }
    event = parse_message(msg, recv_ns=0)
    assert isinstance(event, Quote)
    assert event.spread == 2.0
    assert event.mid == 65000.0


def test_parse_bar() -> None:
    msg = {
        "T": "b",
        "S": "BTC/USD",
        "o": 1.0,
        "h": 2.0,
        "l": 0.5,
        "c": 1.5,
        "v": 10.0,
        "t": "2026-07-02T10:13:00Z",
    }
    event = parse_message(msg, recv_ns=0)
    assert isinstance(event, Bar)
    assert event.close == 1.5


def test_control_messages_return_none() -> None:
    for msg in (
        {"T": "success", "msg": "connected"},
        {"T": "subscription", "trades": ["BTC/USD"]},
        {"T": "error", "code": 406, "msg": "connection limit exceeded"},
    ):
        assert parse_message(msg, recv_ns=0) is None
