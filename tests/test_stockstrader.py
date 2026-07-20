"""The paper trader must follow simrule exactly and never endanger the shared account.

Pure-logic tests against a scripted executor: no network, tiny horizon so held
positions resolve in milliseconds.
"""

from __future__ import annotations

import asyncio

from signals.core import Prediction
from signals.execution.alpaca_exec import OrderResult
from signals.stockstrader import StocksTrader

HN = 40_000_000  # 40ms horizon: real holds, fast tests


class FakeExecutor:
    """Fills every market order instantly at a scripted price."""

    def __init__(self, prices: list[float]):
        self.prices = list(prices)
        self.orders: list[tuple[str, str, float, str]] = []
        self.flattened: list[list[str]] | None = None
        self._i = 0

    async def market_order(self, symbol, side, qty, tif="gtc"):
        self.orders.append((symbol, side, qty, tif))
        px = self.prices[min(self._i, len(self.prices) - 1)]
        self._i += 1
        return OrderResult(f"id{self._i}", "filled", qty, px)

    async def poll_fill(self, order_id, attempts=10, delay_s=0.5):
        i = int(order_id[2:])
        return OrderResult(order_id, "filled", 1, self.prices[min(i - 1, len(self.prices) - 1)])

    async def flatten_symbols(self, symbols):
        self.flattened = (self.flattened or []) + [list(symbols)]


def pred(sym="NVDA", ts=0, predicted=0.0010, mid=900.0, spread=1.0):
    return Prediction(symbol=sym, ts_ns=ts, predicted=predicted, mid=mid,
                      spread_bps=spread, proc_us=1.0)


def make(execu, **kw) -> StocksTrader:
    args = {"executor": execu, "symbols": ["NVDA", "AAPL"], "horizon_ns": HN,
            "dead_zone_bps": 4.0, "max_spread_bps": 2.0, "notional": 1000.0,
            "max_open": 2, "daily_loss_cap_usd": 25.0}
    args.update(kw)
    return StocksTrader(**args)


def run(coro):
    return asyncio.run(coro)


def test_long_round_trip_books_real_fill_pnl():
    async def go():
        ex = FakeExecutor([900.10, 900.55])  # entry fill, exit fill
        tr = make(ex)
        tr.on_prediction(pred(predicted=+0.0010))  # +10bps forecast clears dz4
        await asyncio.sleep(HN / 1e9 + 0.15)
        return ex, tr
    ex, tr = run(go())
    assert [o[:2] for o in ex.orders] == [("NVDA", "buy"), ("NVDA", "sell")]
    assert all(o[3] == "day" for o in ex.orders)  # equities market orders are DAY
    (t,) = tr.trades
    assert t["qty"] == 1  # 1000 // 900 — whole shares only
    assert abs(t["net_bps"] - (900.55 - 900.10) / 900.10 * 1e4) < 1e-9
    assert abs(t["pnl_usd"] - 0.45) < 1e-9
    assert tr.realized_usd > 0


def test_short_round_trip_profits_on_fall():
    async def go():
        ex = FakeExecutor([899.90, 899.30])  # sell high, buy back lower
        tr = make(ex)
        tr.on_prediction(pred(predicted=-0.0010))
        await asyncio.sleep(HN / 1e9 + 0.15)
        return ex, tr
    ex, tr = run(go())
    assert [o[:2] for o in ex.orders] == [("NVDA", "sell"), ("NVDA", "buy")]
    (t,) = tr.trades
    assert t["side"] == "short" and t["pnl_usd"] > 0


