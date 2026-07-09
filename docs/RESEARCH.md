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

### 2026-07-05 — research batch: horizon sweep, ablation, meta-labeling, lead-lag

All on the 22.3h soak DB, non-overlapping windows, fee 0.2bps/side sim.
Full tables: `scripts/research_batch.py` output (repro: just run it).

**1. Horizon sweep (BTC).** Mean reversion is strongest at 5s (fade 0.592) and
decays monotonically with horizon (0.579 @ 10s → 0.533 @ 120s) — no momentum
flip within 2 minutes. The model *tracks* the reversion structure (dir rises as
fade rises) and nearly closes the gap at 5s (0.579 vs 0.592) but never exceeds
it. Hoeffding's trading losses grow with horizon (magnitude overestimation
compounds); classifier abstains almost everywhere.

**2. Feature ablation (BTC, 10s) — the surprise.** Removing the microstructure
group (micro/imbalance/flow/spread + their interactions) IMPROVED direction
0.542 → 0.574 and cut sim losses -137.7 → -25.6bps. Removing momentum collapsed
direction to 0.487. At this data size the momentum/lag family carries
essentially all the signal (it encodes the reversion), while microstructure
features actively distract the tree — despite the monotonic microprice-bucket
relation seen on the 30-min session. Hypothesis: micro carries redundant/noisier
copies of what lags already encode; revisit with more data + equities before
deleting.

**3. Meta-labeling (BTC, 10s).** Direction unchanged (0.538 vs 0.542) but MAE
edge improved (-2.8% vs -8.7%) and losing trades were cut (10 trades/-105bps vs
15/-137.7). The gate does its job: calibration, not direction. Keep as the
execution-layer filter.

**4. Cross-asset lead-lag.** ETH+BTC-leader improved ETH direction 0.518 →
0.544 (d-best -0.030 → -0.004). Lead-lag is real. Note ETH is
persistence-dominated (0.548) unlike BTC — likely stale/sparse quotes
(5.5k vs 54.6k) making 10s windows trend-y. More ETH data needed.

**5. Synthesis run: `meta` @ 5s, microstructure excluded** — dir 0.576,
**MAE edge +0.6% (first positive)**, 3 trades/-29bps. Current best config.

**Standing conclusions:** the model family reliably learns the reversion
structure but hasn't exceeded the fade rule on BTC; the leanest feature set is
currently the best; every config still loses money after costs. Next levers:
equities session (different microstructure, tighter costs), fade-aware feature
(explicit last-window-return input at the scoring horizon), more soak data.

### 2026-07-07 — replication (46h crypto) + first equities session

**Crypto replication (fresh 46h soak, 2.3× more windows):** every 07-05 finding
held — reversion strongest at 5s decaying with horizon (fade 0.575→0.529);
model tracks fade at 5s to within 0.003; no-microstructure ablation again best
(0.566 vs 0.528 full, d-best +0.002 — first non-negative on BTC); no-momentum
again collapses (0.484); meta gate again halves losses; BTC leader again lifts
ETH (+2.3pts). These are now *stable, replicated* conclusions on crypto.

**Equities (6.5h SPY/AAPL/NVDA session, 3.89M events, fee 0.2bps/side sims):**
1. **The fade signature is universal but graded by liquidity:** 5-10s fade =
   SPY 0.548 < NVDA 0.552 < BTC 0.575 < AAPL 0.586. Everything we trade
   mean-reverts at short horizons; the most liquid instrument (SPY) is closest
   to efficient.
2. **Costs transformed as predicted:** SPY sim losses are single-digit bps
   (−3.6 to −9.9) vs crypto's hundreds/thousands. Classifier @ 5s exactly
   matches the fade baseline (d-best −0.001, MAE edge 0.0%) while abstaining.
   We are no longer fighting the toll booth — only efficiency.
3. **SPY ablation is flat** — the BTC drop-microstructure result does NOT
   transfer (0.510 vs 0.509); on SPY every feature group is marginal.
