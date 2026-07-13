"""Live Telegram alerts for the equities session (@ContrafactBot).

Crypto has live alerts (alerts.py, off the pipeline's status.json). Equities had
NOTHING live — only the nightly digest after the close. On 2026-07-13 the 30-symbol
capture spent five hours in a writer death-spiral (queue pinned at its cap, 34
websocket reconnects, rows_written frozen) and nobody knew until it was looked at by
hand. These are the alerts that would have caught it in five minutes.

Reads ONLY the capture's log line — never the DuckDB, which the recorder holds open
as the single writer:
    [+  N s] events=E rows_written=R q_hwm=Q reconnects=C

Transition-based (no spam), plus a periodic heartbeat so silence is meaningful:
  📈 session open        capture up, N symbols
  🔴 capture DOWN        in-session but no recorder process
  🔴 writer STALLED      rows_written frozen while events climb  <-- the 07-13 failure
  ⚠️  queue saturating   backlog near the cap (producer about to block the WS)
  ⚠️  reconnects         websocket dropping (usually the symptom of a stalled writer)
  🟢 recovered           back to healthy after any of the above
  🏁 session close       summary: events, rows, dropped, reconnects, DB size
  💓 heartbeat           hourly: event rate + health, so you know it's alive

Cron (MERGE), every 5 min on weekdays; it no-ops outside market hours:
    */5 * * * 1-5 cd $HOME/intraday-ml-signals && .venv/bin/python \
        scripts/stocks_alerts.py --root . --env $HOME/digest.env \
        >> logs/stocks_alerts_cron.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
import zoneinfo
from datetime import datetime
from pathlib import Path

QUEUE_CAP = 250_000          # record.py's asyncio.Queue maxsize
QUEUE_WARN = 0.5             # warn once the backlog passes this fraction of the cap
HEARTBEAT_MIN = 60           # minutes between "still alive" messages
LOG_LINE = re.compile(
    r"\[\+\s*(\d+)s\]\s+events=(\d+)\s+rows_written=(\d+)\s+q_hwm=(\d+)\s+reconnects=(\d+)"
)
NY = zoneinfo.ZoneInfo("America/New_York")


def load_env(path: str) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"')
    return out


def send(creds: dict[str, str], text: str) -> None:
    data = urllib.parse.urlencode({
        "chat_id": creds["TELEGRAM_CHAT_ID"], "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{creds['TELEGRAM_BOT_TOKEN']}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=15) as resp:
        resp.read()


def in_session(now: datetime) -> bool:
    """US regular hours, in the exchange's own timezone (DST-proof)."""
    et = now.astimezone(NY)
    if et.weekday() > 4:
        return False
    return (et.hour, et.minute) >= (9, 30) and (et.hour, et.minute) < (16, 0)


def capture_alive() -> bool:
    return subprocess.run(
        ["pgrep", "-f", "record.py --market stocks"],
        capture_output=True, check=False,
    ).returncode == 0


def read_log(root: Path) -> dict | None:
    """Last progress line from the capture log."""
    try:
        lines = (root / "logs" / "equities_cron.log").read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines[-40:]):
        m = LOG_LINE.search(line)
        if m:
            up, events, rows, hwm, recon = (int(g) for g in m.groups())
            return {"up_s": up, "events": events, "rows": rows,
                    "q_hwm": hwm, "reconnects": recon}
    return None


def db_size_mb(root: Path) -> float:
    dbs = sorted((root / "data").glob("equities_2*.duckdb"))
    return dbs[-1].stat().st_size / 1e6 if dbs else 0.0


def detect(prev: dict, cur: dict) -> list[str]:
    """Pure transition logic (unit-tested). prev/cur are condensed states."""
    out: list[str] = []
    was, now = prev.get("state"), cur["state"]

    if now == "open" and was in (None, "closed"):
        out.append("📈 <b>stocks session open</b> — capture up, 30 symbols streaming")
    elif now == "closed" and was in ("open", "down", "stalled"):
        out.append(
            f"🏁 <b>stocks session close</b>\n"
            f"events {cur['events']:,} · rows {cur['rows']:,} · "
            f"unwritten {max(0, cur['events'] - cur['rows']):,}\n"
            f"reconnects {cur['reconnects']} · peak queue {cur['q_hwm']:,}/{QUEUE_CAP:,} · "
            f"db {cur['db_mb']:.0f}MB"
        )
        return out  # close summary stands alone

    if now == "down" and was != "down":
        out.append("🔴 <b>stocks capture DOWN</b> — no recorder process during market hours")
    if now == "stalled" and was != "stalled":
        out.append(
            f"🔴 <b>stocks writer STALLED</b> — rows frozen at {cur['rows']:,} "
            f"while events climb ({cur['events']:,}). The queue will fill and the "
            f"websocket will be dropped. Data is being lost."
        )
    if now == "open" and was in ("down", "stalled"):
        out.append("🟢 <b>stocks capture recovered</b> — writer keeping up again")

    # warnings (only on the way up, so they don't repeat every 5 min)
    if cur["q_hwm"] >= QUEUE_CAP * QUEUE_WARN > prev.get("q_hwm", 0):
        out.append(
            f"⚠️ stocks queue backlog {cur['q_hwm']:,}/{QUEUE_CAP:,} — writer falling "
            f"behind; the producer will block the websocket if it hits the cap"
        )
    if cur["reconnects"] > prev.get("reconnects", 0):
        out.append(
            f"⚠️ stocks websocket reconnects: {cur['reconnects']} "
            f"(was {prev.get('reconnects', 0)}) — usually a stalled writer"
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--env", default="/home/deploy/digest.env")
    ap.add_argument("--no-send", action="store_true", help="print instead of Telegram")
    args = ap.parse_args()
    root = Path(args.root)

    now = datetime.now(tz=NY)
    log = read_log(root) or {"up_s": 0, "events": 0, "rows": 0, "q_hwm": 0, "reconnects": 0}
    alive = capture_alive()

    state_path = root / "logs" / "stocks_alert_state.json"
    try:
        prev = json.loads(state_path.read_text())
    except (OSError, ValueError):
        prev = {}

    if not in_session(now):
        state = "closed"
    elif not alive:
        state = "down"
    elif log["events"] > prev.get("events", 0) and log["rows"] == prev.get("rows", -1):
        state = "stalled"  # events climbing but nothing written since last check
    else:
        state = "open"

    cur = {**log, "state": state, "db_mb": db_size_mb(root), "ts": time.time(),
           "last_beat": prev.get("last_beat", 0)}

    msgs = detect(prev, cur)

    # heartbeat so silence is informative, not ambiguous
    if state == "open" and time.time() - prev.get("last_beat", 0) > HEARTBEAT_MIN * 60:
        secs = max(1, log["up_s"])
        msgs.append(
            f"💓 stocks capture healthy — {log['events']:,} events "
            f"({log['events'] / secs:,.0f}/s) · rows {log['rows']:,} · "
            f"queue {log['q_hwm']:,} · reconnects {log['reconnects']} · "
            f"db {cur['db_mb']:.0f}MB"
        )
        cur["last_beat"] = time.time()

    if args.no_send:  # a dry run must NOT consume the transitions it is previewing
        for m in msgs:
            print(m)
        if not msgs:
            print(f"{now:%H:%M} {state}: no alerts")
        return

    for m in msgs:
        send(load_env(args.env), m)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(cur))
    if not msgs:
        print(f"{now:%H:%M} {state}: no alerts")


if __name__ == "__main__":
    main()
