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

from stocks_alerts import QUEUE_CAP, detect, in_session  # noqa: E402

NY = ZoneInfo("America/New_York")


def _state(**kw):
    base = {"state": "open", "events": 1000, "rows": 990, "q_hwm": 50,
            "reconnects": 0, "db_mb": 10.0}
    return {**base, **kw}


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


def test_in_session_uses_exchange_timezone_not_utc():
    # 09:29 ET is closed, 09:30 ET is open — regardless of DST
    assert not in_session(datetime(2026, 7, 13, 9, 29, tzinfo=NY))
    assert in_session(datetime(2026, 7, 13, 9, 30, tzinfo=NY))
    assert not in_session(datetime(2026, 7, 13, 16, 0, tzinfo=NY))
    assert not in_session(datetime(2026, 7, 11, 12, 0, tzinfo=NY))  # Saturday
