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
DISK_WARN_PCT = 85           # capture writes ~350MB/day; warn before the disk kills it
# Matches BOTH capture processes' progress line: record.py prints `rows_written=`,
# stocks_live.py prints `rows=`. The old pattern only matched record.py, so when
# read_status momentarily failed the fallback silently matched a STALE record.py
# line left in the append-only cron log — which is how the open message once
# announced "capture-only" while stocks_live.py was in fact running.
LOG_LINE = re.compile(
    r"\[\+\s*(\d+)s\]\s+events=(\d+)\s+rows(?:_written)?=(\d+)\s+q_hwm=(\d+)\s+reconnects=(\d+)"
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


def capture_kind() -> str | None:
    """WHICH capture process is up — the authority on whether a live model is
    running, independent of whether status_stocks.json happens to be readable this
    instant. 'live' = stocks_live.py (capture + shadow model); 'record' = record.py
    (capture only); None = nothing. The shadow-book descriptor is derived from THIS,
    not from a status file that can be momentarily missing at the open tick, so the
    session-open message can never again announce 'no live model' while one runs."""
    def running(pat: str) -> bool:
        return subprocess.run(["pgrep", "-f", pat],
                              capture_output=True, check=False).returncode == 0
    if running("stocks_live.py"):
        return "live"
    if running("record.py --market stocks"):
        return "record"
    return None


def shadow_descriptor(status: dict | None, kind: str | None) -> str:
    """The shadow-book label for the session-open message. status_stocks.json (if
    readable) is authoritative — it carries the live model's own config string. If it
    is momentarily unreadable, the RUNNING PROCESS decides: stocks_live.py means a
    live model is up (never say 'no live model'); only a bare record.py capture is
    'capture-only'. This is the fix for the false 'capture-only' open message."""
    if status:
        return status.get("config", "")
    if kind == "live":
        return "live shadow model (warming up)"
    if kind == "record":
        return "capture-only (no live model)"
    return ""


def read_status(root: Path) -> dict | None:
    """status_stocks.json from stocks_live.py — capture health AND the live shadow
    book (trades, net bps, per symbol). Preferred source; the log line is the
    fallback for a plain record.py capture, which has no trading numbers at all."""
    try:
        st = json.loads((root / "data" / "status_stocks.json").read_text())
    except (OSError, ValueError):
        return None
    sim = st.get("sim", {})
    pq = st.get("sim_per_quote", {})
    return {
        "pq_trades": int(pq.get("trades", 0)),
        "pq_avg_bps": float(pq.get("avg_net_bps", float("nan"))),
        "up_s": int(st.get("uptime_s", 0)),
        "events": int(st.get("events", 0)),
        "rows": int(st.get("rows_written", 0)),
        "q_hwm": int(st.get("q_hwm", 0)),
        "reconnects": int(st.get("reconnects", 0)),
        "dropped": int(st.get("dropped", 0)),
        "trades": int(sim.get("trades", 0)),
        "net_bps": float(sim.get("net_bps_sum", 0.0)),
        "avg_bps": float(sim.get("avg_net_bps", float("nan"))),
        "hit": float(sim.get("hit_rate", float("nan"))),
        "by_symbol": sim.get("by_symbol", {}),
        "config": st.get("config", ""),
        "age_s": time.time() - st.get("ts", 0),
    }


def read_log(root: Path) -> dict | None:
    """Fallback: last progress line from a plain record.py capture (no trading)."""
    try:
        lines = (root / "logs" / "equities_cron.log").read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines[-40:]):
        m = LOG_LINE.search(line)
        if m:
            up, events, rows, hwm, recon = (int(g) for g in m.groups())
            return {"up_s": up, "events": events, "rows": rows, "q_hwm": hwm,
                    "reconnects": recon, "dropped": 0, "trades": 0, "net_bps": 0.0,
                    "avg_bps": float("nan"), "hit": float("nan"), "by_symbol": {},
                    "config": "capture-only (no live model)", "age_s": 0.0}
    return None


