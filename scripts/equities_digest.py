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
import statistics as stats
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

from signals.evaluation import SymbolScore, evaluate
from signals.features.engine import MICRO_FEATURES, FeatureConfig

TRACKED = ["NVDA", "AAPL"]  # always shown + named as candidates (the 07-10 finding)
DISPLAY_N = 14              # rows in the Telegram table (top movers by net + TRACKED)
HORIZON_S = 5.0
DEAD_ZONE_BPS = 4.0  # the flagship config's selectivity bar
SPREAD_CAP_BPS = 2.0  # spread-conditional entry: only trade when spread < this
GREEN_WINDOW = 10  # rolling tally over the last N sessions
PHASES = 10  # sampling-phase offsets averaged over (the lucky-grid guard)
# Config id stamped on every history row; the rolling tally counts ONLY rows with
# the current id, so changing the config starts a clean tally (no mixing nets from
# a different rule). The tracked candidate is NVDA (RESEARCH.md 2026-07-10).
# nomicro2 (2026-07-13): the ablation now drops the micro PRODUCT INTERACTIONS too
# (see MICRO_FEATURES) — the old list leaked trade-derived flow back in via
# flow_x_imbalance — and replay is deterministic (was tie-unstable, stdev 0.14bps).
# Both change the numbers, so the pre-07-13 rows are not comparable: new id.
CONFIG_ID = (f"ev_nomicro3_{HORIZON_S:g}s_dz{DEAD_ZONE_BPS:g}"
             f"_sc{SPREAD_CAP_BPS:g}_phasemean{PHASES}")


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


def excluded_days(root: Path) -> set[str]:
    """Sessions quarantined as data-quality failures — one YYYY-MM-DD per line in
    logs/excluded_sessions.txt (# comments allowed). A capture that stalled or kept
    reconnecting produces a gappy DB; scoring it would put a meaningless row into the
    rolling tally, which is the one thing the tally must never contain."""
    try:
        lines = (root / "logs" / "excluded_sessions.txt").read_text().splitlines()
    except OSError:
        return set()
    return {ln.split("#")[0].strip() for ln in lines if ln.split("#")[0].strip()}


def latest_db(root: Path, explicit: str | None) -> Path | None:
    if explicit:
        p = root / explicit
        return p if p.exists() else None
    skip = excluded_days(root)
    dbs = [
        p for p in sorted(glob.glob(str(root / "data" / "equities_2*.duckdb")))
        if Path(p).stem.replace("equities_", "") not in skip
    ]
    return Path(dbs[-1]) if dbs else None


def already_recorded(root: Path, day: str) -> bool:
    """Idempotence: never write a second row for a day under the same config (a
    re-run would double-count it in the green tally)."""
    history = root / "logs" / "equities_digest_history.jsonl"
    try:
        lines = history.read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("config") == CONFIG_ID and r.get("day") == day:
            return True
    return False


def db_symbols(db: Path) -> list[str]:
    """Symbols captured in this session (auto-discovered so capture & digest never
    drift). Excludes venue-prefixed leaders (e.g. CB:*) from the crypto store."""
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("SELECT DISTINCT symbol FROM quotes").fetchall()
    finally:
        con.close()
    return sorted(s for (s,) in rows if ":" not in s)


def _non_overlapping(rows, horizon_ns: int):
    """Rows spaced >= horizon apart — correct for SCORING direction (independent
    outcomes). NOT used for the trade sim: doing so silently subsamples entries to one
    per window, which is worth ~3x of the apparent edge (RESEARCH.md 2026-07-14).

    The persistence baseline MUST be recomputed within the subsequence: these rows come
    from the all-rows stream, where `persistence` is the immediately-preceding
    (overlapping, ~99%-shared-window) outcome. Scoring against that baseline compares
    the model to near-autocorrelation and makes d-best meaninglessly negative.
    """
    kept, last, prev_realized = [], -(10**18), 0.0
    for r in rows:
        if r.ts_ns >= last + horizon_ns:
            kept.append(replace(r, persistence=prev_realized))
            last = r.ts_ns
            prev_realized = r.realized
    return kept


def _on_grid(rows, horizon_ns: int, phase_ns: int):
    """First row in each absolute time bucket — a live 'sample every horizon' rule."""
    kept, last_bucket = [], None
    for r in rows:
        bucket = (r.ts_ns + phase_ns) // horizon_ns
        if bucket != last_bucket:
            kept.append(r)
            last_bucket = bucket
    return kept


