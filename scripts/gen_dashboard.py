"""Generate the mobile monitoring dashboard (static HTML, stdlib only).

Reads the pipeline's data/status.json (written atomically by the live service —
never touches the single-writer DuckDB), the equities cron log, and disk state;
renders one self-contained page designed for a phone. Served by the existing
Caddy /stats route behind basic auth. Runs from cron every minute.

This is the read-only half of a future control plane: the page will later gain
inputs that write control.json (position caps, kill switch), which the pipeline
will read each status tick. Any control surface for real money needs stronger
auth than basic-auth-over-TLS — out of scope until paper proves an edge.

Usage:
    python scripts/gen_dashboard.py --out /srv/stats/trading.html
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import time
from pathlib import Path

# Status palette (dataviz skill reference; icons+labels always accompany color)
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"


def load_status(path: Path) -> tuple[dict, str, str]:
    """Returns (status, freshness_key, freshness_label)."""
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


def equities_summary(log_path: Path) -> tuple[str, str, str]:
    """(state_key, label, detail) from the recorder cron log."""
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


def render(root: Path) -> str:
    status, fresh_key, fresh_label = load_status(root / "data" / "status.json")
    colors = {"good": GOOD, "warning": WARNING, "critical": CRITICAL}
    icons = {"good": "&#9679;", "warning": "&#9650;", "critical": "&#9632;"}
    fresh_color = colors[fresh_key]

    eq_key, eq_label, eq_detail = equities_summary(root / "logs" / "equities_cron.log")

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
            tile("paper equity", f"${status.get('equity', 0):,.0f}", "account value"),
        ]
        for sym, m in status.get("per_symbol", {}).items():
            dir_val = m.get("dir")
            has_dir = isinstance(dir_val, float) and dir_val == dir_val
            dir_txt = f"{dir_val:.2f}" if has_dir else "–"
            tiles.append(tile(f"{html.escape(sym)} rolling dir", dir_txt,
                              f"n={m.get('n', 0):,.0f} learned (overlapping)"))

    dbs = sorted((root / "data").glob("*.duckdb"))
    db_rows = "".join(
        f"<tr><td>{html.escape(p.name)}</td>"
        f"<td class='num'>{p.stat().st_size / 1e6:,.0f} MB</td></tr>"
        for p in dbs
    )
    disk = shutil.disk_usage("/")
    disk_pct = disk.used / disk.total * 100

    generated = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    tiles_html = "".join(tiles) or '<div class="tile"><div class="val">no status</div></div>'
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
table {{ width:100%; border-collapse:collapse; background:var(--card);
         border:1px solid var(--line); border-radius:10px; overflow:hidden;
         font-size:.85rem; }}
td {{ padding:8px 12px; border-top:1px solid var(--line); }}
tr:first-child td {{ border-top:none; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
footer {{ margin-top:18px; font-size:.72rem; color:var(--muted); }}
</style></head><body>
<h1>intraday paper trading</h1>
<div class="fresh" style="color:{fresh_color}">{icons[fresh_key]} {fresh_label}</div>
<div class="grid">{tiles_html}</div>
<h2>equities recorder</h2>
<div class="banner" style="border-left:4px solid {colors[eq_key]}">
  <strong style="color:{colors[eq_key]}">{icons[eq_key]} {eq_label}</strong><br>{eq_detail}
</div>
<h2>storage</h2>
<table>{db_rows}
<tr><td>disk used</td><td class="num">{disk_pct:.0f}% of {disk.total / 1e9:.0f} GB</td></tr>
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
