# Anatomy of an honest null result

*intraday-ml-signals, 2026-06 → 2026-07. Written 2026-07-16, when the last lever was
exhausted and the kill/continue rule was pre-registered.*

## What was attempted

An online-learning intraday signal engine (River, Alpaca paper): stream quotes, build
microstructure features, predict the next 5–60s move, trade it net of costs. First on
crypto (one venue, then cross-venue lead-lag), then on US equities (3 names, then 30),
with a live shadow book and a nightly out-of-sample screen.

## The result

**No deployable edge. Five levers tested; the same wall stopped all five.**

What *was* found, replicated and real: liquid instruments mean-revert at ~5s horizons, and
a no-microstructure online model captures that direction consistently — d-best +0.03..+0.06
over the best naive baseline on NVDA/AAPL, positive on every one of 6+ clean sessions,
in univariate AND market-hedged residual space. The structure is genuine.

What that structure is worth after costs: a phase-mean of roughly +2bps/trade whose
**sampling fragility exceeds the mean everywhere** (best net/±ph: univariate 0.80, residual
0.75, time-of-day ≤0.57, all < 1). Any single day or sampling phase is a coin flip around a
small positive. Slippage of 1bp erases most of it. That is a tilt, not an edge.

| Lever | Verdict |
|---|---|
| Cross-venue lead-lag (crypto) | Real direction, toll 20–96bp vs 4–11bp moves — hopeless |
| Maker/passive execution | Halves the toll; correct signals don't fill (0–9%) |
| Cheaper venue (equities) | Toll 1–3bp; direction survives, net sits under fragility |
| Spread-gated entry | The "4/4 green" headline was a lucky sampling grid — retracted |
| Longer horizons | Direction decays faster than the move grows; 5s optimal |
| Time-of-day | Direction strongest at the open; net worst there (spreads, fragility) |
| Cross-sectional residual | Cleanest gross found (6/6 sessions +); dies on the same ridge |

## The seven catches

Every one of these was a believed result, later destroyed by the project's own tooling.
The pattern is the actual lesson.

1. **Overlapping direction accuracy (0.6–0.77)** — autocorrelation, not skill. Caught by
   the persistence baseline. *Score on independent windows, against the best naive rule.*
2. **NVDA net-positive, n=2 sessions** — broke on session 3 with the *highest* direction
   accuracy of the run. *Direction ≠ net. Demand replication before belief.*
3. **The "no-micro" ablation leaked** — a trade-derived product interaction survived the
   exclude list; the flagship config was never what it claimed. Caught by an invariance
   test (0 of 394,696 vectors may differ when trades are stripped). *Test the ablation,
   not the intention.*
4. **Replay was non-deterministic** — 63.8% of rows tie on recv_ns; parallel sort broke
   ties differently per run; the same DB scored ±0.3bps differently and the noise wore a
   causal story. *A backtest that can't reproduce itself byte-for-byte proves nothing.*
5. **The windowed cadence inflated the edge ~3×** — `non_overlapping=True` is correct for
   scoring and silently became the *trade simulator's* entry cadence: one entry per window,
   +3.3bps vs the honest per-quote +1.1bps. *State the cadence; simulate the strategy you
   would actually run.*
6. **The lucky grid** — the surviving windowed rule depended on *which half-second* the 5s
   grid started at; the phase spread within one session exceeded the effect. Caught by
   sweeping the phase. *Any grid-sampled result must be phase-swept before it is believed.*
7. **The AAPL "bright spot" (net/±ph 1.55)** — a 2-session run that landed on AAPL's two
   best days; 4 sessions → 0.91, 6 sessions → 0.80, shrinking as n grows. Caught by the
   fragility metric one commit after it was celebrated. *The over-claim you finally catch
   in others you will still commit yourself.*

## The methodology that survived

The durable deliverable. Each rule exists because its absence shipped a wrong number:

- **Deterministic replay** (total order: recv_ns, ts_ns, symbol) — same DB, same answer,
  every run, every machine.
- **Direction scored on non-overlapping windows against the best of
  {persistence, fade, coin}**; the trade sim NEVER subsamples — the two jobs never share
  a code path silently.
- **Every number is a phase-mean ± phase-spread**, and the ratio is the headline. An effect
  smaller than its own sampling fragility is not a result.
- **Ablations are proven by invariance**, not by feature-name lists.
- **Live and backtest share the decision rule as one function** (`simrule`), verified to
  Δnet = 0.0000 over a full real session — the live number and the nightly number cannot
  drift apart unnoticed.
- **A rolling, idempotent, config-versioned nightly screen** with a cumulative net/σ tally:
  the multi-session verdict computes itself; silence is meaningful (and monitored, since the
  one time it failed it failed silently).
- **Pre-registered kill/continue criteria** (RESEARCH.md 2026-07-16): at 15 clean sessions,
  net/σ ≥ 1 and net/±ph ≥ 1 or the hunt is archived. Decided before the data, because every
  failure above was a decision made after.

## Standing infrastructure

Daily 30-symbol capture (deterministic, quarantine-gated), live shadow books in both
cadences, Telegram ops alerts + nightly screen, a validated single-writer DuckDB store at
~1.8M rows/s. All of it reusable for the next hypothesis at near-zero marginal cost.

## The one-sentence version

Six weeks of increasingly careful measurement converted "promising signals everywhere" into
one honest sentence: *short-horizon reversion is real, small, and priced almost exactly at
the cost of trading it* — which is what an efficient market is supposed to look like from
the outside, and now we can prove it from our own tape.
