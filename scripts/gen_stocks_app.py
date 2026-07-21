"""Generate the stocks Telegram Web App dashboard — the interactive chart
Telegram has no plugin surface for otherwise. A message can only ever carry
static text/images; a Web App button opens a real HTML/JS page inside
Telegram's own webview with one tap, which is the actual ceiling of what the
platform allows for something that feels like "a chart in Telegram."

Three sections, matching the three professional trading reports this project
was missing (2026-07-20 review): a BLOTTER (what got traded, why), a POSITION
report (what's held now, marked to market, cash/holdings/total), and a
RESEARCH panel (windowed/per-quote backtest numbers) — clearly separated so
"is there an edge" and "what did the real book do" are never the same number.

Reads ONLY status_stocks.json + stocks_book.json (never the DuckDB, which the
capture holds open as the single writer). Served by a dedicated no-auth,
unguessable-path Caddy route (see docs/DEPLOY.md) — NOT /stats/*, since a
Web App opened from inside Telegram's webview can't cleanly satisfy an HTTP
basic-auth prompt.

Usage:
    python scripts/gen_stocks_app.py --out /srv/stats/stocks_app.html
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_dashboard import (  # noqa: E402
    CRITICAL,
    GOOD,
    WARNING,
    fmt,
    page_shell,
    svg_chart,
    tile,
)

HISTORY_KEEP = 20_000
HISTORY_PRUNE_AT = 30_000


def load_status(path: Path) -> tuple[dict, str, str]:
    try:
        status = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}, "critical", "OFFLINE — no capture running"
    age = time.time() - status.get("ts", 0)
    if age < 90:
        return status, "good", f"LIVE — updated {age:.0f}s ago"
    if age < 300:
        return status, "warning", f"STALE — {age / 60:.1f}m since update"
    return status, "critical", f"OFFLINE — {age / 3600:.1f}h since update"


def append_history(hist_path: Path, status: dict) -> None:
    """One snapshot per render tick, tracking the STOCKS BOOK (not the shadow
    sim) — this is what the equity-curve chart is built from."""
    if not status:
        return
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    last_ts = None
    if hist_path.exists():
        lines = hist_path.read_text().splitlines()
        if lines:
            with contextlib.suppress(ValueError):
                last_ts = json.loads(lines[-1]).get("ts")
        if len(lines) > HISTORY_PRUNE_AT:
            hist_path.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n")
    if status.get("ts") != last_ts:
        paper = status.get("paper") or {}
        slim = {
            "ts": status.get("ts"),
            "balance": paper.get("balance"),
            "trades": paper.get("trades"),
            "pnl_usd": paper.get("pnl_usd"),
            "sim_gap_bps": paper.get("sim_gap_bps"),
        }
        with hist_path.open("a") as fh:
            fh.write(json.dumps(slim) + "\n")


def load_history(hist_path: Path, window_s: float = 5 * 86_400) -> list[dict]:
    try:
        lines = hist_path.read_text().splitlines()
    except OSError:
        return []
    cutoff = time.time() - window_s
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("ts", 0) >= cutoff:
            out.append(row)
    return out


def blotter_table(trades: list[dict]) -> str:
    """One line per fill: side, qty, price, $ P&L, the WHY (the prediction
    that triggered entry), and the entry slippage/latency (2026-07-21: real
    Alpaca fill latency of 1-2.4s was found to be corrupting the sim
    comparison — now measured and shown per trade) — the report a real
    trading desk expects, which nothing in this project had until now."""
    if not trades:
        return '<div class="banner sub">no trades yet this session</div>'
    rows = []
    for t in reversed(trades):
        tag = ""
        if t.get("reconciliation"):
            tag = ' <span class="sub">(reconciliation — stranded position)</span>'
        pnl = t.get("pnl_usd", 0.0)
        color = GOOD if pnl >= 0 else CRITICAL
        pred = t.get("pred_bps")
        why = f"pred {pred:+.1f}bps" if isinstance(pred, (int, float)) and pred == pred else "—"
        lat = t.get("entry_latency_s")
        lat_s = f"{lat:.1f}s" if isinstance(lat, (int, float)) and lat == lat else "—"
        rows.append(
            f"<tr><td>{html.escape(t.get('symbol', ''))}</td>"
            f"<td>{html.escape(t.get('side', ''))}</td>"
            f"<td class='num'>{t.get('qty', 0):.6g}</td>"
            f"<td class='num'>{fmt(t.get('entry_fill'), '{:,.2f}')}</td>"
            f"<td class='num'>{fmt(t.get('exit_fill'), '{:,.2f}')}</td>"
            f"<td class='num' style='color:{color}'>{pnl:+,.2f}</td>"
            f"<td class='num'>{fmt(t.get('entry_slippage_bps'), '{:+.2f}')}</td>"
            f"<td class='num'>{lat_s}</td>"
            f"<td>{why}{tag}</td></tr>"
        )
    return (
        "<table><tr><td>symbol</td><td>side</td><td class='num'>qty</td>"
        "<td class='num'>entry</td><td class='num'>exit</td>"
        "<td class='num'>P&amp;L $</td><td class='num'>slip bps</td>"
        f"<td class='num'>latency</td><td>why</td></tr>{''.join(rows)}</table>"
    )


def position_table(open_detail: dict) -> str:
    """Current holdings, MARKED TO the latest quote — what a position report
    shows, not just a trade history."""
    if not open_detail:
        return '<div class="banner sub">flat — no open positions</div>'
    rows = "".join(
        f"<tr><td>{html.escape(sym)}</td><td>{html.escape(d['side'])}</td>"
        f"<td class='num'>{d['qty']:.6g}</td>"
        f"<td class='num'>{d['entry_fill']:,.2f}</td>"
        f"<td class='num'>{d['mid']:,.2f}</td>"
        f"<td class='num'>{d['value']:,.2f}</td>"
        f"<td class='num' style='color:{GOOD if d['unrealized_usd'] >= 0 else CRITICAL}'>"
        f"{d['unrealized_usd']:+,.2f}</td></tr>"
        for sym, d in sorted(open_detail.items())
    )
    return (
        "<table><tr><td>symbol</td><td>side</td><td class='num'>qty</td>"
        "<td class='num'>entry</td><td class='num'>now</td>"
        "<td class='num'>value</td><td class='num'>unrealized</td></tr>"
        f"{rows}</table>"
    )


def research_table(status: dict) -> str:
    """The backtest numbers (windowed/per-quote), clearly labelled as NOT the
    real book — this project's recurring failure mode was these two kinds of
    number bleeding into each other."""
    sim = status.get("sim", {})
    pq = status.get("sim_per_quote", {})
    by_symbol = sim.get("by_symbol", {})
    if not by_symbol:
        return '<div class="banner sub">no backtest signal yet</div>'
    rows = "".join(
        f"<tr><td>{html.escape(sym)}</td><td class='num'>{d.get('trades', 0)}</td>"
        f"<td class='num'>{d.get('avg_net_bps', 0.0):+.2f}</td>"
        f"<td class='num'>{d.get('hit_rate', float('nan')) * 100:.0f}%</td></tr>"
        for sym, d in sorted(by_symbol.items(), key=lambda kv: -kv[1].get("trades", 0))[:10]
    )
    return f"""<div class="banner sub" style="margin-bottom:8px">
