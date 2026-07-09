"""Nightly equities Telegram digest — the rolling out-of-sample screen.

The equities capture is data-only (no live pipeline, so no status.json and no
alerts.py path). This runs after the session closes, evaluates the day's captured
DB with the candidate config (no-micro EV @ 5s), and pushes a per-symbol summary
to the same @ContrafactBot the crypto pipeline uses — plus a rolling green-count
so a genuinely repeatable edge would show up as consistency, not day-hopping.

Honest by construction: it reports net bps AND direction-vs-baseline for every
symbol every day, and appends to logs/equities_digest_history.jsonl. A real edge
is "green on most of the last N sessions"; the 07-06/07/08 NVDA episode (green,
green, RED) is exactly the false positive this is meant to expose early.

Cron (MERGE): weekdays ~21:15 UTC, after the 14:30+6.5h capture closes.
    15 21 * * 1-5 cd $HOME/intraday-ml-signals && .venv/bin/python \
        scripts/equities_digest.py --root . --env $HOME/digest.env \
        >> logs/equities_digest_cron.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import glob
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from signals.evaluation import evaluate
from signals.features.engine import FeatureConfig

SYMBOLS = ["SPY", "AAPL", "NVDA"]
HORIZON_S = 5.0
DEAD_ZONE_BPS = 4.0  # the flagship config's selectivity bar
MICRO = ["spread_bps", "imbalance", "flow", "micro_bps", "uptick", "dt_s", "micro_over_spread"]
GREEN_WINDOW = 10  # rolling tally over the last N sessions


def load_env(path: str) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"')
    return out


def send(creds: dict[str, str], text: str) -> None:
    # HTML parse mode so the <pre> block renders as an aligned monospace table.
    data = urllib.parse.urlencode({
        "chat_id": creds["TELEGRAM_CHAT_ID"], "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{creds['TELEGRAM_BOT_TOKEN']}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=15) as resp:
        resp.read()


def account_line(root: Path) -> str:
    """Paper-account balance from the crypto pipeline's status.json (one shared
    Alpaca paper account; equities is capture-only, so this is the whole acct)."""
    try:
        st = json.loads((root / "data" / "status.json").read_text())
        age = time.time() - st.get("ts", 0)
        fresh = "" if age < 600 else " ⚠stale"
        return (f"paper acct ${st.get('equity', 0):,.0f} "
                f"(today {st.get('pnl_today', 0.0):+.2f}){fresh}")
    except (OSError, ValueError):
        return "paper acct n/a"


def latest_db(root: Path, explicit: str | None) -> Path | None:
    if explicit:
        p = root / explicit
        return p if p.exists() else None
    dbs = sorted(glob.glob(str(root / "data" / "equities_2*.duckdb")))
    return Path(dbs[-1]) if dbs else None


async def screen(db: Path) -> dict[str, dict]:
    """Per-symbol day result at the candidate config (no-micro EV @ 5s, dz4)."""
    cfg = FeatureConfig(exclude=tuple(MICRO))
    res = await evaluate(str(db), SYMBOLS, model_kind="ev", horizon_s=HORIZON_S,
                         non_overlapping=True, feature_config=cfg)
    hn = int(HORIZON_S * 1e9)
    out = {}
    for sym in SYMBOLS:
        sc = res.symbols[sym]
        seg = sc.overall()
        sim = sc.simulate_trading(hn, fee_bps=0.0, dead_zone_bps=DEAD_ZONE_BPS)
        out[sym] = {
            "dir": seg.dir_acc,
            "base": seg.dir_best_baseline,
            "d_best": seg.dir_acc - seg.dir_best_baseline,
            "net_bps": sim.avg_net_bps,
            "trades": sim.trades,
            "hit": sim.hit_rate,
        }
    return out


def rolling_green(history_path: Path, today: dict[str, dict]) -> dict[str, str]:
    """green/total over the last GREEN_WINDOW sessions per symbol (incl. today)."""
    rows = []
    if history_path.exists():
        for line in history_path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    rows = rows[-(GREEN_WINDOW - 1):] + [{"result": today}]
    tally = {}
    for sym in SYMBOLS:
        vals = [r["result"].get(sym, {}).get("net_bps") for r in rows]
        vals = [v for v in vals if v is not None and v == v]  # drop None/NaN
        green = sum(1 for v in vals if v > 0)
        tally[sym] = f"{green}/{len(vals)}"
    return tally


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--env", default="/home/deploy/digest.env")
    ap.add_argument("--db", default=None, help="explicit DB (default: latest equities_*)")
    ap.add_argument("--no-send", action="store_true", help="print instead of Telegram")
    args = ap.parse_args()
    root = Path(args.root)

    db = latest_db(root, args.db)
    if db is None:
        print("no equities DB found — nothing to report (weekend/holiday/capture failed)")
        return
    day = db.stem.replace("equities_", "")

    result = asyncio.run(screen(db))
    history = root / "logs" / "equities_digest_history.jsonl"
    tally = rolling_green(history, result)

    # Aligned monospace table inside <pre> (Telegram HTML). Columns:
    # dir = model direction acc, base = best naive baseline, Δd = edge over it,
    # net/tr = avg net bps per trade & trade count, hit = win rate, g = rolling
    # green sessions / total (the out-of-sample consistency check).
    head = f"{'sym':<5}{'dir':>5}{'base':>6}{'Δd':>6}{'net':>7}{'tr':>5}{'hit':>5}{'g':>6}"
    lines = [head]
    for sym in SYMBOLS:
        r = result[sym]
        lines.append(
            f"{sym:<5}{r['dir']:>5.2f}{r['base']:>6.2f}{r['d_best']:>+6.2f}"
            f"{r['net_bps']:>+7.1f}{r['trades']:>5d}{r['hit'] * 100:>4.0f}%{tally[sym]:>6}"
        )
    table = "\n".join(lines)
    msg = (
        f"📊 <b>equities OOS · {day}</b>\n"
        f"{account_line(root)}\n"
        f"ev no-micro · 5s · dead-zone {DEAD_ZONE_BPS:g}bp · fee 0\n"
        f"<pre>{table}</pre>"
    )

    if args.no_send:
        print(msg)
    else:
        send(load_env(args.env), msg)

    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a") as fh:
        fh.write(json.dumps({
            "ts": dt.datetime.now(dt.UTC).isoformat(), "day": day, "db": db.name,
            "config": f"ev_nomicro_{HORIZON_S:g}s_dz{DEAD_ZONE_BPS:g}", "result": result,
        }) + "\n")


if __name__ == "__main__":
    main()
