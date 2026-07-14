"""Live shadow trading book for the equities session.

We do NOT trade stocks. This books the tracked config's trades *as they would have
happened*, live, on the real feed — so the Telegram bot can answer "how many stocks
were traded and what did they make" during the session instead of only after the
nightly replay.

It is fed the same `Resolved` labels the model learns from, so a trade is booked at
the moment its 5s horizon actually elapses on live data. The entry/exit rule comes
from `simrule`, shared verbatim with the offline backtest — if this ever disagrees
with the nightly digest, that is a bug, not a discrepancy to explain away.

One position at a time per symbol, exactly as the backtest sequences them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import simrule


@dataclass
class SymbolBook:
    trades: int = 0
    wins: int = 0
    net_bps_sum: float = 0.0
    busy_until_ns: int = -(10**18)
    last_seen_ns: int = -(10**18)  # windowed cadence: last signal we even LOOKED at

    @property
    def avg_net_bps(self) -> float:
        return self.net_bps_sum / self.trades if self.trades else float("nan")

    @property
    def hit_rate(self) -> float:
        return self.wins / self.trades if self.trades else float("nan")


@dataclass
class LiveSim:
    """Shadow book across all symbols. `fee_bps=0` — US equities are commission-free,
    so the round-trip toll is the spread, which simrule already charges.

    CADENCE — this is not a detail, it is ~3x of the edge (RESEARCH.md 2026-07-14):

    * windowed=True  — look at the signal once per horizon window, act on that
      reading. This is what `evaluate(non_overlapping=True)` does, i.e. what every
      headline number in the research so far describes. NVDA 07-09: 138 trades,
      +3.32bps.
    * windowed=False — look at every quote and enter on the first signal that clears
      the bar. NVDA 07-09: 1,435 trades, +1.09bps. Lower, because entering on the
      first threshold UPCROSSING systematically buys noise spikes.

    The gap is the difference between surviving a 1bp slippage haircut and not. We
    run both books live so the number can't quietly mean the wrong thing.
    """

    horizon_ns: int
    dead_zone_bps: float = 4.0
    max_spread_bps: float | None = 2.0
    fee_bps: float = 0.0
    allow_short: bool = True
    windowed: bool = True
    books: dict[str, SymbolBook] = field(default_factory=dict)
    recent: list[dict] = field(default_factory=list)  # last N trades, for the bot

    def book_for(self, symbol: str) -> SymbolBook:
        b = self.books.get(symbol)
        if b is None:
            b = self.books[symbol] = SymbolBook()
        return b

    def on_resolved(self, symbol: str, ts_ns: int, prediction: float,
                    realized: float, spread_bps: float) -> dict | None:
        """Feed one resolved label. Returns the booked trade, or None if we stood
        aside (wide spread, weak signal, already in a position, or — in windowed
        cadence — this signal falls inside a window we have already sampled)."""
        book = self.book_for(symbol)
        if self.windowed:
            # mirror evaluate(non_overlapping=True): one look per horizon window
            if ts_ns < book.last_seen_ns + self.horizon_ns:
                return None
            book.last_seen_ns = ts_ns
        if ts_ns < book.busy_until_ns:
            return None  # position still open from an earlier signal
        direction = simrule.decide(
            prediction, spread_bps, fee_bps=self.fee_bps,
            dead_zone_bps=self.dead_zone_bps, allow_short=self.allow_short,
            max_spread_bps=self.max_spread_bps,
        )
        if direction == 0.0:
            return None

        pnl = simrule.net_bps(direction, realized, spread_bps, self.fee_bps)
        book.trades += 1
        book.wins += 1 if pnl > 0 else 0
        book.net_bps_sum += pnl
        book.busy_until_ns = ts_ns + self.horizon_ns

        trade = {
            "symbol": symbol, "ts_ns": ts_ns,
            "side": "long" if direction > 0 else "short",
            "pred_bps": prediction * 1e4, "realized_bps": realized * 1e4,
            "spread_bps": spread_bps, "net_bps": pnl,
        }
        self.recent.append(trade)
        del self.recent[:-20]  # keep the tail bounded
        return trade

    @property
    def total_trades(self) -> int:
        return sum(b.trades for b in self.books.values())

    @property
    def total_net_bps(self) -> float:
        return sum(b.net_bps_sum for b in self.books.values())

    @property
    def total_wins(self) -> int:
        return sum(b.wins for b in self.books.values())

    def summary(self) -> dict:
        """Snapshot for status_stocks.json / the Telegram bot."""
        n = self.total_trades
        return {
            "trades": n,
            "wins": self.total_wins,
            "hit_rate": (self.total_wins / n) if n else float("nan"),
            "net_bps_sum": self.total_net_bps,
            "avg_net_bps": (self.total_net_bps / n) if n else float("nan"),
            "by_symbol": {
                s: {"trades": b.trades, "net_bps": b.net_bps_sum,
                    "avg_net_bps": b.avg_net_bps, "hit_rate": b.hit_rate}
                for s, b in sorted(self.books.items()) if b.trades
            },
            "recent": self.recent[-10:],
        }