4. **SPY→AAPL/NVDA lead-lag: negative result.** Leader features didn't help
   (AAPL 0.579→0.571; NVDA 0.532→0.524) — opposite of BTC→ETH. Likely because
   single-name quotes are dense (~65/s), so index information is already
   embedded intra-horizon; the BTC→ETH effect exists because ETH quotes are
   sparse. Lead-lag helps sparse followers, not dense ones.
5. **Overtrading trap on single names:** hoeffding fired 417–489 trades on
   AAPL/NVDA (higher vol → tiny threshold cleared often) and burned −300 to
   −750bps, mostly spread. Even "free" trading isn't free 400 times over.

**Sharpest open question:** AAPL's fade baseline (0.586 at 10s) is strong and
its costs are ~1.5bps round trip. Does the *fade rule itself* clear costs as a
strategy? → add a fade-rule row to the trade sim (it's a strategy now, not
just a baseline). If yes, the model's job reframes as "learn when fade fails."

**Ops:** LabelQueue timestamp clamp (equities bursts regress exchange ts —
would have crashed live); recorder queue 10k→50k (VPS hit the cap; Mac peaked
at 820). Server now runs the 24/7 paper service + weekday equities cron.

### 2026-07-08 — cross-venue leader (Coinbase→Alpaca BTC): FIRST BASELINE BEAT

3h dual-venue session (one process/clock: 12.3k Alpaca + 77.4k Coinbase BTC
quotes), non-overlapping windows, `leaders={"BTC/USD": "CB:BTC/USD"}` adding
leader_r1 / leader_uptick / **leader_gap_bps** (venue price gap):

| config | dir | fade (best naive) | d-best |
|---|---|---|---|
| hoeffding 5s alone | 0.560 | 0.571 | −0.011 |
| **hoeffding 5s +CB leader** | **0.648** | 0.571 | **+0.077** |
| hoeffding 10s alone | 0.495 | 0.570 | −0.075 |
| **hoeffding 10s +CB leader** | **0.587** | 0.570 | **+0.017** |

**First configuration in the project to beat the best naive baseline** — and
not marginally: +7.7pts at 5s (crude z ≈ 4.8 at n=990). Mechanism exactly as
theorized: price discovery happens on Coinbase (~8× denser feed); Alpaca's
venue lags by enough at 5–10s that the gap predicts the catch-up move.
No-lookahead is structural: the leader state visible at prediction time is
whatever the shared clock had already delivered.

Caveats / next:
1. Single 3h overnight session — REPLICATE with a longer daytime session.
2. Classifier barely moved (0.522→0.528): the tree classifier needs more data
   or the band setup dilutes the gap signal; investigate.
3. Economics still unsolved: direction edge ≠ profit at Alpaca's ~11bps spread
   (sims still ≈ −105bps on 4 trades). The gap signal must either select rare
   large moves (magnitude selectivity) or be traded where costs are lower.
4. Productionize: wire CoinbaseSource into the live Pipeline as an auxiliary
   (non-traded) source so the server gets permanent dual capture + live leader
   features; then the paper record measures this config continuously.

### 2026-07-08 — fade rule as a strategy: NEGATIVE (decisively)

The open question from 07-07: AAPL fades at 0.586 with ~1.5bps costs — does the
fade rule itself make money? **No. Everywhere, at every gate.**

Simulated (10s horizon, non-overlapping, cost-charged, one position at a time;
gates = trade only when |last move| > N bps):

| instrument | best config tried | bps/trade | hit rate |
|---|---|---|---|
| SPY (2 sessions) | gate 5bps, 07-07 | −3.0 | 0.25 |
| AAPL (2 sessions) | gate 5bps, 07-07 | −0.9 | 0.18 |
| NVDA (2 sessions) | gate 0, 07-06 | −3.6 | 0.24 |
| BTC (46h) | any | −22.3 | 0.00 |

The resolution of the apparent paradox (dir 0.55–0.59 but hit rates 0.11–0.28):
**reversion is real but smaller than the toll.** When the fade is right, the
retracement usually doesn't cover spread+fees; when wrong, you pay move+toll.
The previous window's move predicts the next move's *sign* better than chance
but not a *magnitude* that clears costs. Statistically real, economically
empty — the exact pattern the move/toll ratio (notebook 05) predicted.

