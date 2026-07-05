# Research: state of the signal, and how this could really work

*2026-07-04. Updated as experiments land. Companion to PLAN.md — this is the
research layer; PLAN.md is the build.*

## Where we actually are (no varnish)

**Evaluation infrastructure now exists that is hard to fool:**
- Walk-forward only; live/replay share the same code path (no train/serve skew).
- Zero-MAE baseline (magnitude skill) and **sign-persistence baseline**
  (direction skill) computed for every run.
- **Non-overlapping scoring**: successive quote-rate predictions share ~99% of
  their outcome window, so overlapping directional accuracy mostly measures
  autocorrelation. `scripts/experiment.py --non-overlapping` scores only
  independent windows.

**Findings from ~1h of recorded BTC/USD (small! 3 sessions):**

| View | Model dir_acc | Persistence | Verdict |
| --- | --- | --- | --- |
| Overlapping (flattering) | 0.61–0.68 | **0.77–0.91** | both are overlap artifacts; persistence "wins" because it's nearly self-fulfilling |
| Non-overlapping (honest) | 0.43–0.60 | 0.38–0.57 (~coin flip) | model slightly ahead in 9/12 cells but n=15–138 → statistically nothing |

- MAE does not beat predict-zero in most cells (model overpredicts magnitude).
- Decision latency p50 ~11µs — compute is a non-issue; **data and costs are
  the binding constraints.**
- Measured BTC round-trip cost on Alpaca paper: **~10.5 bps** vs typical 10s
  mid moves of ~3 bps. At taker fees, short-horizon crypto on this venue is
  structurally unprofitable even with a good model.

## The five ideas most likely to make this actually work

Ranked by expected impact ÷ effort:

### 1. Scale the data (running now)
Nothing can be learned from 1h. The 48h paper soak is recording BTC+ETH
continuously (~300k quotes expected → ~17k independent 10s windows). A week of
recording across 10+ symbols → millions of windows. River is built for exactly
this — the model never needs the data in memory. **Every other idea below is
untestable until this exists.**

### 2. Pivot to equities (venue economics beat model quality)
The 10.5 bps crypto toll is the dominant term — a model with real skill would
still lose money through it. Alpaca US equities are **zero-commission** with
spreads on liquid names (SPY, QQQ, AAPL) of ~1–3 bps. Same code (`--market
stocks`, protocol already validated via the test stream). The cost hurdle drops
~5–10×, which is worth more than any feature. Constraint: market hours only;
start Monday.

### 3. Change the objective: classify tradeable moves, not regress tiny returns
Regression on ~1e-4 returns optimizes MAE on noise. The economically relevant
question is: **P(|forward move| > cost, and which direction)?** Three-class
(up-through-cost / dead-zone / down-through-cost) with `river`'s classifiers
aligns the loss with the trade decision. A prediction of "dead-zone" costs
nothing; precision on the tails is all that matters. This alone may flip the
signal layer from never-firing to selectively-firing.

### 4. Meta-labeling (the López de Prado trick, online)
Keep the primary direction model. Add a second online model that predicts
*whether the primary is currently right*, from regime features (vol, spread,
quote intensity, time-of-day). Trade only when the meta-model is confident.
This converts a weak 52% signal into fewer, higher-precision trades — which is
the only way a weak signal survives costs. Fits our streaming design perfectly:
the meta-label arrives at the same time as the primary label.

### 5. Cross-asset lead-lag features
Documented effect at seconds horizons: BTC leads ETH; SPY/QQQ lead single names
and sector ETFs. Our multi-symbol pipeline already ingests in one stream — a
"leader return over last k seconds" feature for the laggard symbol is ~20 lines
in the feature engine and is one of the few short-horizon alphas that persists
in the literature. (Needs the multi-symbol data from the soak.)

## Second-tier ideas (worth trying after the above)

- **Maker-side execution**: rest limit orders instead of crossing the spread —
  flips cost from −spread to ~0 (fill uncertainty becomes the cost). Requires
  order-management logic (timeouts, repricing) — meaningful build.
- **Vol-normalized targets**: predict return/σ so the target is stationary
  across regimes; de-normalize at the signal layer.
- **Horizon ensemble**: predict 10/30/60s jointly; trade only on sign agreement.
- **Feature interactions for the tree model**: HoeffdingTree can't see products;
  feed micro_bps × uptick, imbalance × vol explicitly.
- **Time-of-day / session features** (equities): open/close auctions dominate
  intraday regime shifts.

## Evaluation hygiene (already enforced, keep it that way)

1. Persistence + zero baselines on every run — a model that can't beat both is
   noise. 2. Non-overlapping windows for any claim. 3. No parameter chosen on
   the data that scores it (when data volume allows: tune on session N, score
   on N+1). 4. The only number that ultimately counts: **net paper PnL after
   real fills over weeks**, which the pipeline measures end-to-end.

## Current queue

- [x] Persistence baseline + non-overlapping scoring (`evaluation.py`)
- [x] Microstructure features: microprice offset, uptick EMA, quote-intensity
- [~] 48h BTC+ETH recording (in flight, ends 2026-07-06)
- [ ] Re-run grid on soak data (~17k independent windows) — first adequately
      powered test of the new features
- [ ] Equities session Monday: record SPY/QQQ/AAPL, replay-grid on it
- [ ] Classification-with-dead-zone model variant
- [ ] Meta-labeling gate
- [ ] Cross-asset lead-lag feature

## Findings log

### 2026-07-05 — first powered evaluation (22.3h soak DB, 3,965 independent windows, BTC/USD, 10s)

The killed 48h soak still delivered 54,604 BTC quotes across a full day/night cycle.
Non-overlapping scoring, fee 0.2bps/side sim:

| model | interactions | dir | persistence | d−p | MAE edge% | trades | net bps |
|---|---|---|---|---|---|---|---|
| hoeffding | on | 0.542 | 0.421 | **+0.121** | −8.7 | 15 | −137.7 |
| hoeffding | off | 0.544 | 0.421 | +0.123 | −14.9 | 14 | −134.4 |
| classifier | on | 0.513 | 0.421 | +0.092 | −2.9 | 0 | 0.0 |
| classifier | off | 0.506 | 0.421 | +0.085 | −2.8 | 0 | 0.0 |

**What it says:**
1. **The model now clearly beats sign-persistence on independent windows**
   (+9 to +12pts) — first real evidence the features carry short-horizon information
   at scale (the 30-min session showed nothing: 0.496 vs 0.504).
2. **BUT: 10s BTC mids are mean-reverting.** Persistence at 0.421 means the
   *anti*-persistence rule ("fade the last window's move") scores 0.579 — which
   still beats the model's 0.542. The dominant 10s structure is reversion, and the
   model captures only part of it. Obvious next probes: an explicit fade baseline
   in the eval table; horizon sweep (reversion at 10s may become momentum at 60s+);
   a `ret_1`-sign-flipped feature is already representable, so more data may close it.
3. **Interaction features: neutral so far.** Direction unchanged (±0.002);
   hoeffding's MAE calibration improved (−8.7 vs −14.9 edge). Kept (cheap, and
   product-confirmation terms should matter more for tree splits with more data),
   but no lift claimed.
4. Economics unchanged: sims negative at both cost scenarios; classifier still
   correctly abstains. No tradeable edge demonstrated.
