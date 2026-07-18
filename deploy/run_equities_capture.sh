#!/usr/bin/env bash
# DST-proof US-equities session capture.
#
# A fixed-UTC cron can't track daylight saving: 09:30 ET = 13:30 UTC in EDT but
# 14:30 UTC in EST, so the old `30 14 * * 1-5` line was an hour late half the year
# (missed the opening hour, recorded an hour of after-hours). This wrapper asks the
# OS tz database (America/New_York) for the real session and records 09:30->16:00 ET
# regardless of season. Cron starts it at 13:00 UTC daily (before both possible
# opens); it waits until the bell, then records until the close.
#
# Cron (MERGE): 0 13 * * 1-5 cd $HOME/intraday-ml-signals && bash deploy/run_equities_capture.sh >> logs/equities_cron.log 2>&1
# Env: DRY_RUN=1 prints the computed plan and exits (no recording) — used to verify.
set -euo pipefail
cd "$HOME/intraday-ml-signals" 2>/dev/null || cd "$(dirname "$0")/.."

dow=$(TZ=America/New_York date +%u)  # 1=Mon .. 7=Sun
if [ "$dow" -ge 6 ]; then echo "$(date -u +%FT%TZ) weekend (dow=$dow) — skip"; exit 0; fi

open_et=$(TZ=America/New_York date -d "09:30" +%s)
close_et=$(TZ=America/New_York date -d "16:00" +%s)
now=$(date +%s)
db="data/equities_$(TZ=America/New_York date +%F).duckdb"

wait_s=$(( open_et - now )); [ "$wait_s" -lt 0 ] && wait_s=0
dur=$(( close_et - (now + wait_s) ))
echo "$(date -u +%FT%TZ) plan: wait ${wait_s}s -> record ${dur}s -> $db"
if [ "$dur" -le 0 ]; then echo "market already closed — skip"; exit 0; fi
if [ "${DRY_RUN:-0}" = "1" ]; then echo "DRY_RUN — not recording"; exit 0; fi

# 30 liquid names, quotes-only (Alpaca free IEX caps at 30 channel-subscriptions;
# quotes-only fits 30 symbols vs 15 with trades, and the no-micro strategy uses no
# trade-derived features). 3 index ETFs + mega-cap tech + semis + liquid retail —
# the universe to screen nightly for repeatable single-name reversion edges.
SYMBOLS="SPY QQQ IWM AAPL MSFT NVDA AMZN GOOGL META TSLA AMD AVGO NFLX \
INTC MU MRVL SMCI ARM QCOM PLTR COIN SOFI F BAC T UBER DIS BABA NIO JPM"

# Retention: 30 quotes-only symbols make ~0.3-0.5GB/session, so keep only the
# newest 30 session DBs (~6 weeks, well past the 10-session rolling screen) to
# stop the disk filling. Safe glob; deletes nothing else.
ls -1t data/equities_2*.duckdb 2>/dev/null | tail -n +31 | xargs -r rm -f

[ "$wait_s" -gt 0 ] && sleep "$wait_s"
# Recompute duration after the wait so a slow start can't overrun the close.
dur=$(( close_et - $(date +%s) )); [ "$dur" -le 0 ] && { echo "closed during wait — skip"; exit 0; }
# stocks_live.py = record.py's capture (identical quotes -> identical DuckDB) PLUS the
# tracked model running live on the same websocket, booking a shadow trading book,
# PLUS (since 2026-07-18, Oli's call) REAL PAPER ORDERS on the tracked names:
# NVDA/AAPL, $1000 max/position (whole shares), 2 open max, $25 daily loss cap,
# EOD flatten scoped to its own symbols (the paper account is shared with crypto).
# Paper money only; the point is measuring real fills vs the sim's cost model.
exec .venv/bin/python scripts/stocks_live.py \
  --symbols $SYMBOLS --duration "$dur" --db "$db" \
  --model ev --horizon-s 5 --dead-zone-bps 4 --max-spread-bps 2 \
  --trade --trade-symbols NVDA AAPL --notional 1000 --max-open 2 --daily-loss-cap 25
