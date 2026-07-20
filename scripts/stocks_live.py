"""Live equities session: capture + live shadow trading book + (since 2026-07-18)
REAL paper orders, with a real-time Telegram blotter (since 2026-07-20).

Replaces scripts/record.py for the market-hours capture. It does everything the
recorder did (same quotes -> same DuckDB, byte-for-byte the same research data) and
ALSO runs the tracked model live on the same feed:
  - a SHADOW book (no orders) in two cadences, for comparison against every
    RESEARCH.md/nightly-digest number
  - with --trade, a REAL paper trader (signals.stockstrader.StocksTrader) that
    places actual orders on Alpaca's paper API — real fills, real slippage — and
    posts each fill to Telegram as it happens (entry, exit, or a reconciliation
    close), so "what got traded, why, and what's the position now" is answered
    in real time, not just at the next heartbeat.

Why one process: Alpaca allows a single stocks websocket. Capture and model share it.

The model/feature/trade code is the SAME code the backtest uses (core.SymbolPipeline
+ simrule), so the live numbers and the nightly digest are directly comparable — any
disagreement is a bug, not an excuse.

Usage (see deploy/run_equities_capture.sh):
    uv run python scripts/stocks_live.py --symbols SPY NVDA ... --duration 23400 \
        --db data/equities_$(date +%F).duckdb --trade --env ~/digest.env
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import time

import uvloop

from signals import telegram as tg
from signals.config import load_alpaca_config
from signals.core import SymbolPipeline
from signals.data.alpaca import AlpacaSource
from signals.data.schema import MarketEvent, Quote
from signals.features.engine import MICRO_FEATURES, FeatureConfig, FeatureEngine
from signals.livesim import LiveSim
from signals.model.online import OnlineModel
from signals.stockstrader import StocksTrader
from signals.storage.coldstore import ColdStore

STATUS_PATH = "data/status_stocks.json"  # NOT status.json — that is the crypto pipeline's


def format_blotter_line(event: dict) -> str:
    """One real-time trade-blotter message per fill: what got traded, why (the
    prediction that triggered entry, or the net result at exit), and the
    running stocks-book balance — the 'cumulative position' the live heartbeat
    alone doesn't answer fast enough (2026-07-20 messaging review)."""
    bal = event["balance"]
    sym = tg.esc(event["symbol"])
    if event["kind"] == "entry":
        verb = "BOUGHT" if event["side"] == "long" else "SOLD SHORT"
        return (
            f"\N{INBOX TRAY} <b>{verb}</b> {event['qty']} {sym} @ {event['price']:,.2f}"
            f" \N{EM DASH} pred {event['pred_bps']:+.1f}bps\n"
            f"stocks book ${bal:,.2f}"
        )
    if event["kind"] == "exit":
        verb = "SOLD" if event["side"] == "long" else "COVERED"
        return (
            f"\N{OUTBOX TRAY} <b>{verb}</b> {event['qty']} {sym} "
            f"@ {event['exit_fill']:,.2f} \N{EM DASH} net {event['net_bps']:+.2f}bps "
            f"(${event['pnl_usd']:+.2f}) vs sim {event['sim_net_bps']:+.2f}bps\n"
            f"stocks book ${bal:,.2f}"
        )
    # reconciliation: a stranded position closed outside the normal entry/exit path
    return (
        f"\N{WARNING SIGN} <b>RECONCILED</b> {event['qty']} {sym} "
        f"@ {event['entry_fill']:,.2f} \N{EM DASH} stranded position closed, "
        f"${event['pnl_usd']:+.2f}\n"
        f"stocks book ${bal:,.2f}"
    )


