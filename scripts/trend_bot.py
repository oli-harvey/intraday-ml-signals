"""$100 long-only QQQ trend filter — the only strategy in this repo that is meant
to hold real money.

Why this and not the ML system: the intraday track is closed (RESEARCH 2026-08-07
— the edge is real and dies within 500ms of the signal, which is inside the
1-2.4s it takes an order to reach the exchange). This strategy needs no edge and
no speed. It is index beta with a trend filter, which is drawdown insurance, NOT
alpha, and it is described that way everywhere it reports.

Rule (evaluated once per day, near the close):
    hold QQQ while close > SMA(200); otherwise sit in cash.

Backtested 5y with real costs (half-spreads from our own captured quotes + SEC
fee) and the real small-account constraints — see RESEARCH 2026-07-21 evening:
    $100 -> ~$214, 16.4% CAGR, max drawdown -13.6% (vs buy-and-hold -35.6%)
    ~4 trades/year, ZERO day trades (PDT-safe under $25k), costs in cents.

**The 200-day window is the published Faber default, deliberately NOT the window
that topped our own sweep.** Every window we tested (20/50/100/200) was positive
on SPY/QQQ/NVDA, which is the honest justification for using the trend filter at
all; picking the best-scoring one afterwards would be the exact cherry-pick this
project spent a month learning to refuse.

Safety:
  - PAPER ONLY. Refuses to run against a non-paper base_url.
  - Touches QQQ and nothing else. Never calls flatten_all; the account is shared.
  - Acts only in the last 30 minutes of a real trading session (so a market order
    fills near the close, which is the price the backtest assumed), at most once
    per day, and only when Alpaca's own clock says the market is open.
  - The BROKER is the source of truth for whether we hold; the state file is only
    for once-a-day idempotence and reporting.

Messages (deliberately few — silence means the rule said "no change"):
  - on a trade: what was bought/sold and why
  - Fridays: a one-line position + P&L summary, which doubles as the liveness
    proof now that the research digest is gone
"""

from __future__ import annotations

import argparse
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path

from signals import telegram as tg

try:  # this Mac's Python has no system CA bundle in OpenSSL; the Linux server does
    import certifi

    _SSL: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = None

NY = zoneinfo.ZoneInfo("America/New_York")
SYMBOL = "QQQ"
SMA_DAYS = 200
DATA_URL = "https://data.alpaca.markets"
ACT_WITHIN_MIN = 30  # only trade in the last N minutes of the session


def _req(url: str, creds: dict[str, str], method: str = "GET",
         body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": creds["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": creds["ALPACA_SECRET_KEY"],
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30, context=_SSL) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def decide(close: float, sma: float, holding: bool) -> str | None:
    """The whole strategy. Returns 'buy', 'sell', or None (no change).

    Pure and total — every branch is unit-tested, because this function is the
    only thing standing between the rule and real money."""
    if close != close or sma != sma:  # NaN guard: never trade on missing data
        return None
    above = close > sma
    if above and not holding:
        return "buy"
    if not above and holding:
        return "sell"
    return None


def sma_from_closes(closes: list[float], window: int = SMA_DAYS) -> float:
    """Simple moving average of the last `window` closes; NaN if not enough
    history (which `decide` then refuses to trade on, rather than guessing)."""
    if len(closes) < window:
        return float("nan")
    return sum(closes[-window:]) / window


def fetch_closes(creds: dict[str, str], symbol: str = SYMBOL,
                 days: int = 400) -> list[float]:
    """Split-adjusted daily closes, oldest first. 400 calendar days comfortably
    covers a 200-TRADING-day window."""
    start = (datetime.now(tz=NY) - timedelta(days=days)).date().isoformat()
    params = urllib.parse.urlencode({
        "timeframe": "1Day", "start": start, "limit": "10000",
        "adjustment": "split", "feed": "sip", "sort": "asc",
    })
    out = _req(f"{DATA_URL}/v2/stocks/{symbol}/bars?{params}", creds)
    return [b["c"] for b in out.get("bars", [])]


def market_is_closing(creds: dict[str, str], base: str,
                      within_min: int = ACT_WITHIN_MIN) -> bool:
    """True only inside the last `within_min` minutes of an OPEN session.

    Uses Alpaca's own clock, so holidays and early closes are handled by the
    exchange calendar instead of by us guessing."""
    clock = _req(f"{base}/v2/clock", creds)
    if not clock.get("is_open"):
        return False
    close_at = datetime.fromisoformat(clock["next_close"])
    now = datetime.fromisoformat(clock["timestamp"])
    return timedelta(0) <= (close_at - now) <= timedelta(minutes=within_min)


