"""The real-time trade-blotter formatting (stocks_live.py's on_fill hook)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from stocks_live import format_blotter_line  # noqa: E402


def test_entry_line_reports_what_and_why():
    line = format_blotter_line({
        "kind": "entry", "symbol": "NVDA", "side": "long", "qty": 1,
        "price": 900.10, "pred_bps": 14.5, "balance": 49_982.37,
    })
    assert "BOUGHT" in line and "NVDA" in line
    assert "900.10" in line and "+14.5bps" in line
    assert "stocks book $49,982.37" in line


def test_entry_line_labels_a_short_correctly():
    line = format_blotter_line({
        "kind": "entry", "symbol": "AAPL", "side": "short", "qty": 3,
        "price": 200.0, "pred_bps": -12.0, "balance": 50_000.0,
    })
    assert "SOLD SHORT" in line


def test_exit_line_reports_the_result_and_running_balance():
    line = format_blotter_line({
        "kind": "exit", "symbol": "NVDA", "side": "long", "qty": 1,
        "exit_fill": 900.55, "net_bps": 5.0, "pnl_usd": 0.45,
        "sim_net_bps": 3.2, "entry_slippage_bps": -1.1, "balance": 49_982.82,
    })
    assert "SOLD" in line and "900.55" in line
    assert "+5.00bps" in line and "+0.45" in line and "+3.20bps" in line
    assert "-1.10bps" in line  # entry slippage (2026-07-21)
    assert "stocks book $49,982.82" in line


def test_exit_line_labels_covering_a_short():
    line = format_blotter_line({
        "kind": "exit", "symbol": "AAPL", "side": "short", "qty": 3,
        "exit_fill": 199.0, "net_bps": 5.0, "pnl_usd": 3.0,
        "sim_net_bps": 2.0, "entry_slippage_bps": 0.5, "balance": 50_003.0,
    })
    assert "COVERED" in line


def test_reconciliation_line_is_flagged_and_distinct():
    line = format_blotter_line({
        "kind": "reconciliation", "symbol": "SPY", "side": "short", "qty": 2,
        "entry_fill": 744.99, "pnl_usd": -3.0, "balance": 49_997.0,
    })
    assert "RECONCILED" in line and "SPY" in line and "-3.00" in line


def test_symbol_is_html_escaped():
    """Defense in depth: a raw '<' anywhere in a Telegram HTML message 400s the
    whole send (2026-07-20 outage). Symbols never contain one in practice, but
    the formatter must not assume that."""
    line = format_blotter_line({
        "kind": "entry", "symbol": "A<B", "side": "long", "qty": 1,
        "price": 1.0, "pred_bps": 1.0, "balance": 1.0,
    })
    assert "A&lt;B" in line and "A<B" not in line