Consequences:
1. "Learn when fade fails" is NOT the reframing — there is no free fade lunch
   to protect. Question closed.
2. The only demonstrated exploitable signal remains the **cross-venue leader
   gap** (07-08 finding), now running live in the paper service (hoeffding @ 5s
   + CB leader) with continuous dual-venue capture for replication.
3. General lesson now confirmed three ways (model sims, overtrading rows, fade
   rule): at these horizons, *direction accuracy without magnitude selectivity
   is worthless after costs*. Everything ahead should optimize expected net bps
   per trade, not directional accuracy.

Ops: pipeline now reconciles at startup (flattens unmanaged account residue —
a killed soak had held ETH for four days); --cb-leader CLI wiring fixed with a
CLI-path regression test (flag had been parsed but silently ignored).

### 2026-07-08 (later) — decision-aware objective + model race

New model kinds: `adaptive` (ADWIN Hoeffding), `forest` (Adaptive Random
Forest), and **`ev`** — the decision-aware continuous objective: three online
linear quantile regressions (q25/q50/q75) on the forward return in bps;
output = the pessimistic side of the interval (q25 if positive → long, q75 if
negative → short, else abstain). Continuous label preserved; magnitude
selectivity built into the output; the policy still charges the per-quote toll.

Race on the dual-venue session (BTC + CB leader, 5s, non-overlapping, n=990):

| model | dir | d-best | MAE edge | trades (fee 5) | net |
|---|---|---|---|---|---|
| hoeffding (live until now) | 0.648 | +0.077 | −10.9% | 4 | −105 bps |
| adaptive | 0.609 | +0.037 | −7.7% | 2 | −59 |
| forest | 0.620 | +0.049 | **+2.3%** | 0 | 0 |
| meta | 0.677 | +0.105 | +0.4% | 2 | −59 |
| **ev** | **0.700** | **+0.128** | +1.4% | 0 | 0 |

**`ev` is the new best config in the project** — highest direction, largest
baseline margin yet, positive MAE edge, and it abstains at crypto tolls
(nothing clears ~21bps; correct behaviour). Notable: linear quantiles BEAT
tree regressors on identical features — the leader gap is a strong, roughly
linear signal, and quantile pessimism handles the noise that made plain SGD
overcommit. Same replication caveat as the original leader finding (one 3h
session); the live service now runs `ev` so the paper record measures it
continuously, and tomorrow's 6-pair data retests everything.

**Answer to "should this be a continuous prediction task?"** Yes for the
label (returns are continuous; binarising at a fixed band mis-specifies the
per-quote toll and discards information) — but the *decision* should come from
the outcome distribution, not the mean: commit only when a pessimistic
quantile clears zero, and let the policy charge costs. Mean-regression was the
wrong shape for a trading decision; quantile intervals are.

### 2026-07-09 — cross-venue replication across 6 pairs: CONFIRMED but ECONOMICALLY EMPTY

First nightly auto-research (ev @ 5s, non-overlapping, per-pair sim). The
staleness thesis is confirmed in DIRECTION and decisively refuted in ECONOMICS.

| pair | n | dir | fade | d-best | MAE edge | sim net | spread | gap σ |
|---|---|---|---|---|---|---|---|---|
| BTC | 15526 | 0.622 | 0.566 | +0.057 | −1.4% | −100 | 11 | 1.9 |
| ETH | 3346 | 0.856 | 0.467 | +0.323 | +14% | 0 | 11 | 3.7 |
| LTC | 893 | 0.700 | 0.469 | +0.169 | −5.7% | −165 | 69 | 11.1 |
| DOGE | 1158 | 0.911 | 0.480 | +0.39 | +10.5% | −211 | 39 | 5.5 |
| LINK | 1042 | 0.932 | 0.469 | +0.40 | +37% | +4 | 21 | 3.8 |
| SOL | 1329 | 0.933 | 0.508 | +0.42 | +30% | −27 | 45 | 6.0 |

**Confirmed:** d-best scales inversely with liquidity exactly as predicted —
BTC (densest) +0.057, sparse alts +0.32–0.42, directional accuracy up to 0.93.
The venue gap genuinely predicts the follower's next move; ETH replicates the
07-08 BTC result and the alts amplify it.