# Symbols per replay pass. evaluate() — and ReplaySource under it, which
# fetchall()s EVERY quote for its symbol list before yielding — holds the whole
# pass in memory. A 30-symbol pass over a full session is several GB; on the
# 3.7GB server the nightly digest was OOM-KILLED on 07-14/15 and the rolling
# screen silently stopped (the one component that must never fail silently).
# Batching bounds peak memory at ~batch/30 of the day at the cost of extra
# sequential replay passes. Results are IDENTICAL: pipelines are per-symbol
# independent (no cross-feed here) and replay is deterministic — pinned by
# tests/test_equities_digest.py.
# 2: even after replay was made streaming, a chunk holding two ~2M-row names
# (the 3-symbol-era sessions put ALL of NVDA+AAPL+SPY in one chunk at batch=4)
# still OOM'd the box mid-backfill. Two symbols/pass is the worst case that fits.
BATCH_SYMBOLS = 2


def _score_symbol(sym: str, rows: list, hn: int, phases: list[int]) -> dict:
    """Day result for one symbol at the candidate config (metric semantics in
    screen()'s docstring)."""
    nan = float("nan")

    # direction: score on independent windows
    seg = SymbolScore(sym, _non_overlapping(rows, hn)).overall()

    def _sim(rs, allow_short=True):
        return SymbolScore(sym, rs).simulate_trading(
            hn, fee_bps=0.0, dead_zone_bps=DEAD_ZONE_BPS,
            max_spread_bps=SPREAD_CAP_BPS, allow_short=allow_short,
        )

    pq = _sim(rows)
    # The full 10-phase sweep (x2 for long-only) over ~1M rows is ~20 passes/symbol
    # — fine for 3 tracked names, far too slow nightly across 30. Phase-sweep only
    # the TRACKED candidates; for the rest, one grid + per-quote is enough to flag a
    # surprise (and per-quote is the conservative honest number anyway).
    if sym in TRACKED:
        grids = [_sim(_on_grid(rows, hn, p)) for p in phases]
        nets = [g.avg_net_bps for g in grids if g.trades]
        lo_nets = [
            v for p in phases
            if (v := _sim(_on_grid(rows, hn, p), allow_short=False).avg_net_bps) == v
        ]
        net_bps = (sum(nets) / len(nets)) if nets else nan
        net_spread = (max(nets) - min(nets)) if nets else nan
        net_lo = (sum(lo_nets) / len(lo_nets)) if lo_nets else nan
        trades = int(sum(g.trades for g in grids) / max(1, len(grids)))
        hit = (sum(g.hit_rate for g in grids if g.trades) / len(nets)) if nets else nan
    else:
        g0 = _sim(_on_grid(rows, hn, 0))
        net_bps, net_spread = g0.avg_net_bps, nan
        net_lo = _sim(_on_grid(rows, hn, 0), allow_short=False).avg_net_bps
        trades = g0.trades
        # was: reused the loop-carried `grids`/`nets` of the LAST TRACKED symbol —
        # every non-tracked hit rate in history silently belonged to NVDA/AAPL.
        hit = g0.hit_rate

    return {
        "dir": seg.dir_acc,
        "base": seg.dir_best_baseline,
        "d_best": seg.dir_acc - seg.dir_best_baseline,
        "net_bps": net_bps,          # phase-mean (tracked) / single grid (others)
        "net_spread": net_spread,    # phase fragility (tracked only)
        "net_lo": net_lo,
        "net_pq": pq.avg_net_bps,
        "trades": trades,
        "trades_pq": pq.trades,
        "hit": hit,
    }


async def screen(db: Path, batch: int = BATCH_SYMBOLS) -> dict[str, dict]:
    """Per-symbol day result at the candidate config (no-micro EV @ 5s, dz4,
    spread-conditional entry < SPREAD_CAP_BPS).

    Reports the trade sim under BOTH cadences, because reporting one silently means
    the wrong thing (RESEARCH.md 2026-07-14):

      net_bps  — PHASE-MEAN of the windowed rule ("sample once per 5s, act on that
                 reading"), averaged over PHASES absolute-clock offsets. The single
                 grid the old digest happened to use was a lucky draw: the phase
                 spread within one session (~5bps) exceeds the effect (~3bps).
      net_pq   — per-quote: act on every signal. Lower, because entering on the first
                 threshold upcrossing buys noise spikes.

    Direction metrics still use non-overlapping rows (independent outcomes) — that is
    what non_overlapping is FOR. The trade sim never does.

    Evaluated `batch` symbols per replay pass to bound memory (see BATCH_SYMBOLS).
    """
    symbols = db_symbols(db)
    cfg = FeatureConfig(exclude=MICRO_FEATURES)
    hn = int(HORIZON_S * 1e9)
    phases = [int(i * hn / PHASES) for i in range(PHASES)]
    out = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        res = await evaluate(str(db), chunk, model_kind="ev", horizon_s=HORIZON_S,
                             non_overlapping=False, feature_config=cfg)  # ALL rows
        for sym in chunk:
            rows = res.symbols[sym].rows
            out[sym] = _score_symbol(sym, rows, hn, phases)
            rows.clear()  # free each symbol's rows before the next pass
    return out


