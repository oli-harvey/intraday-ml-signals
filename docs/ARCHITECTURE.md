# Architecture

## Data flow

```
                         asyncio.Queue        asyncio.Queue         asyncio.Queue
[WS ingest] ──ticks──▶ [ Feature Engine ] ──feat──▶ [ Online Model ] ──pred──▶ [ Signal/Risk ] ──order──▶ [ Executor ]
   (data/)              (features/)                   (model/)                   (signal/)                (execution/)
      │                      │                            │                          │                        │
      └──────────────────────┴────────────tap────────────┴──────────────────────────┴────────────────────────┘
                                                     │
                                            [ Async cold logger ]  ──▶  DuckDB / SQLite  (offline only)
                                                 (storage/)
```

- Every stage is an `async` task consuming from one queue and producing to the next.
- Stages are decoupled → each is unit-testable in isolation and swappable.
- A non-blocking **tap** copies events to the cold logger; the live loop never reads disk.

## Hot path vs cold path

| | Hot path (live decision loop) | Cold path (offline) |
| --- | --- | --- |
| Data structures | numpy circular arrays, `deque(maxlen=)` | DuckDB/SQLite tables |
| Compute | O(1) incremental updates | batch / vectorized ok |
| pandas | **forbidden** | allowed |
| Purpose | ingest → features → inference → decision | research, reporting, replay source |

## No-lookahead labelling

A prediction made at tick `t` targets the forward return over `t → t+k`. That label
does not exist until `t+k`. The model therefore:

1. `predict_one(features_t)` → store `(t, features_t, prediction)` in a pending queue.
2. When the tick/price at `t+k` arrives, compute the realized forward return.
3. Pop the matching pending entry and `learn_one(features_t, realized_return)`.

This mirrors live trading exactly and is the single most important guard against
train/serve skew and inflated backtest results.

## Latency budget (draft)

Target end-to-end **tick → decision < 50 ms** (revisit on real hardware). Per-stage
soft budgets to profile against:

| Stage | Budget |
| --- | --- |
| WS receive → normalized tick | < 5 ms |
| Feature update (O(1)) | < 5 ms |
| Model `predict_one` | < 10 ms |
| Signal + risk checks | < 5 ms |
| Order submit (async, off critical path) | not counted in tick→decision |

Instrument with monotonic clocks per stage; log to the cold store; visualize as a
latency histogram in Phase 5.

## Swappable data sources

`data/base.py` defines a `DataSource` ABC. Alpaca is primary; Finnhub and Polygon are
alternate adapters implementing the same interface, so nothing downstream changes when
the feed is swapped.

## Concurrency notes

- One producer task per WS connection; backpressure via bounded queues.
- Feature/model stages are single-consumer to keep per-symbol state serial and simple.
- Reconnect/backoff isolated in the data adapter; downstream stages see a clean stream.
