# Build Plan

Incremental, phase-based build. Each phase is independently shippable and testable.
Don't start a phase until the previous phase's **Done when** criteria are met.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Phase 0 — Scaffolding & tooling  `[~]`

- [x] Repo + directory skeleton mirroring the pipeline stages
- [x] `pyproject.toml` with hot-path deps (no pandas), dev extras (pytest, ruff, mypy)
- [x] `.gitignore`, `.env.example`
- [x] Create Alpaca **paper** account; put keys in `.env` (never commit) — verified: account ACTIVE
- [x] `uv venv` + `uv pip install -e ".[dev]"`; `river`, `alpaca-py`, `websockets`, `numpy`, `duckdb` import OK
      (env management is **uv only** — no global installs)
- [x] Guard test enforcing "no pandas import under `src/signals/`" (pandas arrives transitively via alpaca-py)
- [ ] Pre-commit / CI check running `ruff` + `pytest`

**Done when:** clean checkout installs and `pytest` runs (even with 0 tests).

---

## Phase 1 — Data ingestion + ring-buffer storage  `[x]`

Goal: a running asyncio task that connects to Alpaca's WebSocket and pushes normalized
tick events onto an `asyncio.Queue`, with a fixed-size in-memory ring buffer per symbol.

- [x] `data/base.py` — `DataSource` ABC; `stream()` owns connection/reconnect lifecycle
- [x] `data/schema.py` — frozen `Tick`/`Quote`/`Bar` (slots) + `recv_ns` for latency instrumentation
- [x] `data/alpaca.py` — WS adapter driving `websockets` directly; certifi CA bundle for wss
- [x] `data/ringbuffer.py` — preallocated numpy circular arrays (int64 for epoch-ns!); O(1) push
- [x] REST backfill helper (minute bars via alpaca-py in a thread; cold path)
- [x] Reconnect/backoff (exponential + jitter, auth failures fatal) + websockets ping/pong
      heartbeat — covered by tests against a local mini-Alpaca WS server
- [x] Validate live against Alpaca crypto stream — 90s smoke + 10-min soak both PASS

**Done when:** live crypto stream feeds the queue for ≥10 min with no unbounded memory
growth, reconnects cleanly, and ring buffers hold the last N ticks correctly. ✅
*(10-min soak 2026-07-02: zero reconnects needed, RSS flat 42→27MB, queue hwm 5/10000,
buffer integrity + ts monotonicity verified for both symbols. Reconnect behaviour is
covered by the mini-server tests and was exercised live during the SSL failure.)*

**Key decisions**
- Tick granularity: trades vs quotes vs 1s bars for v1 → *start with trades + 1s bars.*
- Buffer depth N per feature horizon (start N=512).

**Findings (2026-07-02 live validation, 10-min soak)**
- Alpaca's crypto **trade** feed is thin (own-venue prints only): BTC 12 trades vs
  3512 quotes in 10 min (max inter-trade gap 143s; ETH 366s!). **Quotes are the dense
  stream** → Phase 2 features and the Phase 3 target should be built on quote **mids**;
  trades become supplementary (size/side) features. SymbolBuffers needs a mid-price
  ring updated from quotes.
- Feed→local (exchange ts → our recv) latency: p50 ~32-38 ms, p99 108-307 ms. This is
  upstream network/feed delay, *outside* our <50 ms tick→decision budget — but it bounds
  how short a horizon can plausibly be traded: k must be ≫ feed delay.

---

## Phase 2 — Streaming feature engine  `[x]`

Goal: O(1)-per-tick features, each verified against a naive full-recompute reference.