def rolling_green(history_path: Path, today: dict[str, dict]) -> dict[str, str]:
    """green/total over the last GREEN_WINDOW sessions per symbol (incl. today).
    Counts ONLY history rows scored with the CURRENT CONFIG_ID, so changing the
    config (e.g. adding the spread cap) starts a clean tally instead of mixing
    nets from a different trading rule."""
    rows = []
    if history_path.exists():
        for line in history_path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("config") == CONFIG_ID:
                rows.append(r)
    rows = rows[-(GREEN_WINDOW - 1):] + [{"result": today}]
    tally = {}
    for sym in today:
        vals = [r["result"].get(sym, {}).get("net_bps") for r in rows]
        vals = [v for v in vals if v is not None and v == v]  # drop None/NaN
        green = sum(1 for v in vals if v > 0)
        tally[sym] = f"{green}/{len(vals)}"
    return tally


def cumulative_tally(history_path: Path, today: dict[str, dict]) -> str:
    """The honest 'is the wall holding?' number, accumulated across every recorded
    session for the current config (+ today) and pushed nightly so the multi-session
    verdict answers itself, unattended.

    For each tracked name: the mean nightly phase-mean net, its ACROSS-session stdev
    (day-to-day fragility — the thing that actually decides deployability, distinct
    from the within-session ±ph the table shows), and their ratio net/σ. net/σ > 1
    means the daily edge exceeds its day-to-day noise; nothing in this project has
    ever cleared that bar, so a real edge would announce itself here as the ratio
    rising past 1 and STAYING there as n grows. A small-sample over-claim (like the
    2-session AAPL 'bright spot') shows up as a high ratio at low n that decays."""
    rows = []
    if history_path.exists():
        for line in history_path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("config") == CONFIG_ID:
                rows.append(r)
    rows.append({"result": today})  # today isn't in history yet (appended after send)
    lines = []
    for sym in TRACKED:
        nets = [r["result"].get(sym, {}).get("net_bps") for r in rows]
        nets = [v for v in nets if v is not None and v == v]  # drop None/NaN sessions
        if not nets:
            continue
        n = len(nets)
        mean = stats.mean(nets)
        sd = stats.stdev(nets) if n > 1 else float("nan")
        ratio = mean / sd if sd and sd == sd else float("nan")
        green = sum(1 for v in nets if v > 0)
        sdstr = f"{sd:.1f}" if sd == sd else "  —"
        rstr = f"{ratio:+.2f}" if ratio == ratio else "  — "
        lines.append(f"{sym:<5} {mean:>+5.1f}  ±σ{sdstr:>5}  net/σ {rstr:>6}"
                     f"  ({green}/{n} grn, n={n})")
    if not lines:
        return ""
    return ("cumulative across sessions (want net/σ &gt; 1 — none has yet):\n"
            f"<pre>{chr(10).join(lines)}</pre>")


