# intraday-ml-signals

A lightweight, low-latency framework that ingests real-time market data, engineers
streaming features (lags, moving averages, online volatility), feeds them into an
**online-learning** model (River), and generates short-horizon trade signals routed
to **Alpaca paper trading**.

This is a speculative, learning-based **signal engine** — not a technical-analysis
rule system, and not a general-purpose batch/vectorized backtesting suite.

> ⚠️ **Speculative software, no guarantee of profitability.** Markets are adversarial
> and noisy; short-horizon prediction is genuinely hard. Paper trade extensively and
> size any eventual real capital small relative to what you can afford to lose.
> Nothing here is financial advice.

## Design principles (hard constraints)

- **No pandas in the hot path** (ingest → features → inference → decision). Pandas is
  allowed *only* for offline analysis/reporting.
- **No existing trading frameworks** (freqtrade, backtrader, zipline, …) — they are
  built around batch/vectorized pandas workflows, too slow for tick-level online updates.
- **O(1) per tick.** Every feature and the model inference update incrementally — never
  recompute over the window/history.
- **No lookahead.** The label for tick `t` is only available `k` steps later; predictions
  are queued and matched to outcomes as they arrive.

## Architecture

```
[WebSocket ingest] --asyncio--> [Feature Engine (ring buffers, numpy)]
        --> [Online Model (River)] --> [Signal / Risk Layer] --> [Executor (Alpaca paper)]
        --> [Async logger --> DuckDB/SQLite for offline analysis]
```

Stages are decoupled via `asyncio.Queue` so each is independently testable. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Status

🚧 **Planning / scaffolding.** See [docs/PLAN.md](docs/PLAN.md) for the phased build.
Modules currently contain interface/skeleton stubs only.

## Quickstart (once implemented)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # add your Alpaca paper keys
python -m signals.pipeline --symbols BTC/USD --paper   # crypto works 24/7 for testing
```

## Layout

| Path | Responsibility |
| --- | --- |
| `src/signals/data/` | Data-source interface + Alpaca/Finnhub adapters, ring buffers |
| `src/signals/features/` | Incremental feature engine + online stats (Welford, EMA, z-score) |
| `src/signals/model/` | River online model wrapper + no-lookahead label queue |
| `src/signals/signal/` | Threshold/cost-aware signal generation + risk controls |
| `src/signals/execution/` | Alpaca paper-trading executor |
| `src/signals/storage/` | Async cold-path logger (DuckDB/SQLite) |
| `src/signals/pipeline.py` | asyncio orchestration wiring the queues together |
| `scripts/replay.py` | Walk-forward replay of stored ticks through the live pipeline |
| `tests/` | Correctness tests (incremental vs naive recompute) |