def trading_line(cur: dict) -> str:
    """How many stocks were traded, and what they made — under BOTH cadences.

    They differ by ~3x of the edge (RESEARCH.md 2026-07-14), so reporting only one
    would quietly mean the wrong thing:
      windowed  — one look per 5s window; the cadence every research headline used
      per-quote — act on every signal; what a naive live implementation earns
    """
    n = cur.get("trades", 0)
    pq_n = cur.get("pq_trades", 0)
    if not n and not pq_n:
        return "trades 0 — no signal has cleared the spread gate yet"
    top = sorted(cur.get("by_symbol", {}).items(),
                 key=lambda kv: -kv[1].get("trades", 0))[:4]
    names = " · ".join(f"{s} {d['trades']}@{d['avg_net_bps']:+.1f}" for s, d in top)
    hit = cur.get("hit", float("nan"))
    hit_s = f"{hit * 100:.0f}%" if hit == hit else "—"
    pq_avg = cur.get("pq_avg_bps", float("nan"))
    pq_s = f"{pq_avg:+.2f}" if pq_avg == pq_avg else "—"
    return (
        f"<b>{n} trades</b> (windowed) · net {cur['net_bps']:+.0f}bps "
        f"(avg {cur['avg_bps']:+.2f}) · hit {hit_s}\n"
        f"{names}\n"
        f"per-quote: {pq_n} trades · avg {pq_s}bps ← the honest live rule"
    )


def db_size_mb(root: Path) -> float:
    dbs = sorted((root / "data").glob("equities_2*.duckdb"))
    return dbs[-1].stat().st_size / 1e6 if dbs else 0.0


def disk_used_pct(root: Path) -> float:
    """Used fraction of the filesystem the capture writes to."""
    import shutil
    u = shutil.disk_usage(root)
    return 100.0 * (u.total - u.free) / u.total


def detect(prev: dict, cur: dict) -> list[str]:
    """Pure transition logic (unit-tested). prev/cur are condensed states."""
    out: list[str] = []
    was, now = prev.get("state"), cur["state"]

    if now == "open" and was in (None, "closed"):
        out.append(
            f"📈 <b>stocks session open</b> — 30 symbols streaming\n"
            f"shadow book: {cur.get('config', '')} (no real orders)"
        )
    elif now == "closed" and was in ("open", "down", "stalled"):
        out.append(
            f"🏁 <b>stocks session close</b>\n"
            f"{trading_line(cur)}\n"
            f"—\ncapture: events {cur['events']:,} · rows {cur['rows']:,} · "
            f"unwritten {max(0, cur['events'] - cur['rows']):,} · "
            f"dropped {cur.get('dropped', 0):,}\n"
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
    # disk fills at ~350MB/session; warn once on the way up, any time of day
    if cur.get("disk_pct", 0.0) >= DISK_WARN_PCT > prev.get("disk_pct", 0.0):
        out.append(
            f"⚠️ disk {cur['disk_pct']:.0f}% full — run "
            f"scripts/archive_sessions.py (or check logs/) before capture dies"
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
    kind = capture_kind()  # 'live' | 'record' | None — the authority on the model
    alive = kind is not None
    # status_stocks.json (has the shadow book) beats the bare capture log
    status = read_status(root)
    log = status or read_log(root) or {
        "up_s": 0, "events": 0, "rows": 0, "q_hwm": 0, "reconnects": 0, "dropped": 0,
        "trades": 0, "net_bps": 0.0, "avg_bps": float("nan"), "hit": float("nan"),
        "by_symbol": {}, "config": "", "age_s": 0.0,
    }
    # The shadow-book descriptor must reflect the RUNNING PROCESS, not a status file
    # that may be unreadable at the first in-session tick (stocks_live.py writes it
    # every 30s). Deriving it from `kind` stops the open message from falling back to
    # the stale "capture-only" cron-log string while the live model is running.
    log["config"] = shadow_descriptor(status, kind)

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
           "disk_pct": disk_used_pct(root), "last_beat": prev.get("last_beat", 0)}

    msgs = detect(prev, cur)

    # heartbeat so silence is informative, not ambiguous — leads with the trading
    if state == "open" and time.time() - prev.get("last_beat", 0) > HEARTBEAT_MIN * 60:
        secs = max(1, log["up_s"])
        msgs.append(
            f"💓 <b>stocks live</b>\n{trading_line(cur)}\n"
            f"—\ncapture ok: {log['events']:,} events ({log['events'] / secs:,.0f}/s) · "
            f"queue {log['q_hwm']:,} · reconnects {log['reconnects']}"
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