**But it is not alpha — it's stale-quote convergence.** Two tells:
1. **0.93 directional accuracy that LOSES money** (DOGE −211bps, LTC −165bps on
   real cost-charged sims). Direction without magnitude, the project's core
   lesson, in its most extreme form yet.
2. **The move is ~1/6 of the toll on every pair.** gap σ / spread: BTC 0.17,
   ETH 0.34, SOL 0.13, DOGE 0.14, LTC 0.16, LINK 0.18. Alt spreads are 21–69bps;
   the gap-driven move is 4–11bps. The universe got bigger but the move/toll
   ratio (notebook 05) is unchanged — we just scaled both signal and toll.

Mechanism: Alpaca's thin alt venue mid lags dense Coinbase and mechanically
catches up. "Predict that a stale number will update" is trivially accurate and
worthless — you'd trade at Alpaca's post-catch-up, wide-spread price. High
d-best on illiquid venues is a DATA-QUALITY signature, not an opportunity.

Consequences:
- The cross-venue edge is REAL but UNTRADEABLE at Alpaca crypto spreads. The
  only path to capture is cutting the toll below the move: maker/limit orders
  at/inside the mid (needs a fill-probability model), or a cheaper venue.
- Decisive next test still worth running: a sign(gap) baseline — if it matches
  the model's 0.93, the ML adds nothing over the raw gap (very likely here).
- Do NOT chase the shiny alt d-best numbers. They are the artifact, not the win.

## 2026-07-09 — sign(gap) baseline: the ML is NOT a gap indicator (verdict reversed)

Ran the queued decisive check (`scripts/gap_baseline.py`, ev @ 5s, non-overlapping,
322k events on `data/paper_live.duckdb`, all 6 pairs each with CB leader). Fixed
an `evaluate()` robustness gap first: the crossfeed only populated when the leader
was ALSO listed in `symbols` (nightly_research/research_batch both do
`eval_syms=[leader, sym]`, so their historical numbers STAND — not corrupted).
Passing leaders without adding them to `symbols` — as this script does — silently
gave `leader_gap_bps` = 0. `evaluate()` now spins up publish-only leader engines
for leaders not in `symbols`, mirroring live. (90 tests green.)

| sym | n | model_dir | gap_dir | fade_dir | agree% | dir\|agree | dir\|disagree | n_dis | model_bps | gap_bps |
|-----|---|-----------|---------|----------|--------|-----------|--------------|-------|-----------|---------|
| BTC | 7171 | 0.704 | 0.609 | 0.568 | 67% | 0.758 | 0.593 | 1083 | −21.5 | −22.0 |
| ETH | 1335 | 0.965 | 0.944 | 0.458 | 99% | 0.968 | 0.556 | 9 | −18.6 | −18.8 |
| SOL | 1462 | 0.940 | 0.568 | 0.506 | 63% | 0.944 | 0.932 | 441 | −55.3 | −59.3 |
| DOGE | 1268 | 0.919 | 0.535 | 0.482 | 76% | 0.938 | 0.862 | 145 | −50.1 | −54.7 |
| LTC | 970 | 0.721 | 0.501 | 0.465 | 90% | 0.707 | 0.836 | 61 | −96.2 | −98.8 |
| LINK | 1136 | 0.932 | 0.836 | 0.460 | 90% | 0.954 | 0.735 | 98 | −29.5 | −30.4 |

**The "dressed-up gap indicator" hypothesis is REFUTED.** On the alts the raw gap
SIGN is near a coin flip (SOL 0.568, DOGE 0.535, LTC 0.501) — yet the model scores
0.92–0.94, and it holds 0.86–0.93 direction *even on the rows where it disagrees
with the gap sign* (`dir|disagree`, n=441 on SOL). It is also NOT persistence/fade
(`fade_dir` 0.46–0.57 everywhere). So the alt direction is a genuine, independent,
short-horizon predictable signal — not gap-closing, not autocorrelation.

Mechanism, refined: it's not the instantaneous gap *level* sign, it's the leader's
recent *move* (`leader_r1` / `leader_uptick`) — which way Coinbase just went — that
predicts the thin Alpaca alt's catch-up. The gap sign can be +ve while the leader
is falling; the model uses the leader's direction, which is the better predictor.

