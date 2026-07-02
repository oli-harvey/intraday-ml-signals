# Build Plan

Incremental, phase-based build. Each phase is independently shippable and testable.
Don't start a phase until the previous phase's **Done when** criteria are met.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Phase 0 — Scaffolding & tooling  `[~]`

- [x] Repo + directory skeleton mirroring the pipeline stages
- [x] `pyproject.toml` with hot-path deps (no pandas), dev extras (pytest, ruff, mypy)
- [x] `.gitignore`, `.env.example`
- [ ] Create Alpaca **paper** account; put keys in `.env` (never commit)
- [ ] `pip install -e ".[dev]"`; confirm `river`, `alpaca-py`, `websockets`, `numpy`, `duckdb` import
- [ ] Pre-commit / CI check running `ruff` + `pytest`

**Done when:** clean checkout installs and `pytest` runs (even with 0 tests).

---

## Phase 1 — Data ingestion + ring-buffer storage  `[ ]`

Goal: a running asyncio task that connects to Alpaca's WebSocket and pushes normalized
tick events onto an `asyncio.Queue`, with a fixed-size in-memory ring buffer per symbol.

- [ ] `data/base.py` — `DataSource` ABC: `subscribe(symbols)`, async iterator of `Tick`
- [ ] `data/schema.py` — frozen `Tick`/`Quote`/`Bar` dataclasses (slots), monotonic timestamps
- [ ] `data/alpaca.py` — Alpaca WS adapter (IEX free feed for equities; crypto stream 24/7)
- [ ] `data/ringbuffer.py` — preallocated numpy circular arrays + `deque(maxlen=)`; O(1) push/read
- [ ] REST backfill helper (historical bars/trades) to warm buffers on startup
- [ ] Reconnect/backoff + heartbeat handling on the WS
- [ ] Validate live against Alpaca sandbox/crypto stream (print tick rate, gaps)

**Done when:** live crypto stream feeds the queue for ≥10 min with no unbounded memory
growth, reconnects cleanly, and ring buffers hold the last N ticks correctly.

**Key decisions**
- Tick granularity: trades vs quotes vs 1s bars for v1 → *start with trades + 1s bars.*
- Buffer depth N per feature horizon (start N=512).

---

## Phase 2 — Streaming feature engine  `[ ]`

Goal: O(1)-per-tick features, each verified against a naive full-recompute reference.

- [ ] `features/online_stats.py`
  - [ ] `Welford` running mean/variance (rolling & expanding variants)
  - [ ] `EMA` O(1) update; `RunningSMA` via deque + running sum (add new / subtract expiring)
  - [ ] `RunningZScore` online normalization (running mean/std, not fit-once)
- [ ] `features/engine.py` — orchestrates per-symbol feature vector from ring buffers:
  - [ ] Lags: last N prices/returns
  - [ ] Momentum: % change over k lags
  - [ ] Volatility: Welford std over window
  - [ ] MAs: EMA(s) + SMA(s)
  - [ ] Microstructure (if available): bid-ask spread, trade size, order imbalance
  - [ ] Online z-normalization of all outputs
- [ ] `tests/test_online_stats.py` — assert incremental == naive numpy recompute (atol)
- [ ] `tests/test_engine.py` — feature-vector snapshot vs reference on a fixed tick sequence

**Done when:** all incremental calcs match naive recompute within tolerance, and one
feature update is O(1) (microbenchmark flat as window grows).

**Key decisions**
- Which normalization warmup guard (min samples before emitting z-scores).
- NaN/first-tick handling policy.

---

## Phase 3 — Online model + no-lookahead labelling  `[ ]`

Goal: River model that predicts short-horizon forward return; learns after the outcome
is observed. Offline-evaluated by replaying stored ticks.

- [ ] `model/labels.py` — prediction queue: emit prediction at `t`, match to realized
      forward return at `t+k`, then `learn_one()`. **Strictly no lookahead.**
- [ ] `model/online.py` — wrap `river.linear_model` (start) and
      `river.tree.HoeffdingTreeRegressor` (compare); `predict_one` / `learn_one`
- [ ] Target: forward return `t → t+k`. Regression first; classification (up/down/flat
      with dead-zone) as an alternative.
- [ ] Rolling metrics (River `metrics`): MAE, R², directional accuracy — walk-forward only
- [ ] `scripts/replay.py` — feed stored ticks through the **exact same** async pipeline
      to catch train/serve skew

**Done when:** model trains online over a historical replay with no lookahead, and
rolling directional accuracy is logged. (Beating a naive baseline is a research goal,
not a gate.)

**Key decisions**
- Horizon k (ticks vs seconds); start k = a few seconds.
- Regression vs classification target for v1.
- Feature/target scaling handled by River pipeline vs our online z-score.

---

## Phase 4 — Signal / risk layer + paper execution  `[ ]`

Goal: turn predictions into risk-managed paper orders.

- [ ] `signal/policy.py` — predicted return → signal only if it clears a threshold that
      covers estimated transaction cost + spread (avoid overtrading on noise)
- [ ] `signal/risk.py` — position sizing (fixed-fractional or vol-scaled, capped);
      hard controls: max position, daily-loss circuit breaker, max open positions
- [ ] `execution/alpaca_exec.py` — route orders via Alpaca **paper** API; track fills/positions
- [ ] Kill-switch + graceful shutdown flattening positions
- [ ] Wire full pipeline in `pipeline.py` (ingest → features → model → signal → exec → log)

**Done when:** end-to-end paper loop places, fills, and closes orders on the crypto
paper stream, and all risk limits are enforced (unit-tested).

**Key decisions**
- Cost model assumptions (spread + fees) for the threshold.
- Sizing scheme + caps.

---

## Phase 5 — Logging / monitoring dashboard  `[ ]`

Offline only — pandas/matplotlib allowed here.

- [ ] `storage/coldstore.py` — async append of raw ticks + predictions + orders to
      DuckDB/SQLite (never read from disk in the live loop)
- [ ] Reporting notebook/script: PnL, hit rate, latency histograms, feature drift
- [ ] Latency instrumentation surfaced against the budget (see ARCHITECTURE.md)

**Done when:** a run produces a durable log queryable offline and a basic report.

---

## Phase 6 — Extended paper validation  `[ ]`

- [ ] Multi-session paper run; monitor stability, memory, reconnects
- [ ] Walk-forward evaluation summary over the run
- [ ] Decision checklist before *any* real-capital consideration (small size, if ever)

**Done when:** the pipeline runs unattended across multiple sessions without leaks,
crashes, or risk-limit breaches, with performance tracked over a meaningful period.

---

## Cross-cutting invariants (check every PR)

- No pandas import anywhere under `src/signals/` hot path.
- No feature/model op scales with window/history size.
- No label uses information from the future.
- Secrets only in `.env` / env vars, never committed.
- Replay path and live path share the *same* code.

## Open questions

- Primary asset for v1 validation: crypto (24/7, easiest) vs a liquid equity.
- Latency budget target (draft: <50 ms tick→decision) — confirm on real hardware.
- Model family beyond linear/Hoeffding to try later (e.g. `river` ensembles, FTRL).
