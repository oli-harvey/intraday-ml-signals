"""The 50/50 virtual split of the shared paper account.

Invariant: crypto book + stocks book == account equity, with all pre-split
history (the small crypto loss) attributed to crypto and the stocks book opening
at exactly $50k.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from signals.books import (
    ALLOC_STOCKS,
    crypto_balance,
    read_stocks_pnl,
    stocks_balance,
    write_stocks_pnl,
)


def test_split_attributes_history_to_crypto_and_sums_to_equity():
    equity = 99_930.0  # the account after the pre-split crypto loss
    assert stocks_balance(0.0) == 50_000.0          # stocks opens clean
    assert crypto_balance(equity, 0.0) == 49_930.0  # crypto absorbs the loss
    # invariant holds for any later state
    spnl = +12.34
    assert stocks_balance(spnl) + crypto_balance(equity + spnl, spnl) == equity + spnl


def test_pnl_file_round_trip_and_missing_default(tmp_path: Path):
    assert read_stocks_pnl(tmp_path) == 0.0  # no file yet -> clean $50k book
    write_stocks_pnl(-3.21, tmp_path)
    assert read_stocks_pnl(tmp_path) == -3.21
    assert (tmp_path / "data" / "stocks_book.json").exists()


def test_trader_persists_book_across_sessions(tmp_path: Path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_stockstrader import HN, FakeExecutor, make, pred

    async def session(prices, prior_root):
        ex = FakeExecutor(prices)
        tr = make(ex, book_root=str(prior_root))
        tr.on_prediction(pred())
        await asyncio.sleep(HN / 1e9 + 0.15)
        return tr

    # session 1: +$0.45 realized -> book file written
    tr1 = asyncio.run(session([900.10, 900.55], tmp_path))
    assert abs(tr1.book_pnl_cum - 0.45) < 1e-9
    assert abs(read_stocks_pnl(tmp_path) - 0.45) < 1e-9
    # session 2 (fresh process): loads the prior book and keeps accumulating
    tr2 = asyncio.run(session([900.00, 899.60], tmp_path))
    assert abs(tr2.book_pnl_cum - (0.45 - 0.40)) < 1e-9
    assert abs(tr2.summary()["balance"] - (ALLOC_STOCKS + 0.05)) < 1e-9