**But the economic verdict is UNCHANGED and total:** every pair loses 18–96 bps/
trade after the toll (model_bps ≈ gap_bps, both deeply negative). 0.94 direction on
SOL still bleeds 55 bps because the round-trip spread (~60 bps) dwarfs the few-bp
move. Direction is real; magnitude-vs-toll is fatal — the core lesson again.

**What changes:** the signal is NOT an in-principle-untradeable data artifact (my
07-09 framing was too harsh). It's real short-horizon predictability that is purely
COST-BLOCKED. That *strengthens* the case for the maker/limit-order sim: if we can
capture the move at/inside the mid instead of paying the full spread, a genuine
0.94-direction 5s signal is exactly the thing that could survive. The maker sim is
now the arbiter — and it must also rule out that the alt 0.94 is a sparse-quote
resolution artifact (first-price-after-horizon on a thin venue) rather than a move
you could actually fill against.

## 2026-07-09 — maker/limit-order sim: passive execution does NOT rescue it (lever closed)

The arbiter (`scripts/maker_sim.py`). Same model-committed signals, three
executions; passive fills walk the ACTUAL future quote path (no assumed fill),
so adverse selection is modelled honestly. ev @ 5s, taker_fee 5bps, maker_fee 0
(ceiling — Alpaca's real maker fee ~15bps would negate most of this anyway).

net bps/trade (fill% = fills/attempts), wait-window sweep:

| pair | taker | mk_in/tk @2s | @30s | mk/mk @30s | fill% @2s→30s |
|------|-------|--------------|------|------------|---------------|
| BTC  | −20.7 | −8.5 (2%) | −8.5 (8%) | −4.6 | 2→8% |
| ETH  | −19.6 | −12.7 (2%) | −13.3 (9%) | −9.6 | 2→9% |
| SOL  | −54.3 | −24.7 (0%) | −26.0 (1%) | −24.4 | 0→1% |
| DOGE | −46.0 | −21.8 (0%) | −20.0 (2%) | −20.0 | 0→2% |
| LTC  | −86.1 | −25.5 (0%) | −22.3 (0%) | −22.3 | 0→0% |
| LINK | −29.5 | −23.6 (0%) | −17.3 (2%) | −16.3 | 0→2% |

**Two independent walls, both fatal:**
1. **Nothing goes positive.** Passive execution ~halves the toll (BTC −21→−8) but
   the move is smaller than even the maker-reduced cost. Best case anywhere is
   BTC mk/mk −4.6 bps at a 30s rest.
2. **Fill rate collapses (0–9%).** This is the deeper, structural wall: a
   *directionally correct* signal is intrinsically un-fillable passively — when
   the model is right (0.94 on alts) price runs AWAY from your resting bid, so you
   only fill on the minority that dip toward you (adverse selection). Longer rests
   barely lift fills and add adverse selection. Worst exactly where direction is
   best: the thin alts fill ~0–2%.

**Verdict: the maker lever is CLOSED on Alpaca crypto.** Both queued levers
(sign(gap), maker sim) are now exhausted. The cross-venue signal is real but the
per-trade move (a few bps) is below *any* executable cost, taker or maker, and a
correct directional predictor cannot be executed passively at all. The remaining
theoretical escape is not execution but a market where the toll is structurally
1–3 bps not 20–70 bps — i.e. **equities**, where prior sessions already showed
single-digit-bp losses (much closer to the line) vs crypto's 20–96 bp chasm. The
next real question is whether any equities microstructure signal clears a 1–3 bp
toll; crypto cross-venue is a closed book for economics.

## 2026-07-09 — EQUITIES: a 2-session NVDA edge that FAILED out-of-sample (retracted)

Pivoted to equities (zero-commission, 1-3bp spreads) after crypto cross-venue
closed. New tooling: `scripts/equities_eval.py` (EV horizon sweep vs baselines,
zero-fee cost sim), `scripts/equities_selectivity.py` (dead-zone selectivity +
no-micro ablation), `scripts/equities_combo.py` (both knobs, replicated on both
sessions). Data: `equities_2026-07-06/07.duckdb`, SPY/AAPL/NVDA, ~6.5h each.

**Step 1 — EV alone (full features):** losses now ~1bp (vs crypto 20-96bp) but no
edge over the fade baseline (d-best ~0) and overtrades (800-1500 trades). Best
NVDA 10s -0.90, SPY 5s -0.70. Real reversion, a hair under the toll.

**Step 2 — two knobs, each helps one name:**
- SELECTIVITY (raise the dead-zone): NVDA 10s dz4 -> -0.19bps (near breakeven);
  SPY dz2 -0.92. AAPL gets worse with selectivity.
- NO-MICRO ABLATION (drop spread/imbalance/flow/micro/uptick/dt): replicates the
  crypto finding on AAPL — flips it from d-best -0.049 (losing to fade) to +0.002
  (beating fade); momentum/lag carries the signal, micro distracts. SPY worse.

**Step 3 — combine (no-micro + selectivity), replicated on BOTH sessions:**

| cell | 07-06 net/trades | 07-07 net/trades |
|------|------------------|------------------|
| **NVDA 5s dz4** | **+1.30 / 117** | **+1.17 / 312** |
| NVDA 5s dz8 | +3.53 / 41 | +0.68 / 126 |
| NVDA 10s dz4 | −0.91 / 100 | +1.43 / 183 |
| AAPL 5s dz8 | −2.60 / 17 | +0.13 / 38 |
| SPY (any) | mixed/neg | mixed/neg |

**PRELIMINARY (2-session) finding — later OVERTURNED, see below:** NVDA @ 5s,
no-micro EV, dz≈4bp was net-positive (+1.30/+1.17) on 07-06 and 07-07. I flagged it
as needing ~5 sessions before belief. It did not survive the third.

### 2026-07-09 (same day) — THIRD session (07-08) BREAKS it. Finding retracted.

Pulled `equities_2026-07-08.duckdb` (already captured by the server cron — 5.4M
events) as an independent out-of-sample test. NVDA 5s, no-micro, per dead-zone:

| cell | 07-06 | 07-07 | **07-08** | verdict |
|------|-------|-------|-----------|---------|
| NVDA 5s dz2 | −0.22 | +0.06 | **−1.11** | fails |
| NVDA 5s dz4 | +1.30 | +1.17 | **−1.48** | **fails** |
| NVDA 5s dz8 | +3.53 | +0.68 | **−2.24** | fails |
| AAPL 5s dz8 | −2.60 | +0.13 | +1.58 | fails (07-06 neg) |

**On 07-08 NVDA has the HIGHEST direction of the three sessions (dir 0.689, d-best
+0.067) yet loses at EVERY dead-zone.** No (symbol, horizon, dz) cell is green on
all three sessions; the "edge" hops between names by day (NVDA on 06/07, AAPL on
07/08). The 2-session result was **overfitting to two favourable days** — the
out-of-sample discipline did its job and caught a false positive. High direction
that still loses = the project's core lesson once more: direction ≠ net edge.

**Honest state of equities:** liquid US-equity intraday reversion is real and only
~1bp under the toll (vastly closer than crypto), but no config in this toolkit
clears it repeatably across independent sessions. Not closed the way crypto is —
the gap is tiny — but there is no demonstrated out-of-sample edge. Same conclusion
as everything before it: below-cost signal, no free lunch.

**Superseded skepticism list (all now moot, kept for the record):** only 2 sessions;
idealised fills; allow_short; post-hoc dead-zone. The first one (n=2) was the fatal one.

**Process win to keep:** the server already captures a dated equities session every
weekday (cron -> `record.py`, `equities_<date>.duckdb`). The right standing use is a
ROLLING out-of-sample screen: eval each new session and only chase a config that is
green across many independent days — never on 2. `scripts/equities_combo.py --dbs …`
is the per-session screen. Cron timing note: it records 14:30-21:00 UTC, which in EDT
misses the 13:30-14:30 opening hour and includes ~1h of after-hours — worth fixing to
13:30-20:00 UTC (true regular session) if equities work resumes.
