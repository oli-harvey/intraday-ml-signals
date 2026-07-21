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

`on_fill` (2026-07-20, messaging review): an optional hook called with one dict
per real fill — entry, exit, or a reconciliation close — so a caller (stocks_
live.py) can post a real-time trade blotter ("what got traded, why, and what's
the cumulative position now") instead of only the periodic heartbeat. Called
SYNCHRONOUSLY on the trading path: it must not block (schedule any I/O, e.g. a
Telegram send, as its own task) or it will stall the round-trip loop.

FILL LATENCY (2026-07-21, found while reviewing the first two real sessions):
entry orders on this Alpaca paper account took 1-2.4s to confirm — 20-48% of the
5s horizon. Two consequences, both fixed here:
  1. The old sim_net_bps compared mid-AT-SIGNAL to mid-AT-EXIT, a window inflated
     by the latency, while the real net_bps correctly measures fill-to-fill. For
     a reversion signal that window mismatch alone produced comparisons wildly
     unrelated to the real trade (observed |gap| up to ~90bps on individual
     trades — many times the entire edge being chased). Fixed by comparing
     mid-AT-FILL to mid-AT-EXIT instead: same window on both sides.
  2. The true cost of the delay — how much the price moved between deciding to
     trade and actually being filled — is a REAL cost of live execution, not a
     comparison bug. It is now reported honestly as its own metric,
     entry_slippage_bps, instead of being silently baked into a wrong sim_gap.
  3. The hold time is now `horizon - measured entry latency` (floored at 0), so
     the exit fires close to horizon_ns after the SIGNAL as designed, instead of
     horizon_ns after the (delayed) fill — otherwise every trade was already
     running ~1-2s longer than the researched 5s regime before the hold timer
     even started.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import books, simrule
from .core import Prediction
from .execution.alpaca_exec import PaperExecutor

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OpenPosition:
    side: float  # +1 long, -1 short
    qty: int
    entry_fill: float
    signal_mid: float       # mid AT THE SIGNAL — basis for entry_slippage_bps
    fill_mid: float         # mid AT ENTRY-FILL CONFIRMATION — basis for sim_net_bps
    signal_spread_bps: float
    pred_bps: float
    entry_ns: int
    entry_latency_s: float  # wall-clock: order submit -> fill confirmed


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
    book_root: str | None = None  # persist the $50k stocks book here (None: off)
    on_fill: Callable[[dict], None] | None = None  # real-time blotter hook, see module docstring

    open_pos: dict[str, OpenPosition] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)  # symbols with in-flight orders
    last_seen_ns: dict[str, int] = field(default_factory=dict)
    last_mid: dict[str, float] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    realized_usd: float = 0.0
    orders: int = 0
    order_errors: int = 0
    halted: bool = False
    book_pnl_cum: float = 0.0  # lifetime stocks-book P&L (loaded across sessions)
    _tasks: set = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.book_root is not None:
            self.book_pnl_cum = books.read_stocks_pnl(self.book_root)

    def _persist_trade(self, trade: dict) -> None:
        """Append every trade to a durable JSONL — the `recent` list in
        summary() only keeps the last 10, which is exactly what made the
        latency bug above take a manual server-side investigation to find
        instead of a five-minute query. Best-effort: a logging failure must
        never affect real order handling."""
        if self.book_root is None:
            return
        try:
            path = Path(self.book_root) / "logs" / "stocks_trades.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(json.dumps({"ts": time.time(), **trade}) + "\n")
        except OSError:
            log.warning("could not persist trade record", exc_info=True)

    def _notify(self, kind: str, **fields: object) -> None:
        """Fire the blotter hook, if any. Never let a hook failure reach the
        trading loop — a Telegram/network bug in the notifier must not stop
        real orders from being managed."""
        if self.on_fill is None:
            return
        event = {"kind": kind, "balance": books.stocks_balance(self.book_pnl_cum), **fields}
        try:
            self.on_fill(event)
        except Exception:
            log.exception("on_fill hook raised for %s event", kind)

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

    async def flatten_on_start(self) -> None:
        """Startup reconciliation, parity with the crypto pipeline: `open_pos`
        starts empty on every process start (fresh restart, new session), so
        any position the BROKER still holds in our symbols is unmanaged
        residue from a prior run (a stranded exit, a killed process). Start
        from known-flat or the day's P&L is unattributable. Safe to call even
        when flat (flatten_symbols reports nothing to close)."""
        closed = await self.executor.flatten_symbols(self.symbols)
        self._record_reconciliation_closes(closed)

    # ---- order path (off the hot loop) --------------------------------------
    async def _round_trip(self, sym: str, direction: float, qty: int, p: Prediction) -> None:
        try:
            t_submit = time.monotonic()
            res = await self.executor.market_order(
                sym, "buy" if direction > 0 else "sell", qty, tif="day")
            self.orders += 1
            res = await self.executor.poll_fill(res.order_id, attempts=20, delay_s=0.25)
            if res.status != "filled" or res.filled_qty <= 0 or res.filled_avg_price <= 0:
                self.order_errors += 1
                log.warning("entry not filled: %s %s -> %s", sym, direction, res.status)
                return
            entry_latency_s = time.monotonic() - t_submit
            # mid AT FILL CONFIRMATION (not at signal) — the fair reference point
            # for sim_net_bps in _exit(), which must compare the SAME window the
            # real trade lived through, not one inflated by fill latency.
            fill_mid = self.last_mid.get(sym, res.filled_avg_price)
            self.open_pos[sym] = OpenPosition(
                side=direction, qty=int(res.filled_qty),
                entry_fill=res.filled_avg_price, signal_mid=p.mid, fill_mid=fill_mid,
                signal_spread_bps=p.spread_bps, pred_bps=p.predicted * 1e4,
                entry_ns=p.ts_ns, entry_latency_s=entry_latency_s,
            )
            self._notify(
                "entry", symbol=sym, side="long" if direction > 0 else "short",
                qty=int(res.filled_qty), price=res.filled_avg_price,
                pred_bps=p.predicted * 1e4, entry_latency_s=entry_latency_s,
            )
            # Hold for horizon MINUS what fill confirmation already cost us, so
            # the exit fires ~horizon_ns after the SIGNAL as designed — not
            # horizon_ns after a fill that itself arrived 1-2s late (2026-07-21).
            hold_s = max(0.0, self.horizon_ns / 1e9 - entry_latency_s)
            await asyncio.sleep(hold_s)
            await self._exit(sym)
        except Exception:
            self.order_errors += 1
            log.exception("round trip failed for %s", sym)
        finally:
            self.pending.discard(sym)
            self.open_pos.pop(sym, None)

    async def _exit(self, sym: str) -> None:
        pos = self.open_pos[sym]
        t_submit = time.monotonic()
        res = await self.executor.market_order(
            sym, "sell" if pos.side > 0 else "buy", pos.qty, tif="day")
        self.orders += 1
        res = await self.executor.poll_fill(res.order_id, attempts=40, delay_s=0.25)
        if res.status != "filled" or res.filled_avg_price <= 0:
            self.order_errors += 1
            log.warning("exit not confirmed for %s (%s) — position may be stranded "
                        "until close_all sweeps it", sym, res.status)
            return
        exit_latency_s = time.monotonic() - t_submit
        exit_px = res.filled_avg_price
        net_bps = pos.side * (exit_px - pos.entry_fill) / pos.entry_fill * 1e4
        pnl_usd = pos.side * (exit_px - pos.entry_fill) * pos.qty
        # Frictionless-sim counterpart of THIS trade, same WINDOW as the real
        # trade lived through: mid AT FILL (not at signal — that window is
        # inflated by entry latency, see module docstring) -> mid now.
        mid_now = self.last_mid.get(sym, exit_px)
        sim_net = (pos.side * (mid_now - pos.fill_mid) / pos.fill_mid * 1e4
                   - pos.signal_spread_bps)
        # entry_slippage_bps: the REAL cost of the delay between deciding to
        # trade and the order actually filling — a genuine execution cost, kept
        # separate from sim_gap so the two questions ("is our cost model
        # right?" vs "how much does fill latency cost?") never conflate again.
        entry_slippage_bps = (
            pos.side * (pos.entry_fill - pos.signal_mid) / pos.signal_mid * 1e4
        )
        self.realized_usd += pnl_usd
        self.book_pnl_cum += pnl_usd
        if self.book_root is not None:
            books.write_stocks_pnl(self.book_pnl_cum, self.book_root)
        trade = {
            "symbol": sym, "ts_ns": pos.entry_ns,
            "side": "long" if pos.side > 0 else "short", "qty": pos.qty,
            "pred_bps": pos.pred_bps, "entry_fill": pos.entry_fill,
            "exit_fill": exit_px, "net_bps": net_bps, "pnl_usd": pnl_usd,
            "sim_net_bps": sim_net, "spread_bps": pos.signal_spread_bps,
            "entry_slippage_bps": entry_slippage_bps,
            "entry_latency_s": pos.entry_latency_s, "exit_latency_s": exit_latency_s,
        }
        self.trades.append(trade)
        self._persist_trade(trade)
        self._notify(
            "exit", symbol=sym, side="long" if pos.side > 0 else "short",
            qty=pos.qty, entry_fill=pos.entry_fill, exit_fill=exit_px,
            net_bps=net_bps, pnl_usd=pnl_usd, sim_net_bps=sim_net,
            entry_slippage_bps=entry_slippage_bps, pred_bps=pos.pred_bps,
        )
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
        closed = await self.executor.flatten_symbols(self.symbols)
        self._record_reconciliation_closes(closed)

    def _record_reconciliation_closes(self, closed: list[dict]) -> None:
        """flatten_symbols() closes via a raw broker call that bypasses
        _round_trip/_exit entirely — without this, a stranded position (e.g. an
        exit whose fill never confirmed) would vanish from the book with no
        trade record at all (the crypto pipeline had exactly this bug: an ETH
        position closed at restart with no sell alert). Booked with real P&L,
        excluded from the strategy performance averages (net_bps/sim_gap) since
        there is no signal/sim counterpart for an orphaned position."""
        for c in closed:
            pnl = c["unrealized_pl"]
            qty, entry = c["qty"], c["avg_entry_price"]
            self.realized_usd += pnl
            self.book_pnl_cum += pnl
            if self.book_root is not None:
                books.write_stocks_pnl(self.book_pnl_cum, self.book_root)
            net_bps = (pnl / (abs(qty) * entry) * 1e4) if qty and entry else float("nan")
            trade = {
                "symbol": c["symbol"], "ts_ns": 0,
                "side": "long" if qty > 0 else "short", "qty": abs(qty),
                "pred_bps": float("nan"), "entry_fill": entry,
                "exit_fill": float("nan"), "net_bps": net_bps,
                "pnl_usd": pnl, "sim_net_bps": float("nan"),
                "spread_bps": float("nan"), "reconciliation": True,
                "entry_slippage_bps": float("nan"),
                "entry_latency_s": float("nan"), "exit_latency_s": float("nan"),
            }
            self.trades.append(trade)
            self._persist_trade(trade)
            self._notify(
                "reconciliation", symbol=c["symbol"],
                side="long" if qty > 0 else "short", qty=abs(qty),
                entry_fill=entry, pnl_usd=pnl, net_bps=net_bps,
            )

    # ---- reporting ----------------------------------------------------------
    def summary(self) -> dict:
        # reconciliation closes carry real P&L (folded into balance/cash below)
        # but have no signal/sim counterpart, so they're excluded from the
        # strategy's own performance averages — one orphaned position must not
        # silently move avg_net_bps or sim_gap_bps.
        real = [t for t in self.trades if not t.get("reconciliation")]
        n = len(real)
        nets = [t["net_bps"] for t in real]
        gaps = [t["net_bps"] - t["sim_net_bps"] for t in real]
        slips = [t["entry_slippage_bps"] for t in real]
        lats = [t["entry_latency_s"] + t["exit_latency_s"] for t in real]
        balance = books.stocks_balance(self.book_pnl_cum)  # the $50k book

        # open positions MARKED TO the latest quote (self.last_mid), so the book
        # can report a current holdings value, not just what it's holding.
        open_detail = {}
        holdings_value = 0.0
        for sym, pos in self.open_pos.items():
            mid = self.last_mid.get(sym, pos.entry_fill)
            value = pos.qty * mid
            holdings_value += value
            open_detail[sym] = {
                "side": "long" if pos.side > 0 else "short", "qty": pos.qty,
                "entry_fill": pos.entry_fill, "mid": mid, "value": value,
                "unrealized_usd": pos.side * (mid - pos.entry_fill) * pos.qty,
            }
        cash = balance - holdings_value

        return {
            "trades": n,
            "wins": sum(1 for x in nets if x > 0),
            "avg_net_bps": (sum(nets) / n) if n else float("nan"),
            "pnl_usd": self.realized_usd,
            "balance": balance,
            "pnl_cum": self.book_pnl_cum,
            "cash": cash,
            "holdings_value": holdings_value,
            "total": cash + holdings_value,  # == balance; shown for the "together" view
            # negative gap = the cost model is wrong (fixed 2026-07-21 to compare
            # the SAME window on both sides — see module docstring)
            "sim_gap_bps": (sum(gaps) / n) if n else float("nan"),
            # negative = fill latency costs us bps, separate from the cost model
            "entry_slippage_bps": (sum(slips) / n) if n else float("nan"),
            "avg_round_trip_latency_s": (sum(lats) / n) if n else float("nan"),
            "orders": self.orders,
            "order_errors": self.order_errors,
            "reconciliations": len(self.trades) - n,  # stranded closes, see note above
            "halted": self.halted,
            "open": sorted(self.open_pos),
            "open_detail": open_detail,
            "recent": self.trades[-10:],  # includes reconciliation entries — visible
        }
