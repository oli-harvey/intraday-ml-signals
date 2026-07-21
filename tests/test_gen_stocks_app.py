"""The stocks Web App dashboard, executed against both a cold start (no
capture has ever run) and a populated status_stocks.json — gen_dashboard.py's
render() has broken twice on f-string brace escaping that only surfaces at
runtime, so these tests actually EXECUTE render(), not just import the module.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gen_stocks_app as gsa  # noqa: E402

PLACEHOLDER = re.compile(r"\{:[+.\-\w]*\}")

STATUS = {
    "ts": 0.0, "events": 1_234_567, "reconnects": 0, "q_hwm": 1200,
    "config": "ev no-micro 5s dz4 spread<2bp",
    "sim": {
        "trades": 214, "avg_net_bps": 2.66, "hit_rate": 0.35,
        "by_symbol": {
            "NVDA": {"trades": 113, "avg_net_bps": 1.2, "hit_rate": 0.4},
            "AAPL": {"trades": 33, "avg_net_bps": 8.7, "hit_rate": 0.5},
        },
    },
    "sim_per_quote": {"trades": 3764, "avg_net_bps": 1.11, "hit_rate": 0.36},
    "paper": {
        "balance": 49_982.37, "cash": 49_982.37, "holdings_value": 905.0,
        "pnl_cum": -17.63, "pnl_usd": -17.63, "trades": 197, "order_errors": 6,
        "reconciliations": 0, "sim_gap_bps": -4.81,
        "entry_slippage_bps": -1.3, "avg_round_trip_latency_s": 2.1,
        "open_detail": {
            "NVDA": {"side": "long", "qty": 1, "entry_fill": 899.10,
                     "mid": 905.0, "value": 905.0, "unrealized_usd": 5.90},
        },
        "recent": [
            {"symbol": "AAPL", "side": "short", "qty": 3, "entry_fill": 200.0,
             "exit_fill": 199.0, "pnl_usd": 3.0, "pred_bps": -12.0,
             "entry_slippage_bps": -0.8, "entry_latency_s": 1.7},
            {"symbol": "MSFT", "side": "short", "qty": -2, "entry_fill": 391.6,
             "exit_fill": 393.0, "pnl_usd": -2.8, "pred_bps": float("nan"),
             "reconciliation": True},
        ],
    },
}


def _write_status(root: Path, ts: float | None = None) -> None:
    (root / "data").mkdir(exist_ok=True)
    st = {**STATUS, "ts": ts if ts is not None else time.time()}
    (root / "data" / "status_stocks.json").write_text(json.dumps(st))


def test_cold_start_renders_without_raising(tmp_path):
    out = gsa.render(tmp_path)
    assert "<html" in out and "</html>" in out
    assert "OFFLINE" in out


def test_populated_render_has_no_leftover_placeholders(tmp_path):
    _write_status(tmp_path)
    out = gsa.render(tmp_path)
    assert not PLACEHOLDER.findall(out)


def test_populated_render_shows_blotter_positions_and_research(tmp_path):
    _write_status(tmp_path)
    out = gsa.render(tmp_path)
    assert "AAPL" in out and "NVDA" in out and "MSFT" in out
    assert "905.00" in out  # marked-to-market position value
    assert "reconciliation" in out.lower()  # flagged, not hidden
    assert "BACKTEST, not real trades" in out  # research clearly labelled


def test_populated_render_shows_entry_slippage_and_latency(tmp_path):
    """2026-07-21: 1-2.4s real Alpaca fill latency was found by manual
    server-side investigation of a handful of trades — must be visible on
    the dashboard, not something that has to be re-derived again."""
    _write_status(tmp_path)
    out = gsa.render(tmp_path)
    assert "entry slippage" in out.lower()
    assert "-1.30bps" in out  # aggregate entry_slippage_bps tile
    assert "2.1s" in out      # avg_round_trip_latency_s
    assert "-0.80" in out     # per-trade slippage in the blotter row
    assert "1.7s" in out      # per-trade latency in the blotter row


def test_render_is_idempotent_across_repeated_calls(tmp_path):
    _write_status(tmp_path)
    first = gsa.render(tmp_path)
    second = gsa.render(tmp_path)
    assert len(first) == len(second)


def test_history_accumulates_and_feeds_the_chart(tmp_path):
    now = time.time()
    _write_status(tmp_path, ts=now - 30)
    gsa.render(tmp_path)
    _write_status(tmp_path, ts=now)
    out = gsa.render(tmp_path)
    hist = gsa.load_history(tmp_path / "logs" / "stocks_app_history.jsonl")
    assert len(hist) == 2
    assert "book" in out  # chart series label present in the rendered SVG


def test_dashboard_button_url_shape():
    """The button opens INSIDE Telegram (web_app), not the system browser —
    confirmed structurally since this is what alerts scripts will attach."""
    from signals.telegram import dashboard_button
    btn = dashboard_button("https://contrafact.quest/app/x/stocks_app.html")
    assert "web_app" in btn["inline_keyboard"][0][0]
