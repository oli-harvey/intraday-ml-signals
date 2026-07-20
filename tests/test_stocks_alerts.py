"""Transition logic for the live equities alerts.

The regression that matters: on 2026-07-13 the 30-symbol capture stalled its writer
for five hours (rows frozen, queue at cap, 34 reconnects) and nothing told us. These
tests pin the alerts that would have caught it.

2026-07-20 messaging review: the live heartbeat used to mix backtest numbers
(windowed/per-quote trade counts) with the REAL paper book in one dense,
unlabelled message — confusing, and the direct cause of "what is windowed vs
paper real fills?". position_line() is now ops-only (the real book);
research_summary() is a short, clearly-labelled backtest comparison used only
in the once-a-day close summary, not the live heartbeat.
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
    position_line,
    research_summary,
    shadow_descriptor,
)

NY = ZoneInfo("America/New_York")


def _state(**kw):
    base = {"state": "open", "events": 1000, "rows": 990, "q_hwm": 50,
            "reconnects": 0, "db_mb": 10.0, "dropped": 0, "paper": None,
            "sim_trades": 0, "sim_avg_bps": float("nan"),
            "pq_trades": 0, "pq_avg_bps": float("nan"),
            "config": "ev no-micro 5s dz4 spread<2bp"}
    return {**base, **kw}


def _paper(**kw):
    base = {
        "trades": 34, "avg_net_bps": 2.9, "pnl_usd": 98.6, "sim_gap_bps": -0.4,
        "order_errors": 0, "halted": False, "reconciliations": 0,
        "balance": 50_098.6, "cash": 50_098.6, "holdings_value": 0.0,
        "total": 50_098.6, "open_detail": {},
    }
    return {**base, **kw}


def _traded(**kw):
    return _state(
        sim_trades=214, sim_avg_bps=2.66, pq_trades=3764, pq_avg_bps=1.11,
        paper=_paper(), **kw,
    )


def test_position_line_reports_the_real_book_not_the_backtest():
    line = position_line(_traded())
    assert "34 real trades" in line
    assert "avg +2.90bps" in line
    assert "$+98.60" in line
    assert "stocks book $50,098.60" in line
    # the backtest numbers must NOT be in this message at all
    assert "214" not in line and "windowed" not in line


def test_position_line_is_explicit_when_no_trading_configured():
    assert "no real orders configured" in position_line(_state())


def test_position_line_shows_open_positions_marked_to_market():
    paper = _paper(open_detail={
        "NVDA": {"side": "long", "qty": 1, "entry_fill": 899.10,
                 "mid": 905.0, "value": 905.0, "unrealized_usd": 5.90},
        "AAPL": {"side": "short", "qty": -1, "entry_fill": 200.0,
                 "mid": 195.0, "value": -195.0, "unrealized_usd": 5.0},
    }, holdings_value=900.0, cash=49_198.6, total=50_098.6)
    line = position_line(_state(paper=paper))
    assert "NVDA long @ $899.10" in line and "$905.00 now" in line and "+5.90" in line
    assert "AAPL short @ $200.00" in line and "$-195.00 now" in line
    assert "cash $49,198.60" in line and "holdings $900.00" in line
    assert "total $50,098.60" in line


def test_position_line_flags_halt_and_reconciliations():
    line = position_line(_state(paper=_paper(halted=True, reconciliations=2)))
    assert "HALTED" in line and "2 reconciliation close(s)" in line


def test_research_summary_is_clearly_labelled_and_separate():
    """The whole point of the split: this must say plainly that it is NOT
    real trades, and must never appear in the live heartbeat (only the
    once-daily close)."""
    line = research_summary(_traded())
    assert "backtest, not real trades" in line
    assert "windowed 214 tr avg +2.66bps" in line
    assert "per-quote 3764 tr avg +1.11bps" in line


def test_position_line_never_contains_the_research_label():
    """The heartbeat is built from position_line() alone (main() appends it
    directly, with no research_summary() call) — assert that at the unit
    level: position_line()'s own output can never carry the backtest label,
    so wiring it into the heartbeat can't accidentally leak research numbers."""
    assert "backtest" not in position_line(_traded()).lower()


def test_close_summary_leads_with_the_real_position_and_a_short_research_line():
    msgs = detect(_traded(), _traded(state="closed"))
    assert len(msgs) == 1
    assert "session close" in msgs[0]
    assert "34 real trades" in msgs[0]
    assert "backtest, not real trades" in msgs[0]


def test_session_open_announced_once():
    msgs = detect({"state": "closed"}, _state())
    assert any("session open" in m for m in msgs)
    # already open -> silent
    assert detect(_state(), _state(events=2000, rows=1990)) == []


def test_session_open_escapes_the_config_string():
    """2026-07-20: config carries a literal '<' ('spread<2bp'). Telegram's HTML
    parser reads '<2bp' as a broken start tag and 400s the WHOLE message — and
    because that exception used to propagate out of main() before the state
    file was written, the cron retried and failed on this exact message every
    5 minutes for hours (184 identical crashes, zero stocks messages sent).
    The config string MUST be HTML-escaped before it reaches a <b>...</b>
    message sent with parse_mode=HTML."""
    (msg,) = detect({"state": "closed"}, _state(config="ev no-micro 5s dz4 spread<2bp"))
    assert "spread&lt;2bp" in msg
    assert "spread<2bp" not in msg  # the raw, Telegram-breaking form must be gone


def test_session_open_reports_real_orders_when_trading_is_on():
    msgs = detect({"state": "closed"}, _state(paper={"trades": 0}))
    assert any("REAL paper orders" in m for m in msgs)
    msgs = detect({"state": "closed"}, _state())  # no paper key -> shadow only
    assert any("no real orders" in m for m in msgs)


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


def test_disk_warning_fires_once_on_the_way_up():
    quiet = _state(disk_pct=60.0)
    full = _state(disk_pct=91.0)
    assert any("disk 91% full" in m for m in detect(quiet, full))
    # already high -> no repeat every 5 minutes
    assert not any("disk" in m for m in detect(full, full))


def test_in_session_uses_exchange_timezone_not_utc():
    # 09:29 ET is closed, 09:30 ET is open — regardless of DST
    assert not in_session(datetime(2026, 7, 13, 9, 29, tzinfo=NY))
    assert in_session(datetime(2026, 7, 13, 9, 30, tzinfo=NY))
    assert not in_session(datetime(2026, 7, 13, 16, 0, tzinfo=NY))
    assert not in_session(datetime(2026, 7, 11, 12, 0, tzinfo=NY))  # Saturday