<strong>BACKTEST, not real trades</strong> — windowed samples one signal per
5s window per symbol (the cadence every RESEARCH.md number was measured at);
per-quote acts on every signal (~3&times; more trades, the honest live rule).
Real fills are the blotter above.</div>
<table><tr><td>windowed</td><td class="num">{sim.get('trades', 0)} tr</td>
<td class="num">{sim.get('avg_net_bps', 0.0):+.2f}bps avg</td>
<td class="num">{sim.get('hit_rate', float('nan')) * 100:.0f}% hit</td></tr>
<tr><td>per-quote</td><td class="num">{pq.get('trades', 0)} tr</td>
<td class="num">{pq.get('avg_net_bps', 0.0):+.2f}bps avg</td>
<td class="num">{pq.get('hit_rate', float('nan')) * 100:.0f}% hit</td></tr></table>
<h2>by symbol (windowed, top 10 by trade count)</h2>
<table><tr><td>symbol</td><td class="num">tr</td><td class="num">avg bps</td>
<td class="num">hit</td></tr>{rows}</table>"""


def render(root: Path) -> str:
    status, fresh_key, fresh_label = load_status(root / "data" / "status_stocks.json")
    colors = {"good": GOOD, "warning": WARNING, "critical": CRITICAL}
    icons = {"good": "&#9679;", "warning": "&#9650;", "critical": "&#9632;"}

    hist_path = root / "logs" / "stocks_app_history.jsonl"
    append_history(hist_path, status)
    history = load_history(hist_path)
    equity_pts = [(r["ts"], r["balance"]) for r in history if r.get("balance")]

    paper = status.get("paper") or {}
    trades_html = blotter_table(paper.get("recent", []))
    position_html = position_table(paper.get("open_detail", {}))
    research_html = research_table(status)

    balance = paper.get("balance", 50_000.0)
    tiles = [
        tile("stocks book", f"${balance:,.2f}",
             f"cash ${paper.get('cash', balance):,.0f} + holdings "
             f"${paper.get('holdings_value', 0.0):,.0f}"),
        tile("cumulative P&amp;L", f"${paper.get('pnl_cum', 0.0):+,.2f}",
             "since the 2026-07-18 split",
             GOOD if paper.get("pnl_cum", 0.0) >= 0 else CRITICAL),
        tile("real trades", str(paper.get("trades", 0)),
             f"errors {paper.get('order_errors', 0)} · "
             f"reconciliations {paper.get('reconciliations', 0)}"),
        tile("vs-sim gap", f"{paper.get('sim_gap_bps', float('nan')):+.2f}bps",
             "real fill net minus the backtest's assumed net (same window, "
             "fixed 2026-07-21)"),
        tile("entry slippage", f"{paper.get('entry_slippage_bps', float('nan')):+.2f}bps",
             f"signal→fill delay cost · avg round-trip "
             f"{paper.get('avg_round_trip_latency_s', float('nan')):.1f}s"),
        tile("open positions", str(len(paper.get('open_detail', {}))),
             f"max {status.get('config', '')[:24]}"),
        tile("capture", f"{status.get('events', 0):,}",
             f"reconnects {status.get('reconnects', 0)} · "
             f"queue {status.get('q_hwm', 0):,}"),
    ]

    generated = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    body = f"""<h1>stocks paper trading</h1>
<div class="fresh" style="color:{colors[fresh_key]}">{icons[fresh_key]} {fresh_label}</div>
<div class="grid">{"".join(tiles)}</div>

<h2>stocks book — ${balance:,.2f}</h2>
<div class="chart">{svg_chart({"book": equity_pts}, value_format="${:,.0f}")}</div>

<h2>blotter — real fills, most recent first</h2>
{trades_html}

<h2>positions — marked to market</h2>
{position_html}

<h2>research (backtest)</h2>
{research_html}

<footer>generated {generated} · auto-refreshes every 30s ·
paper money only; real order fills on the Alpaca paper API</footer>"""
    return page_shell("stocks paper trading", body, refresh_s=30)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.write_text(render(Path(args.root)))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
