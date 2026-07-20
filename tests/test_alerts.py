"""Telegram alert transition logic (pure function, no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from alerts import condense, detect_alerts, holdings_line  # noqa: E402

BASE = {"fresh": "live", "breaker": "ok", "order_errors": 0, "orders": 0,
        "pnl": 0.0, "last_order": None, "equity": 99_930.0, "positions": {}}


def test_no_alerts_when_nothing_changed() -> None:
    assert detect_alerts(dict(BASE), dict(BASE)) == []


def test_offline_and_recovery_transitions() -> None:
    offline = dict(BASE, fresh="offline")
    assert any("OFFLINE" in a for a in detect_alerts(dict(BASE), offline))
    assert any("LIVE" in a for a in detect_alerts(offline, dict(BASE)))
    # steady offline: no repeat spam
    assert detect_alerts(offline, dict(offline)) == []


def test_breaker_trip_fires_once() -> None:
    tripped = dict(BASE, breaker="TRIPPED", pnl=-101.0)
    alerts = detect_alerts(dict(BASE), tripped)
    assert any("TRIPPED" in a for a in alerts)
    assert detect_alerts(tripped, dict(tripped)) == []


def test_new_trade_alert_includes_details() -> None:
    cur = dict(BASE, orders=1, last_order={
        "side": "buy", "qty": 0.0016, "symbol": "BTC/USD",
        "price": 61000.0, "note": "enter long"})
    alerts = detect_alerts(dict(BASE), cur)
    assert len(alerts) == 1 and "BTC/USD" in alerts[0] and "buy" in alerts[0]


def test_trade_alert_reports_crypto_book_and_holdings_marked_to_market() -> None:
    """Every buy/sell message must answer 'and where does that leave me?' —
    reporting the CRYPTO book (the account is virtually split with stocks),
    each holding's CURRENT value (not just entry), and cash/holdings/total."""
    cur = dict(BASE, orders=1, crypto_book=49_930.0,
               last_order={"side": "buy", "qty": 6.71, "symbol": "SOL/USD",
                           "price": 74.46, "note": "enter long"},
               positions={
                   "SOL/USD": {"qty": 6.71, "entry": 74.462, "mid": 75.0,
                               "value": 6.71 * 75.0},
                   "BTC/USD": {"qty": 0.0016, "entry": 61_250.0, "mid": 60_000.0,
                               "value": 0.0016 * 60_000.0},
               })
    (msg,) = detect_alerts(dict(BASE), cur)
    assert "crypto book $49,930.00" in msg
    assert "6.71 SOL @ $74.46" in msg and "$503.25 now" in msg  # 6.71*75.0
    assert "0.0016 BTC @ $61,250.00" in msg and "$96.00 now" in msg  # 0.0016*60000
    holdings_value = 6.71 * 75.0 + 0.0016 * 60_000.0
    cash = 49_930.0 - holdings_value
    assert f"cash ${cash:,.2f}" in msg
    assert f"holdings ${holdings_value:,.2f}" in msg
    assert f"total ${49_930.0:,.2f}" in msg  # cash + holdings == book, always


def test_holdings_line_when_flat() -> None:
    line = holdings_line(dict(BASE, crypto_book=49_930.0))
    assert "crypto book $49,930.00" in line and "holdings: none" in line
    assert "cash $49,930.00" in line and "holdings $0.00" in line
    assert "total $49,930.00" in line
    # no split-book info at all (old status) -> falls back to account equity
    assert "holdings: none" in holdings_line({"equity": 5.0})


def test_condense_marks_offline_on_stale_status() -> None:
    assert condense({}, age_s=1e9)["fresh"] == "offline"
    assert condense({"ts": 0}, age_s=10)["fresh"] == "live"


def test_first_run_no_prev_state_is_quiet() -> None:
    # empty prev (first ever run) must not fire offline/recovery noise
    assert detect_alerts({}, dict(BASE)) == []