def test_windowed_cadence_and_busy_gate_block_reentry():
    async def go():
        ex = FakeExecutor([900.0, 900.0])
        tr = make(ex)
        tr.on_prediction(pred(ts=0))
        tr.on_prediction(pred(ts=HN // 2))      # same window -> ignored
        tr.on_prediction(pred(ts=HN + 1))       # new window but position open -> ignored
        await asyncio.sleep(HN / 1e9 + 0.15)
        return ex, tr
    ex, tr = run(go())
    assert len(tr.trades) == 1
    assert len(ex.orders) == 2  # one entry + one exit only


def test_no_entry_when_spread_wide_or_signal_weak_or_price_too_high():
    async def go():
        ex = FakeExecutor([900.0])
        tr = make(ex)
        tr.on_prediction(pred(spread=6.0))            # spread gate
        tr.on_prediction(pred(ts=HN * 2, predicted=0.00001))  # dead zone
        tr.on_prediction(pred(ts=HN * 4, mid=2500.0))  # 1000 // 2500 = 0 shares
        await asyncio.sleep(0.05)
        return ex, tr
    ex, tr = run(go())
    assert ex.orders == [] and tr.trades == []


def test_loss_cap_halts_entries():
    async def go():
        # huge adverse move: buy 900, exit 870 -> -$30 on 1 share > $25 cap
        ex = FakeExecutor([900.0, 870.0, 900.0, 900.0])
        tr = make(ex)
        tr.on_prediction(pred(ts=0))
        await asyncio.sleep(HN / 1e9 + 0.15)
        assert tr.halted
        tr.on_prediction(pred(ts=HN * 10))  # after the cap -> refused
        await asyncio.sleep(0.05)
        return ex, tr
    ex, tr = run(go())
    assert len(tr.trades) == 1
    assert len(ex.orders) == 2


def test_close_all_halts_and_sweeps_only_own_symbols():
    async def go():
        ex = FakeExecutor([900.0, 900.2])
        tr = make(ex)
        tr.on_prediction(pred(ts=0))
        await tr.close_all()
        tr.on_prediction(pred(ts=HN * 10))  # after close -> refused
        await asyncio.sleep(0.05)
        return ex, tr
    ex, tr = run(go())
    assert ex.flattened == [["NVDA", "AAPL"]]  # never account-wide
    assert tr.halted


def test_cli_trade_universe_defaults_to_all_captured_symbols():
    """2026-07-19 (Oli): 'not just nvda and aapl, any stocks that are predicted
    to be profitable' — the default trade universe is the whole capture list;
    simrule picks per-signal. Pin the CLI resolution (silent no-op wiring has
    bitten this repo before)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from stocks_live import build_parser, resolve_args

    args = resolve_args(build_parser().parse_args(
        ["--symbols", "SPY", "NVDA", "F", "--trade"]))
    assert args.trade_symbols == ["SPY", "NVDA", "F"]
    # an explicit list still wins
    args = resolve_args(build_parser().parse_args(
        ["--symbols", "SPY", "NVDA", "F", "--trade", "--trade-symbols", "NVDA"]))
    assert args.trade_symbols == ["NVDA"]


def test_summary_marks_open_position_to_latest_quote_and_splits_the_book():
    """2026-07-20 (Oli): current value of holdings, plus cash/holdings/total for
    the book. While a position is open, summary() must mark it to the latest
    quote (not the entry fill), and cash+holdings must equal the book balance."""
    async def go():
        ex = FakeExecutor([900.10, 900.10])  # entry fill, exit fill
        tr = make(ex)
        tr.on_prediction(pred(mid=900.0))
        await asyncio.sleep(0.01)  # still within the 40ms horizon: position open
        # a later tick for the same symbol updates last_mid even though the
        # windowed cadence blocks a second entry
        tr.on_prediction(pred(ts=5_000_000, mid=905.0))
        s_open = tr.summary()
        await asyncio.sleep(HN / 1e9 + 0.05)  # let the round trip finish
        return tr, s_open
    tr, s_open = run(go())

    d = s_open["open_detail"]["NVDA"]
    assert d["qty"] == 1 and d["mid"] == 905.0  # marked to the LATEST quote
    assert abs(d["value"] - 905.0) < 1e-9
    assert abs(d["unrealized_usd"] - (905.0 - 900.10)) < 1e-6  # vs the entry FILL
    assert abs(s_open["holdings_value"] - d["value"]) < 1e-9
    assert abs(s_open["cash"] - (s_open["balance"] - d["value"])) < 1e-9
    assert abs(s_open["total"] - s_open["balance"]) < 1e-6  # cash+holdings==balance

    s_closed = tr.summary()
    assert s_closed["open_detail"] == {} and s_closed["holdings_value"] == 0.0


def test_summary_reports_reality_gap():
    async def go():
        ex = FakeExecutor([900.10, 900.55])
        tr = make(ex)
        tr.on_prediction(pred())
        await asyncio.sleep(HN / 1e9 + 0.15)
        return tr.summary()
    s = run(go())
    assert s["trades"] == 1 and s["orders"] == 2
    assert s["sim_gap_bps"] == s["sim_gap_bps"]  # not NaN
    assert "recent" in s and s["recent"][0]["exit_fill"] == 900.55