async def main_async(args: argparse.Namespace) -> None:
    if args.replay:
        # Drive the live code path from a recorded session — proves the live book
        # reproduces the nightly digest's answer before it runs unattended.
        from signals.data.replay import ReplaySource
        source = ReplaySource(args.replay, args.symbols)
    else:
        source = AlpacaSource(
            load_alpaca_config(), market="stocks",
            subscribe_quotes=True, subscribe_trades=False,  # quotes-only: 30-symbol cap
        )
        await source.subscribe(args.symbols)

    horizon_ns = int(args.horizon_s * 1e9)
    cfg = FeatureConfig(exclude=MICRO_FEATURES)  # the tracked "no-micro" ablation
    pipes = {
        s: SymbolPipeline(s, FeatureEngine(cfg), OnlineModel(kind=args.model), horizon_ns)
        for s in args.symbols
    }
    def _book(windowed: bool) -> LiveSim:
        return LiveSim(
            horizon_ns=horizon_ns,
            dead_zone_bps=args.dead_zone_bps,
            max_spread_bps=args.max_spread_bps,
            allow_short=not args.long_only,
            windowed=windowed,
        )

    # Two books, because the cadence is worth ~3x of the edge (RESEARCH.md 07-14):
    #   sim      = windowed  -> the cadence every headline number was measured at
    #   sim_pq   = per-quote -> what acting on every signal actually earns
    sim = _book(True)
    sim_pq = _book(False)

    # Third measurement (2026-07-18, Oli's call): REAL paper orders. Entries at
    # prediction time under the same simrule; every fill records its own
    # frictionless-sim counterpart so the digest can print the reality gap.
    trader: StocksTrader | None = None
    if args.trade:
        if args.replay:
            raise SystemExit("--trade with --replay is forbidden (orders on old data)")
        from signals.execution.alpaca_exec import PaperExecutor

        creds = tg.load_env(args.env)
        button = (tg.dashboard_button(f"{creds['DASHBOARD_BASE_URL']}/stocks_app.html")
                  if creds.get("DASHBOARD_BASE_URL") else None)

        def on_fill(event: dict) -> None:
            # Fire-and-forget in a thread: tg.send() is a blocking HTTP call,
            # and this hook runs synchronously on the trading path — it must
            # never stall the websocket/capture loop waiting on Telegram.
            asyncio.create_task(asyncio.to_thread(
                tg.send, creds, format_blotter_line(event), reply_markup=button))

        trader = StocksTrader(
            executor=PaperExecutor(load_alpaca_config()),
            symbols=args.trade_symbols,  # resolved to ALL captured by resolve_args
            horizon_ns=horizon_ns,
            dead_zone_bps=args.dead_zone_bps,
            max_spread_bps=args.max_spread_bps,
            notional=args.notional,
            max_open=args.max_open,
            daily_loss_cap_usd=args.daily_loss_cap,
            allow_short=not args.long_only,
            book_root=".",  # persists the $50k stocks book (data/stocks_book.json)
            on_fill=on_fill,
        )
        with contextlib.suppress(Exception):
            await trader.flatten_on_start()  # reconcile any stranded position (parity
                                              # with the crypto pipeline's startup flatten)

    store = ColdStore(args.db)
    queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=250_000)
    writer = asyncio.create_task(store.run(queue))

    events = dropped = 0
    q_hwm = 0
    start = time.monotonic()
    last_status = 0.0

    def write_status() -> None:
        payload = {
            "ts": time.time(),
            "uptime_s": time.monotonic() - start,
            "events": events,
            "rows_written": store.rows_written,
            "q_hwm": q_hwm,
            "reconnects": getattr(source, "reconnects", 0),
            "dropped": dropped,
            "symbols": len(args.symbols),
            "config": (f"{args.model} no-micro {args.horizon_s:g}s "
                       f"dz{args.dead_zone_bps:g} spread<{args.max_spread_bps:g}bp"
                       f"{' long-only' if args.long_only else ''}"),
            "sim": sim.summary(),          # windowed (comparable to the digest)
            "sim_per_quote": sim_pq.summary(),  # every signal (the honest live rule)
        }
        if trader is not None:
            payload["paper"] = trader.summary()  # real fills, real slippage
        tmp = STATUS_PATH + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, STATUS_PATH)  # atomic: the bot never reads a half file
        except OSError:
            pass

    print(f"live stocks: {len(args.symbols)} symbols -> {args.db} | {sim.max_spread_bps}bp gate",
          flush=True)
    try:
        async for event in source.stream():
            if time.monotonic() - start > args.duration:
                break
            events += 1

            # 1) capture. Blocking put, like record.py: backpressure is CORRECT here —
            # silently dropping events would corrupt the research data, which is the
            # whole point of the session. The writer does 371k events/s (numpy bulk
            # insert), so this never actually blocks in practice; if it ever does, the
            # stocks_alerts queue/stall alarms fire rather than data quietly vanishing.
            await queue.put(event)
            q_hwm = max(q_hwm, queue.qsize())

            # 2) live model: same SymbolPipeline the backtest replays through
            pipe = pipes.get(event.symbol)
            if pipe is None or not isinstance(event, Quote):
                continue
            step = pipe.on_event(event)
            if trader is not None and step.prediction is not None:
                trader.on_prediction(step.prediction)
            for r in step.resolved:
                sim.on_resolved(event.symbol, r.ts_ns, r.prediction, r.realized, r.spread_bps)
                sim_pq.on_resolved(event.symbol, r.ts_ns, r.prediction, r.realized, r.spread_bps)

            now = time.monotonic()
            if now - last_status >= 30:
                last_status = now
                write_status()
                s = sim.summary()
                recon = getattr(source, "reconnects", 0)
                print(
                    f"[+{now - start:6.0f}s] events={events} rows={store.rows_written} "
                    f"q_hwm={q_hwm} reconnects={recon} dropped={dropped} | "
                    f"windowed: trades={s['trades']} avg={s['avg_net_bps']:+.2f}bps | "
                    f"per-quote: trades={sim_pq.total_trades} "
                    f"avg={sim_pq.summary()['avg_net_bps']:+.2f}bps",
                    flush=True,
                )
    finally:
        await source.close()
        if trader is not None:
            with contextlib.suppress(Exception):
                await trader.close_all()  # settle in-flight, sweep OUR symbols only
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        await store.flush()
        store.close()
        write_status()
    s, q = sim.summary(), sim_pq.summary()
    paper = ""
    if trader is not None:
        t = trader.summary()
        paper = (f" | PAPER {t['trades']}tr {t['avg_net_bps']:+.2f}bps "
                 f"${t['pnl_usd']:+.2f} gap {t['sim_gap_bps']:+.2f}bps "
                 f"errs {t['order_errors']}")
    print(f"done: {store.rows_written} rows -> {args.db} | "
          f"windowed {s['trades']}tr {s['avg_net_bps']:+.2f}bps | "
          f"per-quote {q['trades']}tr {q['avg_net_bps']:+.2f}bps{paper}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--duration", type=float, default=23_400)
    p.add_argument("--db", default="data/equities_live.duckdb")
    p.add_argument("--model", default="ev")
    p.add_argument("--horizon-s", type=float, default=5.0)
    p.add_argument("--dead-zone-bps", type=float, default=4.0)
    p.add_argument("--max-spread-bps", type=float, default=2.0)
    p.add_argument("--replay", default=None,
               help="drive from a recorded DB instead of the live feed "
                    "(validation: must reproduce the digest's numbers)")
    p.add_argument("--long-only", action="store_true",
                   help="no shorts (the tracked edge is short-dependent; this shows "
                        "what a cash account would actually capture)")
    p.add_argument("--trade", action="store_true",
                   help="place REAL paper orders for --trade-symbols (entries at "
                        "prediction time, same simrule; refused with --replay). "
                        "Posts a real-time Telegram blotter line per fill.")
    p.add_argument("--env", default="/home/deploy/digest.env",
                   help="Telegram creds for the real-time blotter (only used with --trade)")
    p.add_argument("--trade-symbols", nargs="+", default=None,
                   help="symbols eligible for real orders (default: ALL captured "
                        "symbols — the simrule gate decides per-signal which are "
                        "predicted profitable)")
    p.add_argument("--notional", type=float, default=1_000.0,
                   help="max $ per position; whole shares only (qty = notional // mid)")
    p.add_argument("--max-open", type=int, default=2)
    p.add_argument("--daily-loss-cap", type=float, default=25.0,
                   help="realized $ loss that halts new entries for the session")
    return p


def resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    """Post-parse resolution, split out so tests can pin it (a silently no-opped
    CLI wiring has bitten this repo before)."""
    if args.trade_symbols is None:
        # "predicted profitable" is simrule's per-signal call across the whole
        # captured universe, not a hand-picked list (2026-07-19, Oli)
        args.trade_symbols = args.symbols
    return args


def main() -> None:
    args = resolve_args(build_parser().parse_args())
    uvloop.install()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