def backfill(root: Path, dbs: list[str], batch: int = BATCH_SYMBOLS) -> None:
    """Score past sessions under the CURRENT config and append history rows (no
    send). Seeds the rolling tally so the candidate's track record shows up
    immediately instead of building over days. Skips a day already recorded for
    this config so re-runs don't double-count."""
    history = root / "logs" / "equities_digest_history.jsonl"
    seen = set()
    if history.exists():
        for line in history.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("config") == CONFIG_ID:
                seen.add(r.get("day"))
    history.parent.mkdir(parents=True, exist_ok=True)
    for dbp in sorted(dbs):
        day = Path(dbp).stem.replace("equities_", "")
        if not Path(dbp).exists():
            # a missing DB must not kill the rest of the list (it did: the server
            # has no 07-06, and the crash left 6 later sessions unscored)
            print(f"{day}: {dbp} not found — skip")
            continue
        if day in seen:
            print(f"{day}: already recorded for {CONFIG_ID} — skip")
            continue
        result = asyncio.run(screen(Path(dbp), batch=batch))
        with history.open("a") as fh:
            fh.write(json.dumps({
                "ts": dt.datetime.now(dt.UTC).isoformat(), "day": day,
                "db": Path(dbp).name, "config": CONFIG_ID, "result": result,
            }) + "\n")
        nv = result["NVDA"]
        print(f"{day}: NVDA net {nv['net_bps']:+.2f} (Lnet {nv['net_lo']:+.2f}) — recorded")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--env", default="/home/deploy/digest.env")
    ap.add_argument("--db", default=None, help="explicit DB (default: latest equities_*)")
    ap.add_argument("--no-send", action="store_true", help="print instead of Telegram")
    ap.add_argument("--backfill", nargs="+", metavar="DB",
                    help="score these past DBs into history (no send) then exit")
    ap.add_argument("--batch", type=int, default=BATCH_SYMBOLS,
                    help="symbols per replay pass (memory/speed trade; 1 = safest)")
    args = ap.parse_args()
    root = Path(args.root)

    if args.backfill:
        backfill(root, args.backfill, batch=args.batch)
        return

    db = latest_db(root, args.db)
    if db is None:
        print("no equities DB found — nothing to report (weekend/holiday/capture failed)")
        return
    day = db.stem.replace("equities_", "")
    if not args.db and already_recorded(root, day):
        print(f"{day}: already scored for {CONFIG_ID} — nothing new to report")
        return

    result = asyncio.run(screen(db, batch=args.batch))
    history = root / "logs" / "equities_digest_history.jsonl"
    tally = rolling_green(history, result)
    cum = cumulative_tally(history, result)

    # Aligned monospace table inside <pre> (Telegram HTML). Columns:
    # dir = model direction acc, Δd = edge over the best naive baseline, net =
    # net bps/trade (long+short, the candidate), Lnet = long-only net (the
    # deployable-in-a-cash-account version — the edge is short-dependent, so this
    # shows how much survives without shorting), tr = trades, hit = win rate,
    # g = rolling green sessions / total for THIS config (out-of-sample check).
    def cell(x: float, w: int, prec: int, plus: bool = True) -> str:
        """Right-justified numeric cell; '—' for NaN (e.g. a symbol with no
        trades after the spread gate) instead of an alarming '+nan'."""
        if x != x:  # NaN
            return f"{'—':>{w}}"
        return f"{x:>{'+' if plus else ''}{w}.{prec}f}"

    # Screen the whole captured universe; show the top movers by net plus the
    # tracked candidates (so a new single-name edge surfaces on its own, and the
    # tracked ones are always visible even on an off day). Nan-net (no trades) sinks.
    def net_key(sym: str) -> float:
        v = result[sym].get("net_bps")
        return v if v is not None and v == v else -1e9
    ranked = sorted(result, key=net_key, reverse=True)
    show = list(dict.fromkeys(ranked[:DISPLAY_N] + [s for s in TRACKED if s in result]))
    show.sort(key=net_key, reverse=True)

    head = (f"{'sym':<5}{'dir':>5}{'Δd':>6}{'net':>6}{'±ph':>5}"
        f"{'pq':>6}{'Lnet':>6}{'tr':>5}{'g':>6}")
    lines = [head]
    for sym in show:
        r = result[sym]
        star = "*" if sym in TRACKED else " "
        lines.append(
            f"{sym:<4}{star}{cell(r['dir'], 5, 2, plus=False)}{cell(r['d_best'], 6, 2)}"
            f"{cell(r['net_bps'], 6, 1)}{cell(r.get('net_spread', float('nan')), 5, 1, plus=False)}"
            f"{cell(r.get('net_pq', float('nan')), 6, 1)}{cell(r['net_lo'], 6, 1)}"
            f"{r['trades']:>5d}{tally[sym]:>6}"
        )
    table = "\n".join(lines)
    greens = sum(1 for s in result if net_key(s) > 0)
    msg = (
        f"📊 <b>equities OOS · {day}</b>  ({greens}/{len(result)} names net+)\n"
        f"{account_line(root)}\n"
        f"ev no-micro · 5s · dz{DEAD_ZONE_BPS:g} · spread&lt;{SPREAD_CAP_BPS:g}bp · fee 0\n"
        f"net=phase-mean · ±ph=spread across phases (fragility) · pq=per-quote · "
        f"Lnet=long-only\n"
        f"* = tracked. top {len(show)} of {len(result)} by net\n"
        f"<pre>{table}</pre>"
    )
    if cum:
        msg += f"\n{cum}"

    if args.no_send:
        print(msg)
    else:
        send(load_env(args.env), msg)

    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a") as fh:
        fh.write(json.dumps({
            "ts": dt.datetime.now(dt.UTC).isoformat(), "day": day, "db": db.name,
            "config": CONFIG_ID, "result": result,
        }) + "\n")


if __name__ == "__main__":
    main()
