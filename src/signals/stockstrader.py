"""Live PAPER equities trader — the real-fills test of the tracked config.

Decision (2026-07-18, Oli): stop waiting for the shadow tally to mature and measure
the edge properly — real paper orders, real fills, real slippage. The backtest's
cost model (enter/exit at NBBO, pay the spread) becomes an empirical question:
every trade records its own frictionless-sim counterpart, so the nightly digest
can print the REALITY GAP (paper net minus sim net) per trade.

Design:
  - Entry at PREDICTION time. The shadow book (livesim) books trades when labels
    resolve — retrospectively. A real trader acts on the forecast: same simrule,
    same dead zone, same spread gate, windowed cadence (one look per horizon per
    symbol — the tracked config's cadence; phase re-rolls daily, RESEARCH 07-17).
  - Whole shares only (Alpaca cannot short fractionals): qty = notional // mid,
    skip if that rounds to zero.
  - One position per symbol, max_open in total, entries halt at the daily loss
    cap and at session close.
  - Exit = market order after the horizon elapses (wall clock ≈ event clock live).
  - The paper account is SHARED with the crypto pipeline: all cleanup goes
    through executor.flatten_symbols(OUR symbols), never account-wide.

We keep the shadow books running unchanged next to this — three measurements of
the same config (backtest, shadow, paper) that must agree or explain themselves.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from . import simrule
from .core import Prediction
from .execution.alpaca_exec import PaperExecutor

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OpenPosition:
    side: float  # +1 long, -1 short
    qty: int
    entry_fill: float
    signal_mid: float
    signal_spread_bps: float
    pred_bps: float
    entry_ns: int


@dataclass
class StocksTrader:
    executor: PaperExecutor
    symbols: list[str]
    horizon_ns: int
    dead_zone_bps: float = 4.0
    max_spread_bps: float | None = 2.0
    notional: float = 1_000.0
    max_open: int = 2
    daily_loss_cap_usd: float = 25.0
    allow_short: bool = True

    open_pos: dict[str, OpenPosition] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)  # symbols with in-flight orders
    last_seen_ns: dict[str, int] = field(default_factory=dict)
    last_mid: dict[str, float] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    realized_usd: float = 0.0
    orders: int = 0
    order_errors: int = 0
    halted: bool = False
    _tasks: set = field(default_factory=set)

    # ---- hot path -----------------------------------------------------------
    def on_prediction(self, p: Prediction) -> None:
        """Called for every live prediction; spawns an order task on entry."""
        sym = p.symbol
        self.last_mid[sym] = p.mid
        if sym not in self.symbols or self.halted:
            return
        # windowed cadence: one look per horizon window per symbol
        if p.ts_ns < self.last_seen_ns.get(sym, -(1 << 62)) + self.horizon_ns:
            return
        self.last_seen_ns[sym] = p.ts_ns
        if sym in self.open_pos or sym in self.pending:
            return
        if len(self.open_pos) + len(self.pending) >= self.max_open:
            return
        direction = simrule.decide(
            p.predicted, p.spread_bps, fee_bps=0.0,
            dead_zone_bps=self.dead_zone_bps, allow_short=self.allow_short,
            max_spread_bps=self.max_spread_bps,
        )
        if direction == 0.0:
            return
        qty = int(self.notional // p.mid)
        if qty < 1:
            return  # whole shares only (shorts cannot be fractional)
        self.pending.add(sym)
        task = asyncio.create_task(self._round_trip(sym, direction, qty, p))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- order path (off the hot loop) --------------------------------------
    async def _round_trip(self, sym: str, direction: float, qty: int, p: Prediction) -> None:
        try:
            res = await self.executor.market_order(
                sym, "buy" if direction > 0 else "sell", qty, tif="day")
            self.orders += 1
            res = await self.executor.poll_fill(res.order_id, attempts=20, delay_s=0.25)
            if res.status != "filled" or res.filled_qty <= 0 or res.filled_avg_price <= 0:
                self.order_errors += 1
                log.warning("entry not filled: %s %s -> %s", sym, direction, res.status)
                return
            self.open_pos[sym] = OpenPosition(
                side=direction, qty=int(res.filled_qty),
                entry_fill=res.filled_avg_price, signal_mid=p.mid,
                signal_spread_bps=p.spread_bps, pred_bps=p.predicted * 1e4,
                entry_ns=p.ts_ns,
            )
            await asyncio.sleep(self.horizon_ns / 1e9)  # hold for the horizon
            await self._exit(sym)
        except Exception:
            self.order_errors += 1
            log.exception("round trip failed for %s", sym)
        finally:
            self.pending.discard(sym)
            self.open_pos.pop(sym, None)

    async def _exit(self, sym: str) -> None:
        pos = self.open_pos[sym]
        res = await self.executor.market_order(
            sym, "sell" if pos.side > 0 else "buy", pos.qty, tif="day")
        self.orders += 1
        res = await self.executor.poll_fill(res.order_id, attempts=40, delay_s=0.25)
        if res.status != "filled" or res.filled_avg_price <= 0:
            self.order_errors += 1
            log.warning("exit not confirmed for %s (%s) — position may be stranded "
                        "until close_all sweeps it", sym, res.status)
            return
        exit_px = res.filled_avg_price
        net_bps = pos.side * (exit_px - pos.entry_fill) / pos.entry_fill * 1e4
        pnl_usd = pos.side * (exit_px - pos.entry_fill) * pos.qty
        # frictionless-sim counterpart of THIS trade: mid at signal -> mid now,
        # charged the quoted spread — what the backtest/shadow book would book.
        mid_now = self.last_mid.get(sym, exit_px)
        sim_net = (pos.side * (mid_now - pos.signal_mid) / pos.signal_mid * 1e4
                   - pos.signal_spread_bps)
        self.realized_usd += pnl_usd
        self.trades.append({
            "symbol": sym, "ts_ns": pos.entry_ns,
            "side": "long" if pos.side > 0 else "short", "qty": pos.qty,
            "pred_bps": pos.pred_bps, "entry_fill": pos.entry_fill,
            "exit_fill": exit_px, "net_bps": net_bps, "pnl_usd": pnl_usd,
            "sim_net_bps": sim_net, "spread_bps": pos.signal_spread_bps,
        })
        if self.realized_usd <= -self.daily_loss_cap_usd:
            self.halted = True
            log.warning("daily loss cap hit ($%.2f) — entries halted", self.realized_usd)

    async def close_all(self) -> None:
        """Session end: stop entries, let in-flight round trips settle, then
        sweep anything stranded in OUR symbols (never the shared account)."""
        self.halted = True
        if self._tasks:
            _, still_running = await asyncio.wait(
                self._tasks, timeout=self.horizon_ns / 1e9 + 30)
            for task in still_running:
                # a straggler's exit order landing AFTER the sweep would OPEN a
                # fresh position on a flat book — cancel before sweeping
                task.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)
        await self.executor.flatten_symbols(self.symbols)

    # ---- reporting ----------------------------------------------------------
    def summary(self) -> dict:
        n = len(self.trades)
        nets = [t["net_bps"] for t in self.trades]
        gaps = [t["net_bps"] - t["sim_net_bps"] for t in self.trades]
        return {
            "trades": n,
            "wins": sum(1 for x in nets if x > 0),
            "avg_net_bps": (sum(nets) / n) if n else float("nan"),
            "pnl_usd": self.realized_usd,
            # negative gap = reality pays more toll than the sim charges
            "sim_gap_bps": (sum(gaps) / n) if n else float("nan"),
            "orders": self.orders,
            "order_errors": self.order_errors,
            "halted": self.halted,
            "open": sorted(self.open_pos),
            "recent": self.trades[-10:],
        }
