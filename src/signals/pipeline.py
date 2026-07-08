"""asyncio orchestration — wires the stages together via bounded queues.

    ingest -> [consumer: SymbolPipeline -> policy -> positions/risk -> executor]
                  \\------------------ tap ------------------> cold store

The consumer is single-task so per-symbol state stays serial (no locks). Order
submission runs in fire-and-forget tasks off the critical path; a symbol with an
in-flight order is locked against re-entry until the fill is booked. The cold
store tap never blocks: if its queue is full, records are dropped and counted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .config import AlpacaConfig, load_alpaca_config
from .core import SymbolPipeline
from .data.alpaca import AlpacaSource
from .data.base import DataSource
from .data.coinbase import CoinbaseSource
from .data.ingest import IngestStage
from .data.schema import MarketEvent
from .execution.alpaca_exec import PaperExecutor
from .features.cross import CrossFeed
from .features.engine import FeatureEngine
from .model.online import OnlineModel
from .signal.policy import SignalPolicy
from .signal.positions import OrderIntent, PositionBook
from .signal.risk import RiskLimits, RiskManager
from .storage.coldstore import ColdStore, LogOrder, LogPrediction, LogRecord, LogResolution

log = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    symbols: list[str] = field(default_factory=lambda: ["BTC/USD"])
    market: str = "crypto"  # "crypto" | "stocks" (stocks: US market hours only)
    horizon_s: float = 10.0
    model_kind: str = "linear"
    leaders: dict[str, str] | None = None  # follower -> leader cross-asset features
    cb_products: list[str] = field(default_factory=list)  # aux Coinbase feed (leader-only)
    cost_bps: float = 5.0
    dead_zone_bps: float = 2.0
    limits: RiskLimits = field(default_factory=RiskLimits)
    db_path: str = "data/live.duckdb"
    dry_run: bool = False  # predictions + signals, but no orders
    flatten_on_exit: bool = True
    flatten_on_start: bool = True  # clear unmanaged account residue at startup
    status_every_s: float = 30.0
    equity_refresh_s: float = 60.0


class Pipeline:
    def __init__(
        self,
        config: PipelineConfig,
        alpaca: AlpacaConfig,
        source: DataSource | None = None,
        executor: PaperExecutor | None = None,
    ) -> None:
        self.config = config
        self.source = source or AlpacaSource(
            alpaca, market=config.market, subscribe_quotes=True
        )
        self.executor = executor or (None if config.dry_run else PaperExecutor(alpaca))
        self.policy = SignalPolicy(config.cost_bps, config.dead_zone_bps)
        self.risk = RiskManager(config.limits)
        self.book = PositionBook(self.risk)
        self.crossfeed = CrossFeed() if config.leaders else None
        self.pipes = {
            s: SymbolPipeline(
                s,
                FeatureEngine(crossfeed=self.crossfeed, leader=(config.leaders or {}).get(s)),
                OnlineModel(kind=config.model_kind),
                horizon_ns=int(config.horizon_s * 1e9),
            )
            for s in config.symbols
        }
        # Leader-only engines: symbols we stream for their information (they
        # publish to the crossfeed) but never predict on or trade.
        self.leader_engines: dict[str, FeatureEngine] = {
            leader: FeatureEngine(crossfeed=self.crossfeed)
            for leader in (config.leaders or {}).values()
            if leader not in self.pipes
        }
        self.aux_source: CoinbaseSource | None = (
            CoinbaseSource() if config.cb_products else None
        )
        self.store = ColdStore(config.db_path)
        self.tap: asyncio.Queue[LogRecord] = asyncio.Queue(maxsize=50_000)
        self.tap_dropped = 0
        self.equity = 0.0
        self.orders_submitted = 0
        self.order_errors = 0
        self.signals_actioned = 0
        self._inflight: set[str] = set()
        self._order_tasks: set[asyncio.Task] = set()
        self._proc_us: deque[float] = deque(maxlen=5000)
        self._recent_orders: deque[dict] = deque(maxlen=20)  # surfaced in status.json

    # ---- logging tap (never blocks the hot loop) ----
    def _tap(self, record: LogRecord) -> None:
        try:
            self.tap.put_nowait(record)
        except asyncio.QueueFull:
            self.tap_dropped += 1

    # ---- order path (off the critical loop) ----
    async def _execute(self, intent: OrderIntent) -> None:
        assert self.executor is not None
        try:
            qty = intent.qty
            if intent.side == "sell":
                # Alpaca takes crypto buy fees in the asset: actual position is
                # slightly below the buy's filled qty. Clamp to what we hold.
                held = await self.executor.position_qty(intent.symbol)
                if held <= 0:
                    self.book.on_fill(intent.symbol, "sell", 0.0, 0.0)  # de-sync guard
                    return
                qty = min(qty, held)
            result = await self.executor.market_order(intent.symbol, intent.side, qty)
            if result.status not in ("filled", "canceled", "rejected"):
                result = await self.executor.poll_fill(result.order_id)
            self.orders_submitted += 1
            if result.status == "filled" and result.filled_qty > 0:
                realized = self.book.on_fill(
                    intent.symbol, intent.side, result.filled_qty, result.filled_avg_price
                )
                note = f"{intent.reason}; realized={realized:.2f}"
            else:
                note = f"{intent.reason}; NOT FILLED"
                log.warning("order not filled: %s %s", intent, result)
            self._tap(
                LogOrder(
                    symbol=intent.symbol,
                    ts_ns=time.time_ns(),
                    action=intent.side,
                    qty=result.filled_qty or intent.qty,
                    status=result.status,
                    fill_price=result.filled_avg_price,
                    note=note,
                )
            )
            self._recent_orders.append(
                {
                    "ts": time.time(),
                    "symbol": intent.symbol,
                    "side": intent.side,
                    "qty": result.filled_qty or intent.qty,
                    "price": result.filled_avg_price,
                    "status": result.status,
                    "note": note,
                }
            )
        except Exception:
            self.order_errors += 1
            log.exception("order failed: %s", intent)
        finally:
            self._inflight.discard(intent.symbol)

    def _submit(self, intent: OrderIntent) -> None:
        self._inflight.add(intent.symbol)
        task = asyncio.create_task(self._execute(intent))
        self._order_tasks.add(task)
        task.add_done_callback(self._order_tasks.discard)

    # ---- hot loop ----
    def _on_event(self, event: MarketEvent) -> None:
        self._tap(event)  # leader events included: the DB doubles as dual-venue capture
        leader_engine = self.leader_engines.get(event.symbol)
        if leader_engine is not None:
            leader_engine.update(event)  # publishes to crossfeed; no prediction
            return
        pipe = self.pipes.get(event.symbol)
        if pipe is None:
            return
        step = pipe.on_event(event)
        for r in step.resolved:
            self._tap(
                LogResolution(event.symbol, r.ts_ns, r.resolved_ts_ns, r.prediction, r.realized)
            )
        pred = step.prediction
        if pred is None:
            return
        self._proc_us.append(pred.proc_us)
        self._tap(
            LogPrediction(
                pred.symbol, pred.ts_ns, pred.predicted, pred.mid, pred.spread_bps, pred.proc_us
            )
        )
        signal = self.policy.decide(pred.symbol, pred.predicted, pred.spread_bps)
        if signal.action.value == 0 or self.config.dry_run or self.executor is None:
            return
        if pred.symbol in self._inflight:
            return  # order already working for this symbol
        vol = pipe.features.last_raw.get("vol", 0.0)
        intent = self.book.on_signal(signal, pred.mid, self.equity, vol)
        if intent is not None:
            self.signals_actioned += 1
            self._submit(intent)

    async def _consume(self, queue: asyncio.Queue[MarketEvent]) -> None:
        while True:
            self._on_event(await queue.get())

    async def _status_loop(self, stage: IngestStage, started: float) -> None:
        last_equity_refresh = 0.0
        current_day = time.gmtime().tm_yday
        while True:
            await asyncio.sleep(self.config.status_every_s)
            now = time.monotonic()
            day = time.gmtime().tm_yday
            if day != current_day:  # UTC date rollover: daily loss limit resets
                current_day = day
                self.risk.reset_day()
                log.info("new UTC day: daily-loss circuit breaker reset")
            refresh_due = now - last_equity_refresh > self.config.equity_refresh_s
            if self.executor is not None and refresh_due:
                with contextlib.suppress(Exception):
                    self.equity = await self.executor.equity()
                    last_equity_refresh = now
            lat = np.array(self._proc_us) if self._proc_us else np.array([0.0])
            metrics = {s: p.model.metrics() for s, p in self.pipes.items()}
            summary = " | ".join(
                f"{s}: n={m['n']:.0f} dir={m['directional_acc']:.2f}" for s, m in metrics.items()
            )
            print(
                f"[+{now - started:6.0f}s] events={stage.events}"
                f" proc_us p50={np.percentile(lat, 50):.0f} p99={np.percentile(lat, 99):.0f}"
                f" | {summary} | pos={self.book.open_count} orders={self.orders_submitted}"
                f" errs={self.order_errors} pnl_today={self.risk.realized_pnl_today:.2f}"
                f" breaker={'TRIPPED' if self.risk.circuit_breaker_tripped else 'ok'}"
                f" tap_drop={self.tap_dropped}",
                flush=True,
            )
            self._write_status(stage, started, lat, metrics)

    def _write_status(self, stage, started, lat, metrics) -> None:  # type: ignore[no-untyped-def]
        """Atomically dump machine-readable status for the monitoring dashboard.

        This file (not the DB) is the dashboard's source of truth — the DuckDB
        is single-writer and must not be opened by other processes while live.
        Later, a control-plane counterpart (control.json, read here) can adjust
        limits at runtime; writing status is the read-only half of that design.
        """
        status = {
            "ts": time.time(),
            "uptime_s": round(time.monotonic() - started),
            "symbols": self.config.symbols,
            "model": self.config.model_kind,
            "events": stage.events,
            "proc_us_p50": float(np.percentile(lat, 50)),
            "proc_us_p99": float(np.percentile(lat, 99)),
            "per_symbol": {
                s: {"n": m["n"], "dir": m["directional_acc"]} for s, m in metrics.items()
            },
            "open_positions": self.book.open_count,
            "orders": self.orders_submitted,
            "order_errors": self.order_errors,
            "pnl_today": self.risk.realized_pnl_today,
            "breaker": "TRIPPED" if self.risk.circuit_breaker_tripped else "ok",
            "tap_dropped": self.tap_dropped,
            "equity": self.equity,
            "reconnects": getattr(self.source, "reconnects", 0),
            "recent_orders": list(self._recent_orders),
        }
        try:
            tmp = "data/status.json.tmp"
            with open(tmp, "w") as fh:
                json.dump(status, fh)
            os.replace(tmp, "data/status.json")
        except OSError:  # monitoring must never hurt the pipeline
            log.warning("could not write status.json", exc_info=True)

    async def run(self, duration_s: float | None = None) -> None:
        await self.source.subscribe(self.config.symbols)
        queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=10_000)
        stage = IngestStage(self.source, queue)
        if self.executor is not None:
            if self.config.flatten_on_start:
                # The local book starts empty, so any position on the account is
                # unmanaged residue (e.g. a killed process that never flattened —
                # this happened: a soak died holding ETH for four days). Start
                # from a known-flat account or PnL is unattributable.
                with contextlib.suppress(Exception):
                    await self.executor.flatten_all()
            self.equity = await self.executor.equity()
            print(f"paper equity: {self.equity:.2f}")
        started = time.monotonic()
        tasks = [
            asyncio.create_task(stage.run()),
            asyncio.create_task(self._consume(queue)),
            asyncio.create_task(self.store.run(self.tap)),
            asyncio.create_task(self._status_loop(stage, started)),
        ]
        if self.aux_source is not None:
            await self.aux_source.subscribe(self.config.cb_products)
            tasks.append(asyncio.create_task(IngestStage(self.aux_source, queue).run()))
        try:
            if duration_s is None:
                await asyncio.gather(*tasks)
            else:
                await asyncio.sleep(duration_s)
        finally:
            await self.source.close()
            if self.aux_source is not None:
                await self.aux_source.close()
            if self._order_tasks:  # let in-flight orders settle
                await asyncio.wait(self._order_tasks, timeout=15)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.executor is not None and self.config.flatten_on_exit and self.book.open_count:
                with contextlib.suppress(Exception):
                    await self.executor.flatten_all()
            await self.store.flush()
            self.store.close()
            print(
                f"shutdown: events={stage.events} orders={self.orders_submitted}"
                f" errors={self.order_errors} tap_dropped={self.tap_dropped}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the live signal pipeline (paper only).")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD"])
    parser.add_argument("--market", choices=["crypto", "stocks"], default="crypto")
    parser.add_argument(
        "--duration", type=float, default=None, help="seconds; default: run forever"
    )
    parser.add_argument("--horizon-s", type=float, default=10.0)
    parser.add_argument(
        "--model", choices=["linear", "hoeffding", "classifier"], default="linear"
    )
    parser.add_argument("--db", default="data/live.duckdb")
    parser.add_argument("--dry-run", action="store_true", help="no orders, signals only")
    parser.add_argument("--max-position-usd", type=float, default=1_000.0)
    parser.add_argument("--daily-loss-limit-usd", type=float, default=200.0)
    parser.add_argument(
        "--cb-leader",
        nargs="*",
        default=[],
        metavar="SYMBOL",
        help="symbols to give a Coinbase same-asset leader (e.g. BTC/USD)",
    )
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument("--dead-zone-bps", type=float, default=2.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    leaders = {s: f"CB:{s}" for s in args.cb_leader}
    config = PipelineConfig(
        symbols=args.symbols,
        market=args.market,
        horizon_s=args.horizon_s,
        model_kind=args.model,
        leaders=leaders or None,
        cb_products=[s.replace("/", "-") for s in args.cb_leader],
        db_path=args.db,
        dry_run=args.dry_run,
        cost_bps=args.cost_bps,
        dead_zone_bps=args.dead_zone_bps,
        limits=RiskLimits(
            max_position_usd=args.max_position_usd,
            daily_loss_limit_usd=args.daily_loss_limit_usd,
        ),
    )
    with contextlib.suppress(ImportError):
        import uvloop

        uvloop.install()
    asyncio.run(Pipeline(config, load_alpaca_config()).run(args.duration))


if __name__ == "__main__":
    main()
