"""Transition logic for the live equities alerts.

The regression that matters: on 2026-07-13 the 30-symbol capture stalled its writer
for five hours (rows frozen, queue at cap, 34 reconnects) and nothing told us. These
tests pin the alerts that would have caught it.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from stocks_alerts import (  # noqa: E402
    LOG_LINE,
    QUEUE_CAP,
    detect,
    in_session,
    shadow_descriptor,
    trading_line,
)

NY = ZoneInfo("America/New_York")


def _state(**kw):
    base = {"state": "open", "events": 1000, "rows": 990, "q_hwm": 50,
            "reconnects": 0, "db_mb": 10.0, "dropped": 0, "trades": 0,
            "net_bps": 0.0, "avg_bps": float("nan"), "hit": float("nan"),
            "by_symbol": {}, "config": "ev no-micro 5s dz4 spread<2bp"}
    return {**base, **kw}


def _traded(**kw):
    return _state(
        trades=34, net_bps=98.6, avg_bps=2.9, hit=0.62,
        by_symbol={
            "NVDA": {"trades": 21, "net_bps": 65.1, "avg_net_bps": 3.1, "hit_rate": 0.62},
            "AAPL": {"trades": 13, "net_bps": 33.5, "avg_net_bps": 2.2, "hit_rate": 0.61},
        },
        **kw,
    )


def test_trading_line_reports_how_many_stocks_were_traded():
    line = trading_line(_traded())
    assert "34 trades" in line
    assert "+99bps" in line or "+98bps" in line  # net for the session
    assert "hit 62%" in line
    assert "NVDA 21@+3.1" in line and "AAPL 13@+2.2" in line


def test_trading_line_is_explicit_when_nothing_traded():
    assert "trades 0" in trading_line(_state())


def test_close_summary_leads_with_the_trading_result():
    msgs = detect(_traded(), _traded(state="closed"))
    assert len(msgs) == 1
    assert "session close" in msgs[0]
    assert "34 trades" in msgs[0]  # the number the user actually asked for


def test_session_open_announced_once():
    msgs = detect({"state": "closed"}, _state())
    assert any("session open" in m for m in msgs)
    # already open -> silent
    assert detect(_state(), _state(events=2000, rows=1990)) == []


def test_writer_stall_fires_the_alarm():
    """The 07-13 failure: events climb, rows frozen."""
    prev = _state(events=8_600_000, rows=8_631_663)
    cur = _state(state="stalled", events=9_035_989, rows=8_631_663)
    msgs = detect(prev, cur)
    assert any("STALLED" in m for m in msgs), msgs
    assert any("Data is being lost" in m for m in msgs)


def test_stall_alerts_once_not_every_poll():
    prev = _state(state="stalled", events=9_000_000, rows=8_631_663)
    cur = _state(state="stalled", events=9_100_000, rows=8_631_663)
    assert not any("STALLED" in m for m in detect(prev, cur))


def test_capture_down_and_recovery():
    down = detect(_state(), _state(state="down"))
    assert any("DOWN" in m for m in down)
    back = detect(_state(state="down"), _state(state="open"))
    assert any("recovered" in m for m in back)


def test_queue_saturation_warns_on_the_way_up_only():
    prev = _state(q_hwm=1_000)
    cur = _state(q_hwm=QUEUE_CAP)  # pinned at the cap, as on 07-13
    assert any("queue backlog" in m for m in detect(prev, cur))
    # already high -> don't repeat
    assert not any("queue backlog" in m for m in detect(cur, cur))


def test_reconnects_warn_when_climbing():
    msgs = detect(_state(reconnects=0), _state(reconnects=34))
    assert any("reconnects: 34" in m for m in msgs)


def test_close_summary_reports_unwritten_rows():
    msgs = detect(_state(), _state(state="closed", events=9_000_000, rows=8_600_000,
                                   reconnects=34, q_hwm=QUEUE_CAP))
    assert len(msgs) == 1 and "session close" in msgs[0]
    assert "400,000" in msgs[0]  # unwritten = events - rows, surfaced honestly


def test_log_line_matches_both_capture_processes():
    """read_log must parse BOTH formats. The old regex only matched record.py's
    `rows_written=`; stocks_live.py prints `rows=`, so a transient status miss fell
    back to a STALE record.py line and mislabelled the session 'capture-only'."""
    live = ("[+  2821s] events=4617167 rows=4614559 q_hwm=1674 reconnects=0 dropped=0 "
            "| windowed: trades=51 avg=+2.81bps | per-quote: trades=1619 avg=+4.37bps")
    rec = "[+   30s] events=16814 rows_written=10931 q_hwm=4175 reconnects=0"
    for line, want_rows in ((live, 4614559), (rec, 10931)):
        m = LOG_LINE.search(line)
        assert m is not None, line
        assert int(m.group(3)) == want_rows


def test_shadow_descriptor_never_says_capture_only_while_live_model_runs():
    """The user-visible bug: the open message said 'capture-only (no live model)'
    while stocks_live.py was running. When status is momentarily unreadable, the
    descriptor must follow the RUNNING PROCESS, not a stale log string."""
    # status readable -> its own config wins
    assert shadow_descriptor({"config": "ev no-micro 5s dz4 spread<2bp"}, "live") \
        == "ev no-micro 5s dz4 spread<2bp"
    # status missing but stocks_live.py up -> a live label, NEVER 'capture-only'
    d = shadow_descriptor(None, "live")
    assert "capture-only" not in d and "no live model" not in d
    # genuinely capture-only -> the honest label is allowed
    assert shadow_descriptor(None, "record") == "capture-only (no live model)"
    # nothing running -> empty, not a false claim either way
    assert shadow_descriptor(None, None) == ""


def test_in_session_uses_exchange_timezone_not_utc():
    # 09:29 ET is closed, 09:30 ET is open — regardless of DST
    assert not in_session(datetime(2026, 7, 13, 9, 29, tzinfo=NY))
    assert in_session(datetime(2026, 7, 13, 9, 30, tzinfo=NY))
    assert not in_session(datetime(2026, 7, 13, 16, 0, tzinfo=NY))
    assert not in_session(datetime(2026, 7, 11, 12, 0, tzinfo=NY))  # Saturday
