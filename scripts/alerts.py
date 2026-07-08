"""Push alerts to Telegram (reuses the contrafact @ContrafactBot credentials).

Alert on STATE TRANSITIONS only — no spam: pipeline going OFFLINE/back LIVE,
circuit breaker tripping, order errors increasing, and new trades. A --daily
flag sends an unconditional one-line summary (separate cron, once a day).
State between runs lives in logs/alert_state.json. Stdlib only; cron every
5 minutes on the server.

Usage:
    python scripts/alerts.py --env /home/deploy/digest.env [--daily]
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


def load_env(path: str) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"')
    return out


def detect_alerts(prev: dict, cur: dict) -> list[str]:
    """Pure transition logic (unit-tested). prev/cur: condensed state dicts."""
    alerts = []
    if cur["fresh"] != prev.get("fresh") and prev:
        if cur["fresh"] == "offline":
            alerts.append("\N{LARGE RED SQUARE} pipeline OFFLINE — status stopped updating")
        elif prev.get("fresh") == "offline":
            alerts.append("\N{LARGE GREEN CIRCLE} pipeline back LIVE")
    if cur["breaker"] == "TRIPPED" and prev.get("breaker") != "TRIPPED":
        alerts.append(
            f"\N{LARGE RED SQUARE} circuit breaker TRIPPED — pnl today ${cur['pnl']:+.2f};"
            " no new entries until UTC rollover"
        )
    if cur["order_errors"] > prev.get("order_errors", 0):
        alerts.append(f"\N{WARNING SIGN} order errors: {cur['order_errors']} (was"
                      f" {prev.get('order_errors', 0)})")
    if cur["orders"] > prev.get("orders", 0):
        last = cur.get("last_order") or {}
        alerts.append(
            f"\N{CHART WITH UPWARDS TREND} trade: {last.get('side', '?')}"
            f" {last.get('qty', 0):.6g} {last.get('symbol', '?')}"
            f" @ {last.get('price', 0):,.2f} — {last.get('note', '')[:60]}"
        )
    return alerts


def condense(status: dict, age_s: float) -> dict:
    orders = status.get("recent_orders", [])
    return {
        "fresh": "offline" if age_s > 300 else "live",
        "breaker": status.get("breaker", "?"),
        "order_errors": status.get("order_errors", 0),
        "orders": status.get("orders", 0),
        "pnl": status.get("pnl_today", 0.0),
        "last_order": orders[-1] if orders else None,
    }


def send(creds: dict[str, str], text: str) -> None:
    data = urllib.parse.urlencode(
        {"chat_id": creds["TELEGRAM_CHAT_ID"], "text": f"intraday: {text}"}
    ).encode()
    url = f"https://api.telegram.org/bot{creds['TELEGRAM_BOT_TOKEN']}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=15) as resp:
        resp.read()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--env", default="/home/deploy/digest.env")
    parser.add_argument("--daily", action="store_true", help="send summary unconditionally")
    args = parser.parse_args()
    root = Path(args.root)
    creds = load_env(args.env)

    try:
        status = json.loads((root / "data" / "status.json").read_text())
        age = time.time() - status.get("ts", 0)
    except (OSError, ValueError):
        status, age = {}, 1e9
    cur = condense(status, age)

    state_path = root / "logs" / "alert_state.json"
    try:
        prev = json.loads(state_path.read_text())
    except (OSError, ValueError):
        prev = {}

    if args.daily:
        per_sym = status.get("per_symbol", {})
        learned = sum(m.get("n", 0) for m in per_sym.values())
        send(
            creds,
            f"daily: {cur['fresh'].upper()} · pnl ${cur['pnl']:+.2f}"
            f" · orders {cur['orders']} · {learned:,.0f} samples learned"
            f" · equity ${status.get('equity', 0):,.0f}",
        )
    else:
        for alert in detect_alerts(prev, cur):
            send(creds, alert)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(cur))


if __name__ == "__main__":
    main()
