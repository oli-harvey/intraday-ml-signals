"""Telegram alert transition logic (pure function, no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from alerts import condense, detect_alerts  # noqa: E402

BASE = {"fresh": "live", "breaker": "ok", "order_errors": 0, "orders": 0,
        "pnl": 0.0, "last_order": None}


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


def test_condense_marks_offline_on_stale_status() -> None:
    assert condense({}, age_s=1e9)["fresh"] == "offline"
    assert condense({"ts": 0}, age_s=10)["fresh"] == "live"


def test_first_run_no_prev_state_is_quiet() -> None:
    # empty prev (first ever run) must not fire offline/recovery noise
    assert detect_alerts({}, dict(BASE)) == []