def position_qty(creds: dict[str, str], base: str, symbol: str = SYMBOL) -> float:
    try:
        pos = _req(f"{base}/v2/positions/{symbol}", creds)
        return float(pos.get("qty", 0.0))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 0.0  # no position is a normal state, not an error
        raise


def submit(creds: dict[str, str], base: str, side: str, *, notional: float | None = None,
           qty: float | None = None, symbol: str = SYMBOL) -> dict:
    """Market DAY order. Buys by notional (fractional shares — $100 does not buy
    a whole QQQ share); sells the whole position by qty."""
    body: dict[str, object] = {"symbol": symbol, "side": side, "type": "market",
                               "time_in_force": "day"}
    if notional is not None:
        body["notional"] = f"{notional:.2f}"
    else:
        body["qty"] = f"{qty}"
    return _req(f"{base}/v2/orders", creds, method="POST", body=body)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--env", default="/home/deploy/digest.env")
    ap.add_argument("--notional", type=float, default=100.0)
    ap.add_argument("--sma-days", type=int, default=SMA_DAYS)
    ap.add_argument("--no-send", action="store_true",
                    help="print instead of Telegram AND place no orders (dry run)")
    ap.add_argument("--ignore-clock", action="store_true",
                    help="skip the market-hours gate (testing only; still no orders "
                         "unless --no-send is absent)")
    args = ap.parse_args()
    root = Path(args.root)
    # Two credential files by design: the repo .env holds the BROKER keys (never
    # copied around), digest.env holds the Telegram creds shared with the other
    # bots. We need both, so merge — Telegram file last, it is the one passed in.
    def _env(path: str) -> dict[str, str]:
        try:
            return tg.load_env(path)
        except OSError:
            return {}  # a missing file is reported by the credential check below

    creds = {**_env(str(root / ".env")), **_env(args.env)}
    missing = [k for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY") if not creds.get(k)]
    if missing:
        raise SystemExit(f"missing broker credentials: {', '.join(missing)}")
    base = creds.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    if "paper-api" not in base:
        raise SystemExit(f"refusing to run against a non-paper endpoint: {base}")

    state_path = root / "data" / "trend_bot_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}

    now = datetime.now(tz=NY)
    today = now.date().isoformat()
    if state.get("last_day") == today and not args.no_send:
        print(f"{today}: already ran today")
        return
    if not args.ignore_clock and not market_is_closing(creds, base):
        print(f"{now:%H:%M} ET: not in the closing window — no action")
        return

    closes = fetch_closes(creds, days=max(400, args.sma_days * 2))
    sma = sma_from_closes(closes, args.sma_days)
    close = closes[-1] if closes else float("nan")
    qty = position_qty(creds, base)
    holding = qty > 0
    action = decide(close, sma, holding)

    gap_pct = (close / sma - 1) * 100 if sma == sma else float("nan")
    print(f"{today}: close {close:.2f} sma{args.sma_days} {sma:.2f} "
          f"({gap_pct:+.1f}%) holding={holding} qty={qty} -> {action or 'no change'}")

    msg = None
    if action == "buy":
        if not args.no_send:
            submit(creds, base, "buy", notional=args.notional)
        msg = (f"\N{CHART WITH UPWARDS TREND} <b>BOUGHT ${args.notional:,.0f} {SYMBOL}</b>\n"
               f"close ${close:,.2f} is {gap_pct:+.1f}% above its {args.sma_days}d average "
               f"(${sma:,.2f}) \N{EM DASH} trend filter says hold.\n"
               f"<i>Index exposure with a trend filter: drawdown insurance, not alpha.</i>")
    elif action == "sell":
        if not args.no_send:
            submit(creds, base, "sell", qty=qty)
        msg = (f"\N{CHEQUERED FLAG} <b>SOLD {qty} {SYMBOL} \N{RIGHTWARDS ARROW} cash</b>\n"
               f"close ${close:,.2f} fell {gap_pct:+.1f}% below its {args.sma_days}d average "
               f"(${sma:,.2f}) \N{EM DASH} sitting out until it recovers.")
    elif now.weekday() == 4:  # Friday: the one routine message, also the liveness proof
        value = qty * close
        msg = (f"\N{BAR CHART} <b>{SYMBOL} trend filter \N{MIDDLE DOT} weekly</b>\n"
               f"{'holding ' + f'{qty:g} sh (${value:,.2f})' if holding else 'in cash'} "
               f"\N{MIDDLE DOT} close ${close:,.2f} vs {args.sma_days}d ${sma:,.2f} "
               f"({gap_pct:+.1f}%)\nno change this week.")

    if msg:
        if args.no_send:
            print(msg)
        else:
            tg.send(creds, msg)

    if not args.no_send:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "last_day": today, "close": close, "sma": sma,
            "holding": holding, "qty": qty, "action": action,
        }))


if __name__ == "__main__":
    main()
