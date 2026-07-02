"""One-off paper execution check: enter -> fill -> exit -> confirm flat.

Exercises PaperExecutor against the real Alpaca PAPER endpoint with a tiny
order, independent of signal quality, so the execution path is validated
before the full pipeline trades on it.
"""

from __future__ import annotations

import asyncio

from signals.config import load_alpaca_config
from signals.execution.alpaca_exec import PaperExecutor

NOTIONAL_USD = 25.0


async def main() -> None:
    cfg = load_alpaca_config()
    executor = PaperExecutor(cfg)
    equity = await executor.equity()
    print(f"paper equity: {equity:.2f}")

    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoLatestQuoteRequest

    quote = await asyncio.to_thread(
        lambda: CryptoHistoricalDataClient(cfg.api_key, cfg.secret_key)
        .get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols="BTC/USD"))["BTC/USD"]
    )
    mid = 0.5 * (quote.bid_price + quote.ask_price)
    qty = round(NOTIONAL_USD / mid, 6)
    print(f"BTC mid={mid:.2f} -> qty={qty}")

    buy = await executor.market_order("BTC/USD", "buy", qty)
    buy = await executor.poll_fill(buy.order_id)
    print(f"BUY  {buy.status}: qty={buy.filled_qty} @ {buy.filled_avg_price:.2f}")
    assert buy.status == "filled", f"buy not filled: {buy}"

    held = await executor.position_qty("BTC/USD")
    print(f"position after buy: {held}")
    assert held > 0

    sell = await executor.market_order("BTC/USD", "sell", held)
    sell = await executor.poll_fill(sell.order_id)
    print(f"SELL {sell.status}: qty={sell.filled_qty} @ {sell.filled_avg_price:.2f}")
    assert sell.status == "filled", f"sell not filled: {sell}"

    flat = await executor.position_qty("BTC/USD")
    print(f"position after sell: {flat}")
    assert flat == 0.0
    round_trip = (sell.filled_avg_price - buy.filled_avg_price) * sell.filled_qty
    print(f"round-trip cost/pnl: {round_trip:+.4f} USD (spread+slippage)")
    print("PASS: full order lifecycle verified on paper endpoint")


if __name__ == "__main__":
    asyncio.run(main())
