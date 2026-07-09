"""Generate the mobile monitoring dashboard (static HTML + inline SVG, stdlib only).

Reads the pipeline's data/status.json (written atomically by the live service —
never touches the single-writer DuckDB), accumulates a per-minute history JSONL
for the charts, and renders one self-contained page designed for a phone.
Served by the existing Caddy /stats route behind basic auth; cron every minute.

This is the read-only half of a future control plane: the page will later gain
inputs that write control.json (position caps, kill switch), which the pipeline
will read each status tick. Any control surface for real money needs stronger
auth than basic-auth-over-TLS — out of scope until paper proves an edge.

Usage:
    python scripts/gen_dashboard.py --out /srv/stats/trading.html
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import shutil
import time
from pathlib import Path

# dataviz reference palette: status colors + categorical slots 1-2 (validated
# set, fixed order); colors are always paired with icon/label text.
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"
# Validated categorical slots 1-7, fixed order (never cycled beyond 7 series).
SERIES = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4"]

HISTORY_KEEP = 20_000  # ~2 weeks at 1/min
HISTORY_PRUNE_AT = 30_000


def load_status(path: Path) -> tuple[dict, str, str]:
    try:
        status = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}, "critical", "OFFLINE — no status file"
    age = time.time() - status.get("ts", 0)
    if age < 90:
        return status, "good", f"LIVE — updated {age:.0f}s ago"
    if age < 300:
        return status, "warning", f"STALE — {age / 60:.1f}m since update"
    return status, "critical", f"OFFLINE — {age / 3600:.1f}h since update"


def append_history(hist_path: Path, status: dict) -> None:
    """One JSONL snapshot per cron tick; pruned so the file never grows unbounded."""
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
        slim = {
            "ts": status.get("ts"),
            "equity": status.get("equity"),
            "pnl_today": status.get("pnl_today"),
            "events": status.get("events"),
            "dir": {s: m.get("dir") for s, m in status.get("per_symbol", {}).items()},
            "edge": {s: m.get("edge_pct") for s, m in status.get("per_symbol", {}).items()},
        }
        with hist_path.open("a") as fh:
            fh.write(json.dumps(slim) + "\n")


def load_history(hist_path: Path, window_s: float = 86_400) -> list[dict]:
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


def svg_chart(
    series: dict[str, list[tuple[float, float]]],
    height: int = 150,
    value_format: str = "{:,.2f}",
) -> str:
    """Minimal multi-series line chart: recessive grid, 2px strokes, direct
    end labels; native <title> hover on the last point of each series."""
    points = [p for pts in series.values() for p in pts]
    if len(points) < 3:
        return (
            '<div class="banner sub">collecting history — '
            "chart appears after a few minutes</div>"
        )
    width = 600
    pad_l, pad_r, pad_t, pad_b = 8, 96, 10, 18
    t0 = min(p[0] for p in points)
    t1 = max(p[0] for p in points)
    v0 = min(p[1] for p in points)
    v1 = max(p[1] for p in points)
    if t1 == t0:
        t1 = t0 + 1
    if v1 == v0:
        v0, v1 = v0 - 1, v1 + 1
    if "$" in value_format and (v1 - v0) < 50:
        # tiny dollar spans: whole-dollar labels would all render identically
        value_format = "${:,.2f}"
    span_v = v1 - v0
    v0 -= span_v * 0.08
    v1 += span_v * 0.08

    def x(t: float) -> float:
        return pad_l + (t - t0) / (t1 - t0) * (width - pad_l - pad_r)

    def y(v: float) -> float:
        return pad_t + (1 - (v - v0) / (v1 - v0)) * (height - pad_t - pad_b)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'style="width:100%;height:auto;display:block">'
    ]
    for frac, val in ((0.0, v1), (0.5, (v0 + v1) / 2), (1.0, v0)):
        gy = pad_t + frac * (height - pad_t - pad_b)
        parts.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}"'
            f' stroke="var(--line)" stroke-width="1"/>'
            f'<text x="{width - pad_r + 6}" y="{gy + 3.5:.1f}" class="axis">'
            f"{value_format.format(val)}</text>"
        )
    for i, (label, pts) in enumerate(series.items()):
        if not pts:
            continue
        color = SERIES[i % len(SERIES)]
        path = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in pts)
        lt, lv = pts[-1]
        parts.append(
            f'<polyline points="{path}" fill="none" stroke="{color}"'
            f' stroke-width="2" stroke-linejoin="round"/>'
            f'<circle cx="{x(lt):.1f}" cy="{y(lv):.1f}" r="3.5" fill="{color}">'
            f"<title>{html.escape(label)}: {value_format.format(lv)}</title></circle>"
        )
        if len(series) > 1:  # direct end labels only when there is >1 series
            parts.append(
                f'<text x="{x(lt) + 7:.1f}" y="{y(lv) + 3.5:.1f}" class="axis"'
                f' fill="{color}">{html.escape(label)}</text>'
            )
    hours = (t1 - t0) / 3600
    parts.append(
        f'<text x="{pad_l}" y="{height - 4}" class="axis">last {hours:.1f}h</text></svg>'
    )
    return "".join(parts)


def equities_summary(log_path: Path) -> tuple[str, str, str]:
    try:
        lines = log_path.read_text().strip().splitlines()[-3:]
    except OSError:
        return "warning", "NO LOG", "recorder has not run yet"
    last = lines[-1] if lines else ""
    if last.startswith("done:"):
        return "good", "SESSION DONE", html.escape(last)
    if last.startswith("[+"):
        return "good", "RECORDING", html.escape(last)
    return "warning", "CHECK LOG", html.escape(last[-160:])


def tile(label: str, value: str, sub: str = "", color: str = "") -> str:
    style = f"color:{color}" if color else ""
    return (
        f'<div class="tile"><div class="lbl">{label}</div>'
        f'<div class="val" style="{style}">{value}</div>'
        f'<div class="sub">{sub}</div></div>'
    )


def orders_section(status: dict) -> str:
    orders = status.get("recent_orders", [])
    if not orders:
        return (
            '<div class="banner sub">No orders since service start. The policy only'
            " trades when a prediction clears fees + half-spread + dead-zone"
            " (~10&ndash;12 bps on crypto) &mdash; abstaining is the designed behaviour"
            " while no edge is proven. Earlier paper-equity dips came from order-path"
            " validation trades and spread costs, not this run.</div>"
        )
    rows = "".join(
        f"<tr><td>{time.strftime('%d %b %H:%M', time.gmtime(o.get('ts', 0)))}</td>"
        f"<td>{html.escape(o.get('symbol', ''))}</td>"
        f"<td>{html.escape(o.get('side', ''))}</td>"
        f"<td class='num'>{o.get('qty', 0):.6g}</td>"
        f"<td class='num'>{o.get('price', 0):,.2f}</td>"
        f"<td>{html.escape(str(o.get('note', ''))[:60])}</td></tr>"
        for o in reversed(orders)
    )
    return (
        "<table><tr><td>time (UTC)</td><td>symbol</td><td>side</td>"
        f"<td class='num'>qty</td><td class='num'>fill</td><td>note</td></tr>{rows}</table>"
    )


def fmt(value, spec: str = "{:.2f}") -> str:  # type: ignore[no-untyped-def]
    if value is None or value != value:
        return "–"
    return spec.format(value)


def diagnostics_table(status: dict) -> str:
    per = status.get("per_symbol", {})
    if not per:
        return '<div class="banner sub">no model state yet</div>'
    rows = "".join(
        f"<tr><td>{html.escape(sym)}</td>"
        f"<td class='num'>{fmt(m.get('rolling_n'), '{:,.0f}')}</td>"
        f"<td class='num'>{fmt(m.get('commit_rate'), '{:.0%}')}</td>"
        f"<td class='num'>{fmt(m.get('bias_bps'), '{:+.2f}')}</td>"
        f"<td class='num'>{fmt(m.get('resid_std_bps'))}</td>"
        f"<td class='num'>{fmt(m.get('r2'), '{:+.3f}')}</td>"
        f"<td class='num'>{fmt(m.get('edge_pct'), '{:+.1f}%')}</td>"
        f"<td class='num'>{fmt(m.get('coverage'), '{:.0%}')}</td></tr>"
        for sym, m in per.items()
    )
    return (
        "<table><tr><td>symbol</td><td class='num'>n</td><td class='num'>commit</td>"
        "<td class='num'>bias</td><td class='num'>resid σ</td>"
        "<td class='num'>R²</td><td class='num'>edge</td>"
        "<td class='num'>q-cov</td></tr>"
        f"{rows}</table>"
        '<div class="sub" style="margin-top:4px">rolling ≤2000 labels, bps. '
        "bias→0 &amp; R²&gt;0 = predictions carry info; commit = fraction of "
        "quotes acted on; q-cov→50% = EV quantile interval calibrated.</div>"
    )


def calibration_scatter(pairs: list, label: str, size: int = 170) -> str:
    if len(pairs) < 20:
        return ""
    values = sorted(abs(v) for p in pairs for v in p)
    lim = max(values[int(len(values) * 0.95)], 0.5)

    def sc(v: float) -> float:
        clipped = max(-lim, min(lim, v))
        return size / 2 + (clipped / lim) * (size / 2 - 12)

    dots = "".join(
        f'<circle cx="{sc(p):.1f}" cy="{size - sc(t):.1f}" r="2.5" fill="{SERIES[0]}"'
        ' fill-opacity="0.55"/>'
        for p, t in pairs
    )
    mid = size / 2
    return (
        f'<div class="scatter"><svg viewBox="0 0 {size} {size}" role="img">'
        f'<line x1="12" y1="{size - 12}" x2="{size - 12}" y2="12"'
        ' stroke="var(--muted)" stroke-width="1" stroke-dasharray="4 3"/>'
        f'<line x1="{mid}" y1="0" x2="{mid}" y2="{size}" stroke="var(--line)"/>'
        f'<line x1="0" y1="{mid}" x2="{size}" y2="{mid}" stroke="var(--line)"/>'
        f"{dots}"
        f'<text x="4" y="12" class="axis">±{lim:.1f}bps</text></svg>'
        f'<div class="sub" style="text-align:center">{html.escape(label)}'
        f" ({len(pairs)})</div></div>"
    )


def research_section(root: Path) -> str:
    """Latest nightly auto-research results, or the static fallback."""
    try:
        payload = json.loads((root / "logs" / "research_latest.json").read_text())
        age_h = (time.time() - payload.get("ts", 0)) / 3600
        if age_h > 48:
            raise ValueError("stale")
    except (OSError, ValueError):
        return (
            '<div class="banner sub"><strong>research:</strong> nightly evaluation has '
            "not run yet; first table appears after the 04:30 UTC cron.</div>"
        )
    rows = "".join(
        (
            f"<tr><td>{html.escape(r['symbol'])}{' +CB' if r.get('leader') else ''}</td>"
            f"<td class='num'>{r.get('n') or 0}</td>"
            f"<td class='num'>{r.get('dir') if r.get('dir') is not None else '–'}</td>"
            f"<td class='num'>{r.get('d_best') if r.get('d_best') is not None else '–'}</td>"
            f"<td class='num'>{r.get('trades', 0)}</td>"
            f"<td class='num'>{r.get('net_bps', 0):+.0f}</td></tr>"
        )
        if "error" not in r
        else f"<tr><td>{html.escape(r['symbol'])}</td><td colspan='5'>error: "
             f"{html.escape(r['error'])}</td></tr>"
        for r in payload.get("results", [])
    )
    when = time.strftime("%d %b %H:%M UTC", time.gmtime(payload.get("ts", 0)))
    return (
        f"<table><tr><td>pair</td><td class='num'>n</td><td class='num'>dir</td>"
        f"<td class='num'>d-best</td><td class='num'>trades</td>"
        f"<td class='num'>net bps</td></tr>{rows}</table>"
        f'<div class="sub" style="margin-top:4px">nightly walk-forward eval '
        f'({html.escape(str(payload.get("model")))} @ {payload.get("horizon_s")}s, '
        f"independent windows, cost-charged sim) — {when}. "
        f"d-best &gt; 0 = beats every naive baseline.</div>"
    )


def render(root: Path) -> str:
    status, fresh_key, fresh_label = load_status(root / "data" / "status.json")
    colors = {"good": GOOD, "warning": WARNING, "critical": CRITICAL}
    icons = {"good": "&#9679;", "warning": "&#9650;", "critical": "&#9632;"}

    hist_path = root / "logs" / "status_history.jsonl"
    append_history(hist_path, status)
    history = load_history(hist_path)

    equity_pts = [(r["ts"], r["equity"]) for r in history if r.get("equity")]
    dir_series: dict[str, list[tuple[float, float]]] = {}
    for row in history:
        for sym, d in (row.get("dir") or {}).items():
            if isinstance(d, float) and d == d:
                dir_series.setdefault(sym, []).append((row["ts"], d))

    eq_key, eq_label, eq_detail = equities_summary(root / "logs" / "equities_cron.log")

    scatters = "".join(
        calibration_scatter(m.get("pairs") or [], sym)
        for sym, m in status.get("per_symbol", {}).items()
    )
    edge_series: dict[str, list[tuple[float, float]]] = {}
    for row in history:
        for sym, e in (row.get("edge") or {}).items():
            if isinstance(e, (int, float)) and e == e:
                edge_series.setdefault(sym, []).append((row["ts"], e))
    scatter_placeholder = (
        '<div class="banner sub">appears after ≥20 committed predictions/symbol</div>'
    )

    tiles = []
    if status:
        pnl = status.get("pnl_today", 0.0)
        breaker = status.get("breaker", "?")
        tiles += [
            tile("uptime", f"{status.get('uptime_s', 0) / 3600:.1f}h",
                 f"{status.get('model', '')} on {' '.join(status.get('symbols', []))}"),
            tile("events", f"{status.get('events', 0):,}",
                 f"reconnects {status.get('reconnects', 0)}"
                 f" · tap drops {status.get('tap_dropped', 0)}"),
            tile("decision latency", f"{status.get('proc_us_p50', 0):.0f}µs",
                 f"p99 {status.get('proc_us_p99', 0):.0f}µs (budget 15ms)"),
            tile("open positions", str(status.get("open_positions", 0)),
                 f"orders {status.get('orders', 0)} · errors {status.get('order_errors', 0)}"),
            tile("PnL today", f"${pnl:+,.2f}", "realized, paper",
                 GOOD if pnl >= 0 else CRITICAL),
            tile("circuit breaker", ("OK" if breaker == "ok" else "&#9632; TRIPPED"),
                 "daily loss limit",
                 GOOD if breaker == "ok" else CRITICAL),
        ]

    dbs = sorted((root / "data").glob("*.duckdb"))
    db_rows = "".join(
        f"<tr><td>{html.escape(p.name)}</td>"
        f"<td class='num'>{p.stat().st_size / 1e6:,.0f} MB</td></tr>"
        for p in dbs
    )
    disk = shutil.disk_usage("/")

    generated = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    tiles_html = "".join(tiles) or '<div class="tile"><div class="val">no status</div></div>'
    equity_now = f"${status.get('equity', 0):,.2f}" if status else "–"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>intraday paper trading</title>
<style>
:root {{ --bg:#fcfcfb; --ink:#1a1a19; --muted:#6b6b68; --card:#ffffff; --line:#e5e5e2; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#1a1a19; --ink:#f2f2ef; --muted:#a3a39e; --card:#242422; --line:#3a3a37; }}
}}
* {{ box-sizing:border-box; margin:0; }}
body {{ font:16px/1.45 -apple-system,system-ui,sans-serif; background:var(--bg);
       color:var(--ink); padding:16px; max-width:640px; margin:0 auto; }}
h1 {{ font-size:1.15rem; margin-bottom:2px; }}
.fresh {{ font-weight:600; margin-bottom:16px; }}
.grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:12px; }}
.lbl {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.04em;
        color:var(--muted); }}
.val {{ font-size:1.45rem; font-weight:650; margin:2px 0; font-variant-numeric:tabular-nums; }}
.sub {{ font-size:.75rem; color:var(--muted); }}
h2 {{ font-size:.85rem; text-transform:uppercase; letter-spacing:.04em;
      color:var(--muted); margin:20px 0 8px; }}
.banner {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:12px; font-size:.85rem; }}
.chart {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:12px 6px 6px; }}
.scatter-row {{ display:flex; flex-wrap:wrap; gap:10px; }}
.scatter {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:8px; flex:1 1 150px; max-width:200px; }}
.axis {{ font:11px -apple-system,system-ui,sans-serif; fill:var(--muted); }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
         border:1px solid var(--line); border-radius:10px; overflow:hidden;
         font-size:.8rem; }}
td {{ padding:7px 10px; border-top:1px solid var(--line); }}
tr:first-child td {{ border-top:none; color:var(--muted); font-size:.72rem;
                     text-transform:uppercase; letter-spacing:.04em; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
footer {{ margin-top:18px; font-size:.72rem; color:var(--muted); }}
</style></head><body>
<h1>intraday paper trading</h1>
<div class="fresh" style="color:{colors[fresh_key]}">{icons[fresh_key]} {fresh_label}</div>
<div class="grid">{tiles_html}</div>

<h2>paper equity — {equity_now}</h2>
<div class="chart">{svg_chart({"equity": equity_pts}, value_format="${:,.0f}")}</div>

<h2>rolling directional accuracy (overlapping, per symbol)</h2>
<div class="chart">{svg_chart(dir_series, value_format="{:.2f}")}</div>

<h2>model diagnostics (rolling)</h2>
{diagnostics_table(status)}

<h2>calibration: predicted vs realized (dashed = perfect)</h2>
<div class="scatter-row">{scatters or scatter_placeholder}</div>

<h2>rolling MAE edge vs zero baseline</h2>
<div class="chart">{svg_chart(edge_series, value_format="{{:+.1f}}%")}</div>

<h2>orders (crypto pipeline — the only experiment that trades)</h2>
{orders_section(status)}

<h2>all experiments</h2>
<table>
<tr><td>experiment</td><td>where</td><td>trades?</td></tr>
<tr><td>crypto paper pipeline (BTC+ETH, classifier)</td><td>server, 24/7</td>
<td>yes — paper</td></tr>
<tr><td>equities capture (SPY/AAPL/NVDA)</td><td>server cron, weekdays 14:30 UTC</td>
<td>no — data only</td></tr>
<tr><td>cross-venue &amp; research replays</td><td>offline (Mac), on recorded data</td>
<td>no — simulation</td></tr>
</table>
<div class="banner sub" style="margin-top:8px"><strong>equities recorder now:</strong>
<span style="color:{colors[eq_key]}">{icons[eq_key]} {eq_label}</span> — {eq_detail}<br>
<strong>research:</strong> see nightly table below. Details: docs/RESEARCH.md.</div>
<h2>nightly research (auto)</h2>
{research_section(root)}

<h2>storage (server)</h2>
<table>{db_rows}
<tr><td>disk used</td><td class="num">{disk.used / disk.total * 100:.0f}%
 of {disk.total / 1e9:.0f} GB</td></tr>
</table>
<footer>generated {generated} · auto-refreshes every 60s ·
read-only monitor; control plane comes later</footer>
</body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repo root (has data/, logs/)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(Path(args.root)))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
