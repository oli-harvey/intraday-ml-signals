"""Virtual sub-accounts on the ONE shared Alpaca paper account.

2026-07-19 (Oli): split the paper funds — half to crypto, half to stocks — and
report each separately. The account itself cannot be split (one paper account,
one equity number), so this is bookkeeping:

  stocks book = $50k + cumulative stocks P&L   (persisted: data/stocks_book.json,
                                                written by StocksTrader per exit)
  crypto book = account equity − stocks book   (the remainder)

The $100k account had already lost a small amount to crypto trading before the
split, so the crypto book opens at ~$49.93k and the stocks book at exactly $50k —
"the lost small amount was lost on crypto so that comes out of that."

The remainder construction keeps the invariant crypto + stocks == account equity
by definition, needs no changes to the crypto pipeline, and attributes each
book's own realized AND unrealized to itself (a stock position's mark-to-market
sits in account equity for the ~5s it is open; at that scale the cross-leak is
noise). Stdlib only — imported by the alert/digest scripts.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

INITIAL_ACCOUNT = 100_000.0
ALLOC_STOCKS = 50_000.0
ALLOC_CRYPTO = INITIAL_ACCOUNT - ALLOC_STOCKS

STOCKS_BOOK_FILE = "data/stocks_book.json"


def read_stocks_pnl(root: Path | str = ".") -> float:
    """Cumulative stocks paper P&L since the split (0.0 before the first trade)."""
    try:
        return float(json.loads((Path(root) / STOCKS_BOOK_FILE).read_text())["pnl_cum"])
    except (OSError, ValueError, KeyError):
        return 0.0


def write_stocks_pnl(pnl_cum: float, root: Path | str = ".") -> None:
    path = Path(root) / STOCKS_BOOK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"pnl_cum": pnl_cum, "ts": time.time()}, fh)
    os.replace(tmp, path)  # atomic: readers never see a half file


def stocks_balance(stocks_pnl_cum: float) -> float:
    return ALLOC_STOCKS + stocks_pnl_cum


def crypto_balance(account_equity: float, stocks_pnl_cum: float) -> float:
    """Whatever the stocks book doesn't own is crypto's — including all pre-split
    history."""
    return account_equity - stocks_balance(stocks_pnl_cum)