- [x] `features/online_stats.py`
  - [x] `Welford` running mean/variance (expanding + rolling via West's O(1) removal)
  - [x] `EMA` O(1) update; `RunningSMA` via deque + running sum (add new / subtract expiring)
  - [x] `RunningZScore` online normalization (running mean/std, not fit-once; warmup-gated)
- [x] `features/engine.py` — per-symbol vector, quote-mid keyed (per Phase 1 finding):
  - [x] Lag returns over k quote-mid lags (ring buffer)
  - [x] Momentum: % change over k lags (= the lag-return features)
  - [x] Volatility: rolling Welford std of 1-lag returns
  - [x] MAs: EMA fast/slow spread normalized by mid
  - [x] Microstructure: spread bps, book imbalance, signed trade-flow EMA
  - [x] Online z-normalization of all outputs
- [x] `tests/test_online_stats.py` — incremental == naive numpy recompute (incl. O(1) microbench)
- [x] `tests/test_engine.py` — hand-computed reference values, warmup gating, determinism

**Done when:** all incremental calcs match naive recompute within tolerance, and one
feature update is O(1) (microbenchmark flat as window grows). ✅

**Decisions taken**
- Warmup: no emission before 64 valid quotes; per-feature z-scores emit 0.0 for their
  first 30 samples (degenerate std also → 0.0). No NaNs by construction.
- Degenerate quotes (crossed / zero mid) skipped entirely.

---

## Phase 3 — Online model + no-lookahead labelling  `[ ]`

Goal: River model that predicts short-horizon forward return; learns after the outcome
is observed. Offline-evaluated by replaying stored ticks.

- [x] `model/labels.py` — LabelQueue: time-based horizon (ns); label = first observed
      price after `t+horizon` (exactly what live sees). **Strictly no lookahead**;
      out-of-order additions rejected.
- [x] `model/online.py` — River `LinearRegression` (default) and
      `HoeffdingTreeRegressor` behind one wrapper; `predict_one` / `learn_one`
- [x] Target: forward simple return over `horizon_s` (default 10s). Regression first;
      classification with dead-zone deferred as a comparison.
- [x] Walk-forward metrics: MAE vs always-zero baseline + rolling directional accuracy
- [x] `core.SymbolPipeline` — learn-then-predict per quote; shared verbatim by replay
      and live (the train/serve-skew guard)
- [x] `scripts/replay.py` — replay stored events through the same core; per-quartile
      walk-forward metrics
- [x] Replay evaluation on a recorded live session (30 min BTC/USD, 4049 quotes)

**Done when:** model trains online over a historical replay with no lookahead, and
rolling directional accuracy is logged. (Beating a naive baseline is a research goal,
not a gate.) ✅

**Replay results (2026-07-02, 30-min BTC/USD session, 10s horizon, 3962 predictions)**
- Latency: p50 ~9µs, p99 <0.5ms per quote (feature+inference) — far under budget.
- Found & fixed: unclipped online z-scores can spike when a feature's running std is
  momentarily tiny → SGD positive-feedback divergence (linear model MAE exploded to
  2.7 vs targets ~3e-4). Z-scores now clipped to ±8 (regression-tested).
- Post-fix: linear dir_acc 0.60 / MAE edge −21.5%; Hoeffding dir_acc 0.63 / edge −4.1%.
- **Honest read:** direction better than chance (likely partly quote-mid persistence,
  not tradeable edge), magnitude overestimated, MAE does not beat the zero baseline.
  Round-trip cost ~10.5 bps vs typical 10s moves ~3 bps → no economic edge shown.
  30 min is a tiny sample; treat as plumbing validation only.

**Decisions taken**
- Horizon: time-based (default 10s), not event-count — quotes arrive irregularly and
  feed latency p99 ~300ms makes sub-second horizons untradeable.
- Regression on forward return for v1.
- Scaling: our online z-scores (engine) — River pipeline scalers not used, so replay
  and live normalize identically.

---

## Phase 4 — Signal / risk layer + paper execution  `[ ]`

Goal: turn predictions into risk-managed paper orders.

- [x] `signal/policy.py` — signal only when |predicted| > fee + half-spread + dead-zone
- [x] `signal/risk.py` — fixed-fractional sizing (optional vol scaling), position cap,
      min-notional floor, max open positions, daily-loss circuit breaker
- [x] `signal/positions.py` — long-only book (Alpaca crypto is non-marginable): LONG
      enters if flat, SHORT exits an open long; realized PnL feeds the breaker
- [x] `execution/alpaca_exec.py` — paper-only guard at construction; market orders in
      threads (off critical path); fill polling; flatten_all kill-switch
- [x] Kill-switch + graceful shutdown flattening positions (`flatten_on_exit`)
- [x] Wire full pipeline in `pipeline.py` (ingest → core → policy → book → exec → tap → log)
- [x] Live order lifecycle verified: buy → fill → sell → flat on the paper endpoint

**Done when:** end-to-end paper loop places, fills, and closes orders on the crypto
paper stream, and all risk limits are enforced (unit-tested). ✅ (limits unit-tested;
end-to-end order flow verified via `scripts/paper_order_check.py`; full-loop live run
is the Phase 6 gate)

**Decisions taken**
- Cost model: `cost_bps=5` + half observed spread + `dead_zone_bps=2`. Measured real
  round-trip cost on BTC/USD paper: **~10.5 bps** (spread + slippage) — the default
  threshold (~12 bps at 10 bps spread) is calibrated to that reality.
- Sizing: 1% of equity per entry, capped at $1000 notional, $10 min; vol scaling
  available but off by default.
- Gotcha handled: Alpaca charges crypto buy fees **in the asset**, so position <
  filled qty; exits clamp to the actual held quantity.

---

## Phase 5 — Logging / monitoring dashboard  `[ ]`

Offline only — pandas/matplotlib allowed here.

- [x] `storage/coldstore.py` — batched async DuckDB appends (built early: Phase 3's
      replay depends on it); tail-flush on cancellation; never read in the live loop
- [x] `scripts/report.py`: event counts, walk-forward prediction quality by quartile,
      PnL/hit-rate per round trip, latency histogram, price+orders chart → PNG
- [x] Latency instrumentation: per-quote `proc_us` logged with every prediction and
      surfaced in the status loop + report (budget: <15ms features+model)

**Done when:** a run produces a durable log queryable offline and a basic report.
(✅ pending exercise on the Phase 6 run output)

---

## Phase 6 — Extended paper validation  `[~]`

- [x] Bounded live paper session (2026-07-03): full pipeline (ingest → features →
      model → policy → risk → executor → cold store) against the live BTC/USD stream
- [x] Walk-forward evaluation summary over the run (scripts/report.py)
- [x] Order path fired in-pipeline (10-min demo, near-zero threshold, $100 cap):
      signal → book → buy 0.001615 @ 61933.74 → exit clamped to 0.001611 actually held
      (in-asset fee clamp verified live) → sell @ 61913.32 → realized −$0.03 booked
      to the risk manager. 0 errors.
- [ ] **Multi-session / multi-day paper run — outstanding operational task.** Launch:
      `caffeinate -is .venv/bin/python -m signals.pipeline --symbols BTC/USD --db data/paper_$(date +%F).duckdb`
      (caffeinate matters: macOS sleep freezes the monotonic clock and drops the WS)
- [ ] Decision checklist before *any* real-capital consideration (small size, if ever)

**Done when:** the pipeline runs unattended across multiple sessions without leaks,
crashes, or risk-limit breaches, with performance tracked over a meaningful period.
*(Not yet — one bounded session so far; the extended run is wall-clock work, not code.)*

**Bounded session results (2026-07-03, BTC/USD, 10s horizon, Hoeffding)**
- Machine slept mid-run (wall 42 min, awake ~13 min, 888 events) — accidental but
  valuable resilience test: two real WS drops on sleep/wake, both reconnected in ~1.2s
  with the model retaining state. Zero order errors, zero tap drops, breaker never hit.
- 807 predictions, 791 resolved. Decision latency p50 83µs, p99 2.6ms (budget <15ms).
- Quality by quartile: dir_acc 0.75/0.80/0.49/0.63; MAE edge vs zero −14/+29/−38/+12%.
  Consistent with replay: unstable, sample far too small, no edge claim.
- Zero orders: even the lowered ~3bps threshold exceeded every prediction — the
  cost gate is doing exactly its job against a model with no demonstrated edge.

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
