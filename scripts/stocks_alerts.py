"""Live Telegram alerts for the equities session (@ContrafactBot).

This is the stocks OPS report: capture health, session transitions, and the
REAL paper book (position, cash/holdings/total, P&L, vs-sim gap). It does
NOT carry the backtest windowed/per-quote numbers in the live heartbeat —
those answer "is there an edge", which is a research question the nightly
equities_digest.py owns; mixing the two into one message is exactly what
confused a plain reading of this bot (2026-07-20 review). The session-close
summary keeps a short, clearly-labelled backtest line for same-day
comparison, since "how did the real book do vs. the sim today" is a fair
end-of-day question.

⚠️ PROBLEMS ONLY (2026-07-25, Oli: "lots of useless alerts"). This script is
now silent unless something is WRONG. It sends nothing on a normal day:
  🔴 capture DOWN        in-session but no recorder process
  🔴 writer STALLED      rows_written frozen while events climb  <-- the 07-13 failure
  ⚠️  queue saturating   backlog near the cap (producer about to block the WS)
  ⚠️  reconnects         websocket dropping (usually the symptom of a stalled writer)
  🟢 recovered           back to healthy after any of the above
  ⚠️  disk filling       capture writes ~350MB/session

REMOVED and why: the hourly 💓 heartbeat, the 📈 session-open and 🏁
session-close messages. With real orders off (2026-07-21) the heartbeat's
position report was a permanent "no real orders configured" and the rest was
capture stats — ~9 predictable messages per weekday whose only real job was
proving the alerting still worked. That job now belongs to the ONE nightly
research digest (equities_digest.py), which carries a capture-health line:
if it doesn't arrive, something is wrong. Silence here means healthy; the
failure modes above are all still detected, and they are the ones that
actually cost data.

On 2026-07-13 the 30-symbol capture spent five hours in a writer death-spiral
(queue pinned at its cap, 34 websocket reconnects, rows_written frozen) and
nobody knew until it was looked at by hand — that is what these alerts exist
for, and all of them survive this cut.

Cron (MERGE), every 5 min on weekdays; it no-ops outside market hours:
    */5 * * * 1-5 cd $HOME/intraday-ml-signals && .venv/bin/python \
        scripts/stocks_alerts.py --root . --env $HOME/digest.env \
        >> logs/stocks_alerts_cron.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
import zoneinfo
from datetime import datetime
from pathlib import Path

from signals import telegram as tg

QUEUE_CAP = 250_000          # record.py's asyncio.Queue maxsize
QUEUE_WARN = 0.5             # warn once the backlog passes this fraction of the cap
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
    """status_stocks.json from stocks_live.py — capture health, the shadow
    (backtest) book, AND the real paper book. Preferred source; the log line
    is the fallback for a plain record.py capture, which has neither."""
    try:
        st = json.loads((root / "data" / "status_stocks.json").read_text())
    except (OSError, ValueError):
        return None
    sim = st.get("sim", {})
    pq = st.get("sim_per_quote", {})
    return {
        "paper": st.get("paper"),  # real-fills book (present when --trade is on)
        "sim_trades": int(sim.get("trades", 0)),
        "sim_avg_bps": float(sim.get("avg_net_bps", float("nan"))),
        "pq_trades": int(pq.get("trades", 0)),
        "pq_avg_bps": float(pq.get("avg_net_bps", float("nan"))),
        "up_s": int(st.get("uptime_s", 0)),
        "events": int(st.get("events", 0)),
        "rows": int(st.get("rows_written", 0)),
        "q_hwm": int(st.get("q_hwm", 0)),
        "reconnects": int(st.get("reconnects", 0)),
        "dropped": int(st.get("dropped", 0)),
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
                    "reconnects": recon, "dropped": 0, "paper": None,
                    "sim_trades": 0, "sim_avg_bps": float("nan"),
                    "pq_trades": 0, "pq_avg_bps": float("nan"),
                    "config": "capture-only (no live model)", "age_s": 0.0}
    return None


def position_line(cur: dict) -> str:
    """The OPS report: the REAL paper book — position, P&L, and how it
    compares to the sim's assumed cost (vs-sim gap). No backtest trade counts
    here; that question belongs to the nightly research digest, not a live
    heartbeat someone reads to check the account is behaving."""
    paper = cur.get("paper")
    if not paper:
        return "no real orders configured for this session"
    avg = paper.get("avg_net_bps", float("nan"))
    gap = paper.get("sim_gap_bps", float("nan"))
    slip = paper.get("entry_slippage_bps", float("nan"))
    lat = paper.get("avg_round_trip_latency_s", float("nan"))
    avg_s = f"{avg:+.2f}" if avg == avg else "—"
    gap_s = f"{gap:+.2f}" if gap == gap else "—"
    slip_s = f"{slip:+.2f}" if slip == slip else "—"
    lat_s = f"{lat:.1f}s" if lat == lat else "—"
    halt = " \N{MIDDLE DOT} \N{NO ENTRY} entries HALTED (loss cap)" if paper.get("halted") else ""
    recon = paper.get("reconciliations", 0)
    recon_s = f" \N{MIDDLE DOT} \N{WARNING SIGN} {recon} reconciliation close(s)" if recon else ""
    balance = paper.get("balance", 0.0)
    detail = paper.get("open_detail") or {}
    if detail:
        held = "\n" + "\n".join(
            f"  {d['qty']} {tg.esc(sym)} {d['side']} @ ${d['entry_fill']:,.2f} "
            f"\N{RIGHTWARDS ARROW} ${d['value']:,.2f} now ({d['unrealized_usd']:+.2f})"
            for sym, d in sorted(detail.items())
        )
    else:
        held = " none"
    cash = paper.get("cash", balance)
    hv = paper.get("holdings_value", 0.0)
    total = paper.get("total", cash + hv)
    return (
        f"<b>{paper.get('trades', 0)} real trades</b> \N{MIDDLE DOT} avg {avg_s}bps "
        f"\N{MIDDLE DOT} ${paper.get('pnl_usd', 0.0):+.2f} \N{MIDDLE DOT} "
        f"vs-sim gap {gap_s}bps \N{MIDDLE DOT} errs {paper.get('order_errors', 0)}"
        f"{halt}{recon_s}\n"
        f"entry slippage {slip_s}bps (signal\N{RIGHTWARDS ARROW}fill delay) "
        f"\N{MIDDLE DOT} avg round-trip latency {lat_s}\n"
        f"stocks book ${balance:,.2f} \N{MIDDLE DOT} holdings:{held}\n"
        f"cash ${cash:,.2f} \N{MIDDLE DOT} holdings ${hv:,.2f} "
        f"\N{MIDDLE DOT} total ${total:,.2f}"
    )


def db_size_mb(root: Path) -> float:
    dbs = sorted((root / "data").glob("equities_2*.duckdb"))
    return dbs[-1].stat().st_size / 1e6 if dbs else 0.0


def disk_used_pct(root: Path) -> float:
    """Used fraction of the filesystem the capture writes to."""
    u = shutil.disk_usage(root)
    return 100.0 * (u.total - u.free) / u.total


def detect(prev: dict, cur: dict) -> list[str]:
    """Pure transition logic (unit-tested). prev/cur are condensed states."""
    out: list[str] = []
    was, now = prev.get("state"), cur["state"]

    # No session-open / session-close / heartbeat messages: a normal day is
    # SILENT here and is reported once by the nightly digest instead. Only
    # things that need a human stay below.
    if now == "down" and was != "down":
        out.append("\N{LARGE RED SQUARE} <b>stocks capture DOWN</b> — "
                   "no recorder process during market hours")
    if now == "stalled" and was != "stalled":
        out.append(
            f"\N{LARGE RED SQUARE} <b>stocks writer STALLED</b> — rows frozen at "
            f"{cur['rows']:,} while events climb ({cur['events']:,}). The queue "
            f"will fill and the websocket will be dropped. Data is being lost."
        )
    if now == "open" and was in ("down", "stalled"):
        out.append("\N{LARGE GREEN CIRCLE} <b>stocks capture recovered</b> — "
                   "writer keeping up again")

    # warnings (only on the way up, so they don't repeat every 5 min)
    if cur["q_hwm"] >= QUEUE_CAP * QUEUE_WARN > prev.get("q_hwm", 0):
        out.append(
            f"\N{WARNING SIGN} stocks queue backlog {cur['q_hwm']:,}/{QUEUE_CAP:,} — "
            "writer falling behind; the producer will block the websocket if it "
            "hits the cap"
        )
    if cur["reconnects"] > prev.get("reconnects", 0):
        out.append(
            f"\N{WARNING SIGN} stocks websocket reconnects: {cur['reconnects']} "
            f"(was {prev.get('reconnects', 0)}) — usually a stalled writer"
        )
    # disk fills at ~350MB/session; warn once on the way up, any time of day
    if cur.get("disk_pct", 0.0) >= DISK_WARN_PCT > prev.get("disk_pct", 0.0):
        out.append(
            f"\N{WARNING SIGN} disk {cur['disk_pct']:.0f}% full — run "
            "scripts/archive_sessions.py (or check logs/) before capture dies"
        )

    # Real-book problems. Silent while --trade is off (paper is None); if it is
    # ever switched back on, these are the states that need a human — reported
    # once on the way up, never as a recurring status ping.
    paper, was_paper = cur.get("paper") or {}, prev.get("paper") or {}
    if paper:
        if paper.get("halted") and not was_paper.get("halted"):
            out.append(f"\N{NO ENTRY} <b>stocks entries HALTED</b> (daily loss cap)\n"
                       f"{position_line(cur)}")
        if paper.get("order_errors", 0) > was_paper.get("order_errors", 0):
            out.append(f"\N{WARNING SIGN} stocks order errors: "
                       f"{paper['order_errors']} (was {was_paper.get('order_errors', 0)})")
        if paper.get("reconciliations", 0) > was_paper.get("reconciliations", 0):
            out.append(
                f"\N{WARNING SIGN} stocks reconciliation close(s): "
                f"{paper['reconciliations']} (was {was_paper.get('reconciliations', 0)}) — "
                "a position was closed outside the normal order path"
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
    # status_stocks.json (has the shadow book + real book) beats the bare capture log
    status = read_status(root)
    log = status or read_log(root) or {
        "up_s": 0, "events": 0, "rows": 0, "q_hwm": 0, "reconnects": 0, "dropped": 0,
        "paper": None, "sim_trades": 0, "sim_avg_bps": float("nan"),
        "pq_trades": 0, "pq_avg_bps": float("nan"), "config": "", "age_s": 0.0,
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
           "disk_pct": disk_used_pct(root)}

    msgs = detect(prev, cur)

    if args.no_send:  # a dry run must NOT consume the transitions it is previewing
        for m in msgs:
            print(m)
        if not msgs:
            print(f"{now:%H:%M} {state}: no alerts")
        return

    creds = tg.load_env(args.env)
    button = None
    if creds.get("DASHBOARD_BASE_URL"):
        button = tg.dashboard_button(f"{creds['DASHBOARD_BASE_URL']}/stocks_app.html")
    for m in msgs:  # tg.send() never raises — a bad message can't wedge state again
        tg.send(creds, m, reply_markup=button)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(cur))
    if not msgs:
        print(f"{now:%H:%M} {state}: no alerts")


if __name__ == "__main__":
    main()
