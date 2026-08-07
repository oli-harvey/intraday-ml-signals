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

## 2026-07-10 — JOINT 4-session screen: net not repeatable, but a STABLE direction edge

After the 07-08 retraction, ran a proper joint screen over all 4 captured sessions
(`scripts/equities_joint.py`, 5s no-micro EV, green-count per (sym,dz) + per-session
d-best). Sessions 07-06/07/08/09 (all still the old 14:30-21:00 UTC window; the DST
fix applies from 07-10). Last night's digest cron fired automatically (07-09 logged).

**Net bps (green sessions / 4):**

| sym | dz2 | dz4 | dz8 | best mean |
|-----|-----|-----|-----|-----------|
| SPY | 0/4 | 0/4 | 0/4 | −0.91 (dead — efficient index) |
| AAPL | 1/4 | 1/4 | 2/4 | −0.91 |
| NVDA | 1/4 | **3/4** | 2/4 | **+0.31** (dz4: +0.9/+1.1/−1.1/+0.4) |

NVDA 5s dz4 is the best config in the project — 3/4 green, mean +0.31 bps — but
07-08 still breaks it and the magnitude is within noise. Not a deployable edge.

**Direction edge over baseline (d-best) per session — the actual finding:**

| sym | 07-06 | 07-07 | 07-08 | 07-09 | mean |
|-----|-------|-------|-------|-------|------|
| SPY | −0.041 | −0.016 | −0.001 | −0.006 | −0.016 (no edge) |
| AAPL | +0.036 | +0.021 | +0.015 | +0.020 | **+0.023 (all 4 positive)** |
| NVDA | +0.021 | +0.032 | +0.068 | +0.075 | **+0.049 (all 4 positive, rising)** |

**This is the honest, replicated result:** the no-micro EV model has a small but
CONSISTENT directional edge over the best naive baseline on single-name tech
(NVDA, AAPL) — positive on all 4 independent sessions — and NONE on the index
(SPY). That part is not day-hopping noise. What is *not* stable is converting that
edge to net profit: d-best of +0.02..+0.075 is marginal against the ~1-3bp spread,
so net lands green only sometimes (NVDA dz4 3/4). Retraction of the *net* claim
stands; what's new is that the *direction* signal is real and repeatable, just
sub-toll. Separates "is there a signal?" (yes, weak, single-names only) from "can
we monetise it?" (not yet).

**Concrete next lever (untested):** the net sim charges the full spread every trade.
Since the direction edge is stable, SPREAD-CONDITIONAL entry — trade only when the
quoted spread is tight — could convert a stable ~2-7% direction edge into net-green
without needing a bigger edge. This is the equities analog of the (failed) crypto
maker lever, but here the fill isn't the problem, the toll variance is. Worth a
session-joint test before more model work. Also: SPY has no edge and can be dropped
from the trade set (keep capturing it as the liquidity/context reference).

## 2026-07-10 — SPREAD-CONDITIONAL entry: first robust config (NVDA), with a short-side asterisk

The lever from the joint screen: the single-name direction edge is stable but sub-toll,
so cap the quoted spread at entry (`simulate_trading(max_spread_bps=…)`, condition is
known at decision time — legitimately tradeable, unlike the crypto gap). Swept dz × cap
across all 4 sessions, plus a slippage-haircut robustness filter and a long-only pass
(`scripts/equities_spread.py`).

**Capping the spread converts the stable edge to net-green — a gradient, not a lucky cell.**
No cap: AAPL/NVDA 0–2/4 green. Cap spread <2bp: both go **4/4 green** across all 4 sessions
with real trade counts. SPY: 0/4 at every cap (no edge — the efficient index).

| config (5s no-micro EV, long+short) | 4/4 green | mean net | med trades | survives −1bp haircut |
|---|---|---|---|---|
| **NVDA dz4, spread<2bp** | yes | **+2.5bps** | 146 | **YES (only one)** |
| NVDA dz2, spread<2bp | yes | +1.2 | 296 | no (dies −1bp) |
| NVDA dz4, spread<1.5bp | yes | +2.1 | 115 | no |
| AAPL dz4, spread<2bp | yes | +1.3 | 65 | no (knife-edge, flips 4/4↔3/4 on numerical hair) |

**The one robust config: NVDA, 5s, no-micro EV, dead-zone 4bp, spread<2bp** — 4/4 green
AND survives a 1bp/trade slippage haircut (+2.5 → +1.5), ~146 trades/session. Everything
else is too thin to trust against real fills.

**Two hard caveats, both checked:**
1. **SHORT-DEPENDENT.** Long-only, NOTHING is 4/4 green even at zero haircut (best NVDA
   dz2<2 = 3/4). The edge lives substantially on the short side. Deployment therefore
   needs a margin account + borrow, and the pipeline's PositionBook is currently long-only
   (crypto-era). This is the main blocker.
2. **Trend confound — checked, largely REFUTED for NVDA.** Session open→close: NVDA rose
   3/4 days (+3.4/+4.2/+1.0%, one −0.1%) yet the edge is short-biased — so the shorts fade
   intraday *rallies that revert*, not a downtrend, and at 5s the daily drift is ~0.1bp/
   window (negligible). NVDA's short-bias is reversion ASYMMETRY (up-moves snap back harder
   than dips), a real microstructure effect. AAPL fell 3/4 days, so its weaker short edge
   IS partly trend beta — consistent with it failing the haircut anyway.

**Status:** the first genuinely promising, mechanistically-defensible, replicated (4/4) and
slippage-robust result in the project — NVDA 5s reversion, spread-gated, short-biased. NOT
yet proven: 4 sessions only, all a high-vol NVDA week (+3-4% days), idealised fills, and it
needs short infrastructure. **Next:** (a) accumulate more sessions incl. calm/ up-quiet
regimes via the running capture; (b) add spread-cap + a `<2bp` gate to the digest so the
NVDA config's daily net is tracked in the rolling screen; (c) only after ~10 green sessions
across regimes, consider short support in the book + a paper-live NVDA dry-run. Do NOT
deploy on 4 high-vol days.

## 2026-07-13 — PRE-TRADING AUDIT: two defects found and fixed; the finding survives

Sitrep before the first 30-symbol session. Three real problems, all caught before
they did damage.

**1. The capture would have stalled (fixed, deployed).** `ColdStore.run` did one
`asyncio.wait_for(queue.get())` PER RECORD — a timer handle per event — capping the
writer at **4,025 events/s** (measured, on hardware faster than the VPS). Fine for 3
crypto symbols (~215/s); today's 30-symbol equities capture multiplies the rate, and
a full queue makes `IngestStage.put()` **block**, stalling the websocket reader until
Alpaca drops us as a slow consumer. Greedy `get_nowait()` drain → **825,927 events/s
(205x)**. Recorder queue 50k→250k to absorb the open-bell burst.

**2. The "no-micro" ablation was never micro-free (fixed).** `exclude` was given only
the BASE micro features; the three micro-derived PRODUCT INTERACTIONS survived — and
`flow_x_imbalance` is **trade-derived**. So trades still fed the model, and switching
to a quotes-only capture would have silently changed the config. Canonical
`MICRO_FEATURES` (base + ratio + product) now lives in `features/engine.py`; all five
research scripts import it so the list can't drift. **Verified: with a true no-micro
ablation the engine is EXACTLY trades-invariant — 0 of 394,696 feature vectors differ
with trades stripped.** Quotes-only is therefore comparable to the historical sessions.

**3. Replay was NON-DETERMINISTIC (fixed).** `ReplaySource` ordered by `recv_ns`
alone, but one WS frame delivers many quotes sharing a `recv_ns` (**63.8%** of NVDA
rows tied, up to 75/frame) and DuckDB's parallel sort broke ties differently each
run. The same DB scored differently every time: **stdev 0.14bps, spread 0.31bps** on
a ~3bps signal — enough to flip a thin session green/red, and it masqueraded as a
"trades matter" effect. Now ordered by `(recv_ns, ts_ns, symbol)` — a total order
that is also semantically correct (arrival batch, then exchange time within it).
Same DB 3x is byte-identical. **Every number before today carried this noise.**

**The finding SURVIVES both corrections, slightly stronger.** NVDA 5s no-micro dz4
spread<2bp, re-scored deterministically:

| session | NVDA net | Lnet (long-only) | trades | AAPL net |
|---|---|---|---|---|
| 07-07 | +3.78 | +3.83 | 240 | +0.83 |
| 07-08 | +2.51 | −0.00 | 134 | +3.44 |
| 07-09 | +3.32 | −0.11 | 138 | +1.62 |
| 07-10 | +2.76 | −0.31 | 98 | +3.04 |

NVDA **4/4 green, min +2.51** (every session clears a 1bp slippage haircut), still
**short-dependent** (long-only is ~0/negative on 3 of 4). AAPL also 4/4 but thinner
(+0.83 min). CONFIG_ID → `nomicro2` so the rolling tally doesn't mix the old leaky,
non-deterministic rows.

**Status unchanged in substance:** promising, replicated, slippage-robust, but still
4 sessions of one high-vol week, idealised fills, and short-dependent (needs margin +
borrow + short support in the book). The audit raises confidence in the *measurement*,
not the sample size. Do not deploy on 4 sessions.

## 2026-07-14 — CADENCE: the headline +3.3bps is ~3x inflated by non-overlapping scoring

Built a live shadow book (`scripts/stocks_live.py` + `signals/livesim.py`) so the bot
can report trades DURING the session. Validating it against the nightly digest exposed
a serious measurement error in every equities headline so far.

`evaluate(non_overlapping=True)` exists to score DIRECTION on independent windows —
correct, and long-standing hygiene. But the digest also runs `simulate_trading` on
those same subsampled rows, which silently makes the BACKTEST take **one entry per 5s
window**. A live strategy sees every quote and enters on the first signal that clears
the bar. Those are different strategies, and the difference is most of the edge:

| NVDA, 07-09 (dz4, spread<2bp) | trades | avg net |
|---|---|---|
| windowed (one look per 5s — what every headline reported) | 138 | **+3.320** |
| per-quote (act on every signal — the naive live rule) | 1,435 | **+1.089** |

Same model, same rule, same data. The gap is **entry-timing selection bias**: acting on
every quote means entering at the first threshold UPCROSSING, which systematically buys
noise spikes. The windowed cadence never sees those.

**Why this is decisive:** the 1bp slippage haircut was the bar the finding had to clear.
+3.32 clears it (+2.32). **+1.09 does not (+0.09 ≈ zero).** So whether the NVDA result is
an edge at all depends entirely on a cadence choice that was never stated — it was an
artifact of a scoring flag, not a designed strategy.

**Not necessarily fatal**, but it must be earned rather than assumed: "sample the signal
once per horizon and act on that reading" IS a legitimate, implementable rule, and it is
the one the +3.3bps describes. What is NOT established is that it survives a different
sampling phase — the windowed grid here is set by resolution timing, not a clean clock.
**Next: sweep the sampling phase/grid. If +3.3 is stable across phases it is a real rule;
if it swings, it is a lucky grid and the honest number is the per-quote +1.09.**

Both books now run live and BOTH are reported (status_stocks.json, Telegram), so the
number can never quietly mean the wrong thing again. The live windowed book reproduces
the digest exactly (NVDA 138 @ +3.3201) — verified by replaying the live code path over
a recorded session. Also fixed while validating: stocks_live dropped events on a full
queue (silent data loss) — restored the blocking put; 2.74M rows, 0 dropped.

## 2026-07-14 (cont.) — PHASE SWEEP: the windowed edge is a lucky grid. Headline retracted.

Tested whether the windowed rule ("sample the signal once per 5s and act on that
reading") is real or an artifact of the particular grid the scorer happened to land on.
Replaced the data-driven grid with an ABSOLUTE clock grid — the rule you would actually
implement — and swept the phase offset across the whole window
(`scripts/cadence_sweep.py`, 10 phases × 4 clean sessions).

**NVDA, windowed, net bps/trade by sampling phase:**

| session | phase-mean | sd | range | phases ≤0 | phases failing the 1bp haircut |
|---|---|---|---|---|---|
| 07-07 | +3.17 | 1.24 | +0.93 … +5.58 | 0/10 | 1/10 |
| 07-08 | +4.05 | 1.71 | +0.83 … +6.46 | 0/10 | 1/10 |
| 07-09 | +1.92 | 1.31 | −1.12 … +3.77 | 1/10 | 1/10 |
| 07-10 | **+0.36** | 1.24 | −1.78 … +2.68 | **5/10** | 6/10 |

**Verdict: the +3.3bps headline was a lucky grid.** The phase spread WITHIN one session
(4.5–5.6bps) exceeds the effect (~3bps). The celebrated "+3.32 on 07-09" is one phase of
a distribution whose mean is +1.92 and whose worst phase is −1.12. On 07-10 half the
phases lose money. A genuine microstructure edge cannot depend on which half-second you
sample at; this one does.

**What survives, honestly:** a residual positive tilt. Windowed still beats per-quote on
average (phase-means +3.17/+4.05/+1.92/+0.36, avg ≈ +2.4, vs per-quote +0.41/+3.02/
+1.09/+0.03, avg ≈ +1.1) — so avoiding the first-upcrossing entry bias is a REAL effect,
just a much smaller one than advertised. But per-session it swings from +0.36 to +4.05,
and after a 1bp slippage haircut two of four sessions are at or below zero.

**So: NVDA spread-gated 5s reversion is NOT a deployable edge.** The chain of claims —
"4/4 green, survives a 1bp haircut, +2.5..+3.8bps" — rested on a single arbitrary
sampling grid. It is retracted. What is left is a weak, phase-sensitive tilt of ~+1-2bps
that is not reliably above slippage — i.e. the same wall as every other result in this
project: real short-horizon structure, no net-of-cost edge.

**Consequences:**
- The nightly digest currently reports the single lucky grid. That number is misleading
  and must be replaced by the phase-MEAN (the expectation of the implementable rule) plus
  the per-quote figure. Until then, treat the tally's green count as unearned.
- The methodological bug is general: `non_overlapping=True` is right for SCORING
  direction and wrong for SIMULATING trading. Any future config must be phase-swept
  before it is believed.

## 2026-07-15 — HORIZON sweep: longer holds are WORSE. The last lever is refuted.

The one principled lever left: the spread toll is fixed (~1-2bp) but a move grows ~sqrt(t),
so a longer hold might clear it. Tested properly — phase-swept, corrected ablation,
deterministic replay (`scripts/horizon_sweep.py`, 6 phases, 2 sessions 07-08/07-10):

| sym | horizon | net (phase-mean) | ±phase | net/±ph | d-best | sessions>0 |
|-----|---------|------------------|--------|---------|--------|-----------|
| NVDA | 5s | +2.35 | 3.05 | 0.77 | 0.055 | 2/2 |
| NVDA | 30s | +0.21 | 6.84 | 0.03 | 0.031 | 1/2 |
| NVDA | 60s | +0.49 | 6.19 | 0.08 | 0.022 | 1/2 |
| AAPL | 5s | +2.94 | 1.90 | **1.55** | 0.034 | 2/2 |
| AAPL | 30s | +0.32 | 3.95 | 0.08 | 0.040 | 2/2 |
| AAPL | 60s | −0.83 | 4.16 | −0.20 | −0.003 | 0/2 |

**Refuted.** The bigger move at longer horizons is beaten by TWO effects: (1) the direction
edge DECAYS with horizon (reversion fades — d-best falls monotonically), and (2) phase
fragility GROWS (fewer trades -> noisier; ±phase doubles from 5s to 30s). Net collapses to
~0 by 30s. 5s is the optimal horizon, which is the reversion story restated.

**⚠ SUPERSEDED — do not cite the table above or the "bright spot" below.** The 2-session
run (07-08/07-10) was preliminary; both are corrected by the 4-session sweep and again by the
6-session sweep in the sections that follow. The `net/±ph > 1` "bright spot" for AAPL @ 5s was
a small-sample artifact — it does not survive more sessions. Kept here only as the record of
the over-claim the project's own fragility metric then caught. The horizon *refutation*
(longer = worse, 5s optimal) is the durable finding and is reconfirmed below.

**State of the project, honestly (as of the 2-session run):** every lever is exhausted —
execution/maker (closed), cheaper venue via equities (real direction, sub-toll net),
spread-conditional entry (lucky grid, retracted), horizon (refuted). What remains is
consistent and small: real short-horizon reversion DIRECTION on liquid single-name tech
(d-best +0.03..+0.06, replicated), whose net-of-cost value is marginal and phase-fragile.
No durable edge demonstrated. See the corrected verdict below for the current numbers.

### 2026-07-15 (correction) — the AAPL "bright spot" was a 2-session artifact

The full 4-session horizon sweep (8 phases; the heavy run I'd assumed was killed actually
finished) walks back the AAPL claim above. On all four sessions:

| sym | horizon | net (phase-mean) | ±phase | net/±ph | d-best | sessions>0 |
|-----|---------|------------------|--------|---------|--------|-----------|
| NVDA | 5s | +2.44 | 4.52 | 0.54 | 0.055 | 4/4 |
| NVDA | 15s | +0.29 | 3.71 | 0.08 | 0.043 | 2/4 |
| NVDA | 30s | +0.38 | 5.12 | 0.07 | 0.061 | 2/4 |
| NVDA | 60s | +0.11 | 11.17 | 0.01 | 0.046 | 3/4 |
| AAPL | 5s | +2.36 | 2.59 | **0.91** | 0.029 | 4/4 |
| AAPL | 15s | +0.52 | 3.57 | 0.15 | 0.002 | 2/4 |
| AAPL | 30s | −0.20 | 5.32 | −0.04 | 0.003 | 1/4 |
| AAPL | 60s | −1.20 | 6.13 | −0.20 | −0.021 | 0/4 |

**AAPL @ 5s is 0.91 on 4 sessions, NOT 1.55.** The lean 2-session run (07-08/07-10) landed
on AAPL's two best days — my own small-sample over-claim, the very thing this project keeps
tripping on. Corrected: **NO config clears net/±ph > 1.** Both single names have a phase-
mean that is reliably positive at 5s (+2.4bps, 4/4 sessions) but a phase spread that EXCEEDS
that mean (0.54 / 0.91), so any single day-or-phase is roughly a coin-flip around a small
positive. Horizon conclusion is otherwise unchanged and now confirmed on 4 sessions: 5s
optimal, longer strictly worse.

**Final honest bottom line:** a weak, real, positive TILT (~+2bps phase-mean, direction
d-best +0.03..+0.06, both replicated 4/4) that is not robust to sampling phase and not
reliably above slippage. Real short-horizon structure; no deployable net-of-cost edge. The
wall holds. Methodology (phase-mean + fragility, deterministic, corrected ablation) is the
durable deliverable — it makes the next over-claim impossible to ship silently, including,
belatedly, this one.

## 2026-07-16 — more data, a cross-check, and a time-of-day test. The wall holds on all three.

Three things run against the corrected methodology; none breaks the wall.

**1. The honest 5s number on 6 sessions (was 4).** Added 07-14 (07-13 stays quarantined;
07-15 was still live). Phase-swept horizon sweep, 8 phases, dz4 spread<2bp:

| sym | horizon | net (phase-mean) | ±phase | net/±ph | d-best | sessions>0 |
|-----|---------|------------------|--------|---------|--------|-----------|
| NVDA | 5s | +2.71 | 4.14 | **0.65** | 0.057 | 6/6 |
| AAPL | 5s | +1.74 | 2.18 | **0.80** | 0.029 | 6/6 |
| (both) | 15s/30s/60s | ≤+0.6 | grows | ≤0.15 | decays | ≤3/6 |

Both single names are positive on **all six** sessions at 5s (direction is real and
consistent), but **neither clears net/±ph > 1** and 50% more data did not move them toward
it: NVDA 0.54→0.65, AAPL 0.91→**0.80** (down — the AAPL "bright spot" keeps shrinking as n
grows, exactly the small-sample-artifact signature). 5s remains optimal; longer strictly
worse, now confirmed on 6 sessions.

**2. Live shadow book == offline backtest, exactly, on a real session.** `live_vs_backtest.py`
drives BOTH the live `LiveSim` books and the offline `evaluate` row-collection off ONE replay
pass of 07-14, so predictions are identical by construction and only the trade-accounting is
under test. Result: identical trade counts and net to 4 decimals (Δnet 0.0000) for NVDA/AAPL/
SPY, both cadences. **No train/serve skew** — the nightly digest and the live Telegram numbers
mean the same thing, so the negative result is trustworthy from both directions. (Side note:
07-14 was NVDA-good / AAPL-**negative** windowed — the day-to-day swing the fragility metric
exists to expose, and another nail in the AAPL-is-special coffin.)

**3. Time-of-day conditioning (open vs midday) — the reversion is strongest at the open
DIRECTIONALLY, but net-of-cost the open is the WORST slice.** `tod_sweep.py`, ET buckets,
same machinery. Only DST-fixed sessions captured the 09:30 open, so open/close rest on n=2
and few trades — the noisiest cells, flagged as such:

| sym | bucket | net | ±phase | net/±ph | d-best | sess>0 |
|-----|--------|-----|--------|---------|--------|--------|
| NVDA | open | +1.60 | 11.32 | 0.14 | **0.101** | 2/2 |
| NVDA | mid/aft | +2.7/+2.9 | ~6 | 0.44/**0.51** | 0.05/0.06 | 4-5/6 |
| AAPL | open | +4.33 | 8.59 | 0.50 | 0.037 | 2/2 |
| AAPL | morn | +2.08 | 3.62 | **0.57** | 0.049 | 5/6 |
| (both) | close | swings ±5 | ~19 / ~8 | ≤0.26 | ~0 | thin |

The microstructure prior is half-right: **d-best IS highest at the open** (NVDA 0.101 — the
reversion signal genuinely concentrates there). But opening spreads are wide and phase
fragility explodes (±11–19bps), so net-of-cost the open is the LOWEST net/±ph bucket, not the
highest. **No bucket clears net/±ph > 1**; the trustworthy midday/afternoon cells (n=5-6) top
out at ~0.55. The flat whole-session headline was not hiding an open-only edge — it was
averaging a directionally-stronger-but-costlier open against a weaker-but-cheaper afternoon.

**Verdict, unchanged and now stress-tested from three new directions:** a weak, real, positive
short-horizon reversion TILT on liquid single-name tech (5s, direction replicated 6/6, ~+2bps
phase-mean) whose net-of-cost value is below its own phase/day fragility (net/±ph 0.65-0.80)
and is not rescued by horizon, cheaper venue, spread-gate, OR time-of-day. Not deployable. The
un-foolable rolling screen now also posts a cumulative net/σ tally nightly, so if the wall ever
cracks it will announce itself unattended. Nothing so far cracks it.

## 2026-07-16 — PRE-REGISTERED kill/continue rule (written before the data arrives)

This project's recurring failure mode is deciding *after* seeing the numbers. So the decision
rule for the equities hunt is fixed NOW, while the tally stands at 7 clean sessions and no
config clears any bar. When the history under `ev_nomicro3_5s_dz4_sc2_phasemean10` reaches
**15 clean sessions**:

- **CONTINUE** only if, for at least one tracked name, BOTH hold on the full tally:
  (a) cumulative **net/σ ≥ 1** (mean nightly phase-mean net ≥ its across-session stdev), AND
  (b) the phase-swept **net/±ph ≥ 1** re-run over all 15 sessions.
- **ARCHIVE** otherwise: active equities research stops; capture + nightly screen may keep
  running unattended (they are cheap and the tally keeps accruing), but no new levers, no new
  configs, no "one more idea" without NEW data first. A later reopen requires the nightly
  tally itself to print net/σ ≥ 1 sustained for 5 consecutive sessions — the screen must
  volunteer the signal; we do not go looking for it.

No mid-course peeking exceptions, no swapping the tracked names after the fact, no counting
quarantined sessions. Basis for the thresholds: both ratios are "effect exceeds its own
noise" — the minimum any deployable claim must clear, and nothing in 7 sessions has come
within 20% of either.

## 2026-07-16 — cross-sectional residual reversion: the LAST untested lever. Same wall.

Every prior test was univariate. `scripts/xsec_sweep.py` tests the one genuinely new signal
class our simultaneous 30-symbol capture enables: fade the RESIDUAL r_sym − r_hedge over 5s
buckets (hedge = SPY/QQQ, β=1 so zero fitted parameters), dollar-neutral, both legs' spreads
charged at entry, phase-swept (8 offsets), rule-based — nothing to overfit. Only the SPY-hedge
cells have 6 sessions (QQQ/MSFT/TSLA exist in 1 local session — the universe was 3 names
before 07-14; those cells are n=1 leads, not results).

| sym/hedge | dz | gross | net | ±phase | net/±ph | dir | tr/day | sess>0 |
|-----------|----|-------|-----|--------|---------|-----|--------|--------|
| AAPL/SPY | 8 | +3.12 | +1.19 | 2.12 | 0.56 | 0.57 | 119 | 5/6 |
| AAPL/SPY | 12 | +4.18 | **+2.23** | 2.96 | **0.75** | 0.57 | 65 | **6/6** |
| AAPL/SPY | 16 | +4.80 | +2.82 | 5.29 | 0.53 | 0.57 | 38 | 6/6 |
| NVDA/SPY | 12 | +3.59 | +1.72 | 4.78 | 0.36 | 0.54 | 166 | 6/6 |
| NVDA/SPY | 16 | +4.40 | +2.53 | 5.97 | 0.42 | 0.54 | 129 | 6/6 |
| TSLA/* | any | ≤+0.95 | negative | — | — | 0.51 | — | 0/1 |

**The residual genuinely reverts** — dir 0.54–0.57 vs the 0.5 coin-flip a β=1 hedge implies,
gross monotonically rising with the dead zone to +5.4–5.8bps, and at dz12–16 the NET
phase-mean is positive on **all six sessions** for both names. That is the cleanest gross
signal the project has found. And it still fails the same way everything fails: **phase
fragility grows faster than the edge** (±ph 3.0→9.4 as dz rises), peak net/±ph = 0.75
(AAPL/SPY dz12) — below the univariate AAPL's 0.80, below the bar, on the exact
selectivity-vs-fragility ridge every other lever died on. The double toll costs ~+0.5–1bp
and buys a cleaner signal worth about the same. TSLA's residual barely reverts (0.51) —
idiosyncratic TSLA moves trend, consistent with the liquidity-graded fade found on 07-06.

**Verdict: fifth lever, same wall.** Real structure (now in residual space too), sub-fragility
net. The kill/continue rule above stands unmodified; this result does not feed the tally (it
is a different config family) and does not reopen anything.

## 2026-07-17 — the screen is live again; first honest tally reads BOTH names > 1 at n=7. No action.

The OOM chain is fixed (batched screen -> streaming replay -> batch=2 default; the cron is now
flock-guarded so a backfill can never again race the nightly run) and the history is seeded
under `ev_nomicro3_5s_dz4_sc2_phasemean10`. The first cumulative tally, n=7 clean sessions:

| | per-session phase-mean net (bps) | mean | σ (across days) | net/σ | green |
|---|---|---|---|---|---|
| NVDA | +1.19 +3.17 +4.05 +1.92 +0.36 +4.63 +5.96 | **+3.04** | 1.99 | **1.52** | 7/7 |
| AAPL | +0.53 +0.99 +3.12 +1.36 +2.74 +0.64 +1.05 | +1.49 | 1.03 | **1.45** | 7/7 |

NVDA's PER-QUOTE net (the implementable-without-a-grid cadence) is also 7/7 positive
(+0.41 +0.41 +3.02 +1.09 +0.03 +2.32 +4.71, mean +1.71). 07-16 is the strongest session yet
(+5.96 windowed / +4.71 per-quote — nearly equal, i.e. NOT a windowing artifact that day).

**What this does and does not mean.** The across-DAY axis (stability of the phase-mean) now
clears 1 on both names — an axis never separately tallied before. The within-day axis does
NOT: net/±ph is still 0.65/0.80, meaning a live strategy running ONE sampling phase still
carries fragility bigger than the mean, and ±ph on 07-16 was 7.3. The pre-registered rule
requires BOTH ratios ≥ 1 at n=15 exactly so that a good week on one axis doesn't restart the
research loop at n=7 — and 5 of these 7 sessions are one high-vol tech week, the known regime
caveat. **Decision remains scheduled at n=15. Nothing is reopened. The screen accumulates.**

**Addendum, hours later (07-15 backfilled, n=8):** NVDA +4.09 that day, AAPL negative — tally
now NVDA **1.68** (8/8), AAPL **0.84** (7/8). The "both names > 1" observation above lasted
exactly one session. QED the rule: at this n, single days move the ratio by ±0.6; anyone
acting on the n=7 read would already have been wrong about AAPL by dinner. n=15 stands.

## 2026-07-17 — phase persistence test (a challenge from Oli): luck RE-ROLLS daily. Rule amended, transparently.

Oli asked the right question: "is the luck test needed? surely accumulating samples averages
lucky and unlucky timings out." That is true **iff phase luck is independent across days** —
if instead some phases are persistently better (clock-aligned microstructure: quote refresh
cycles, second-boundary algos), a phase-locked deployment carries a permanent offset that
never averages out. Empirical question; tested on the net(day, phase) matrix (6 sessions ×
10 phases, scratch `phase_persist.py`):

- NVDA: per-phase-mean spread sd 0.63 vs 0.51 expected under the no-persistence null;
  cross-day phase-rank correlation **+0.11**. AAPL: 0.35 vs 0.34; rank corr **−0.08**.
- **Verdict: no evidence of persistent phase structure. Which phase wins is re-rolled every
  day.** Even a phase-locked strategy converges to the phase-mean across days; the within-day
  phase noise (sd ≈ 1.25bps NVDA, 0.83 AAPL per day) only fattens day-to-day variance.

**What survives unchanged:** the phase SWEEP as a backtest-honesty instrument. Its job was
never forecasting deployment variance — it was killing the cherry-pick (configs get promoted
BECAUSE their arbitrary grid was lucky; the retracted +3.32 headline was exactly that).
Every new config headline must still be phase-swept. Non-negotiable.

**What was wrong and is hereby AMENDED (at n=8, before days 9–15 exist):** arm (b) of the
kill/continue rule used within-day net/±ph as a DEPLOYMENT bar. Given no persistence, that
mistakes daily-re-rolled noise for a permanent defect — Oli's point, confirmed. Amended rule
at n=15: **CONTINUE iff, for a tracked name, cumulative net/σ′ ≥ 1, where σ′ inflates the
across-day σ with the single-phase noise a real deployment feels: σ′ = sqrt(σ² + σ_ph²)**
(σ_ph = 1.25 NVDA / 0.83 AAPL; a phase-jittered implementation would earn the phase-mean
directly and may use σ unadjusted). At n=8 this reads NVDA 3.2/√(1.9²+1.25²) ≈ **1.4** (still
above), AAPL ≈ 0.7 (still below) — the amendment changes the measure, not today's verdict.
Motivated-reasoning check, stated plainly: this amendment relaxes a bar while the tally looks
good, which is exactly the failure mode this log documents — it is accepted anyway because it
was (1) prompted by an outside challenge, (2) decided by a test on already-collected sessions,
not on tally outcomes, and (3) replaces an ad-hoc bar with the variance a deployment actually
experiences. The n=15 date and everything else stand.

## 2026-07-18 — OWNER DECISION: skip the n=15 wait, measure with REAL paper orders.

Oli: "n15 is too long for just gathering data... we will carry out trades to test it
properly — it's a better measurement and only paper money." He is right that it is a better
measurement: every number so far rests on the sim's cost model (enter/exit at NBBO mid±half-
spread, zero latency). Real paper fills make that model an empirical question instead of an
assumption. This is not the tally being abandoned — the shadow books and nightly screen run
unchanged — it is a THIRD measurement of the same config added on top:

`signals/stockstrader.py`, wired into stocks_live.py behind `--trade` (refused with
--replay). Enabled from Mon 2026-07-20 via run_equities_capture.sh:

- **Entries at PREDICTION time** (the shadow book books at label-resolution — hindsight; a
  real trader acts on the forecast), same `simrule.decide` verbatim, windowed cadence (one
  look per 5s per symbol; phase re-rolls daily per 07-17).
- NVDA + AAPL, **whole shares** (shorts can't be fractional), $1000 max/position, max 2
  open, **$25/day realized-loss halt**, EOD flatten. Market DAY orders both ways — paying
  the spread is the point; that IS the toll being measured.
- **Every trade records its own frictionless-sim counterpart** (signal-mid → current-mid,
  minus quoted spread). The headline deliverable is `sim_gap_bps` = real net − sim net:
  the sim's cost-model error, measured per trade, in Telegram live and nightly.
- **Shared-account safety:** the crypto pipeline's startup/exit flatten was ACCOUNT-WIDE
  (`close_all_positions`) — it would have nuked stock positions on any restart, and the
  stocks EOD flatten would have nuked crypto's. Both now use `flatten_symbols(own)`;
  `flatten_all` is demoted to a human kill-switch.

Success/failure reads the same as ever, now with real fills: paper avg net per trade vs the
shadow book's, and sim_gap_bps ≈ 0 (cost model honest) vs strongly negative (the sim was
flattering us — which would retro-taint every prior number and be a finding in itself).

**2026-07-19 amendments (Oli):** (1) *"not just NVDA and AAPL, any stocks that are predicted
to be profitable"* — the trade universe defaults to ALL captured symbols; which signals are
"predicted profitable" is simrule's per-signal call (dead zone + spread gate), not a
hand-picked list. Caps rescaled: max 6 open (order rate stays well under Alpaca's 200/min),
$50/day loss halt. (2) *Split the paper funds*: the one $100k account is virtually divided
(`signals/books.py`) — **stocks book = $50k + its own cumulative P&L** (persisted
`data/stocks_book.json`, written per exit), **crypto book = account equity − stocks book**,
so the small pre-split loss (~$70) is attributed to crypto and the stocks book opens at
exactly $50k. Invariant by construction: books sum to account equity. All bot messages now
report per-book: crypto trade alerts + daily show the crypto book, stocks heartbeats/close +
nightly digest show the stocks book.

## 2026-07-21 — real fills, two sessions in: sim_gap looked catastrophic (-14bps). It wasn't.

"Review models, results so far, improve" (Oli). First look at the real book after 07-20/07-21:
298 trades, avg net **-1.35bps**, `sim_gap_bps` **-14.13bps** — over 5x the entire edge ever
being chased. Individual trades showed the tell before the aggregate did: `recent` trades had
sim_net_bps values of −92.53, +76.37, −53.12 bps on a 5s horizon signal that has never
measured more than a few bps — an impossible magnitude, not a real cost.

**Root cause, found via Alpaca's own order timestamps (`submitted_at`/`filled_at`):** entry
orders on this paper account take **1-2.4 seconds** to confirm — 20-48% of the entire 5s
horizon. The old `sim_net_bps` compared mid-AT-SIGNAL to mid-AT-EXIT, a window silently
inflated by however long that fill took; `net_bps` (real) correctly measured fill-to-fill.
For a reversion signal, that window mismatch alone produces comparisons unrelated to the real
trade — exactly the wild swings observed. **This was a measurement-methodology bug, not
evidence the strategy is 5x worse than believed** — the same discipline this project applies
to everything else, now applied to itself.

**Fixed (`signals/stockstrader.py`):**
1. `sim_net_bps` now compares mid-AT-FILL-CONFIRMATION to mid-AT-EXIT — the same window the
   real trade lived through. Apples to apples.
2. The genuine cost of the delay — how far price moved between the signal and the confirmed
   fill — is real and is now its own honest metric, `entry_slippage_bps`, never again baked
   into a comparison bug.
3. Hold duration is now `horizon - measured entry latency` (floored at 0), so the exit fires
   ~5s after the SIGNAL as designed, not ~5s after a fill that itself arrived 1-2s late (every
   real trade was running 7-9s total before this fix — inside the regime the horizon-sweep,
   RESEARCH 2026-07-15, showed is strictly worse than 5s).
4. Full per-trade history now persists to `logs/stocks_trades.jsonl` — the old `recent`
   window (last 10) is exactly why this took a manual server-side investigation instead of a
   query. Latency + slippage are surfaced in every report (live blotter, position line,
   dashboard).

**What this does NOT do:** retroactively fix the 298 already-recorded trades (their sim_gap
stays wrong in history) or prove the strategy is fine — it removes a specific, confirmed
measurement artifact. The honest open question the fix reveals: **is 1-2.4s fill latency on
this paper account itself a structural blocker for a 5s-horizon strategy, separate from
whether the cost model is accurate?** That's now measurable cleanly (`entry_slippage_bps`,
`avg_round_trip_latency_s`) instead of buried in a broken comparison. Next few sessions'
readings under the fix are the real answer, not the -14bps number above.

**Model review, briefly:** the `ev` model (three online quantile regressions, pessimistic-
quantile decision rule) is unchanged and not implicated by this bug — it's a comparison-layer
fix, downstream of prediction. One flag while reviewing: the crypto pipeline's live
`status.json` per-symbol `dir` (0.83-0.96 across pairs) is a *rolling, overlapping* metric —
the same shape of number this project's own early research (2026-07-03/09) showed is inflated
by autocorrelation on overlapping windows. It is a legitimate health/drift diagnostic, not a
claim of that much directional skill; the properly-scored, non-overlapping, baseline-relative
number is what `docs/RESEARCH.md`'s equities work reports (d-best), and no equivalent honest
crypto re-score exists yet. Flagged, not actioned — no evidence pulled that crypto's live
numbers are wrong, only that they're the same *kind* of number this project already knows not
to trust at face value without the non-overlapping correction.

## 2026-07-21 (evening) — REGROUP (Oli): one live day answered the question. Trading OFF, crypto OFF.

Oli: "regroup, review what we have seen and simplify. turn off crypto unless you can find a
better algorithm. for stocks review the trading which lost money and think about why. come
back tomorrow with a strategy that doesnt use short selling and simulate the best option if
i had put 100 dollars of real money in."

**The loss autopsy (356 real round trips, one full session, logs/stocks_trades.jsonl):**
avg net **-1.53bps**, win rate 16.3%, -$47.49 on the day, halted at the $50 cap. The
decomposition is unambiguous:

- **Gross direction (before costs) = 47.5% — a coin flip.** The live model called direction
  no better than chance.
- **Long and short lost identically** (-1.59 vs -1.48bps): shorting was NOT the problem;
  direction was.
- **Prediction magnitude was anti-informative**: |pred| 4-6bps bucket lost -1.27; 12+bps
  bucket lost -1.95. Bigger conviction, bigger loss.
- The loss mechanism is the toll: ~1bp spread + ~0.7bp measured entry slippage against a
  ±1-2bp 5s move — the backtest's own verdict (net/±ph 0.65-0.80, never above 1) played out
  live, at ~-$0.13/trade × 356 trades. sim_gap under the fixed measurement was ~+1bp: the
  sim was honest; the STRATEGY has no live edge over its costs.

**Actions taken (all live on the server):** crypto pipeline stopped + disabled (its exit
flatten worked — BTC closed at 20:32:28Z; book -$130 lifetime, no honest re-scored edge, per
Oli's conditional), 4 crypto crons commented out; stocks `--trade` REMOVED from
run_equities_capture.sh (d518b92) — capture + shadow book + nightly screen continue, the
n=15 tally keeps accruing, real orders require a strategy that clears the bar in backtest
first. EOD-flatten defect found during shutdown: the 20:00:00Z close was missed by 4s and 4
positions (NVDA/SPY/QQQ/UBER) carried into after-hours "accepted" limbo — flattened manually
via extended-hours marketable limits; account fully flat. Telegram flood (~700 msgs/day =
2/round-trip × 356) dies with --trade.

**Model sweep status:** lean screen (2 sessions, 3 phases, PRELIMINARY): ev_tree is
phase-fragile junk (NVDA ±8.72, 14 trades); `adaptive` the only interesting line (NVDA
net/±ph 3.92 on 404 trades — but d-best ≤ 0, so its net may be cadence luck, not skill).
Full 6-session confirm crashed 8h in on a River bug (HoeffdingAdaptiveTree
`_estimate_model_size` ZeroDivision when drift-pruning empties the tree) — patched
(memory_estimate_period pushed out of reach), restarted as 5 crash-proof single-model runs
with incremental output. Results pending; nothing is a finding yet.

**The $100 long-only study (5y daily bars ×10 symbols + 1y 5-min, Alpaca SIP; costs =
half-spread per side from OUR OWN capture medians + SEC fee; PDT modeled):** the real
constraints at $100 — PDT (max 3 day trades/5 days under $25k) and cost-per-trade vs
dollar-edge ((2bp on $100 = $0.002/trade) — make EVERY intraday-ML-style strategy
structurally impossible with real money at this size, independent of whether the edge
exists. What survives, with real n (1,260 days):

| strategy ($100, 5y) | final $ | CAGR | maxDD | t | day-trades/wk | PDT-legal |
|---|---|---|---|---|---|---|
| bh QQQ | $197 | 14.4% | -35.6% | 1.6 | 0 | yes |
| **sma-filter QQQ (50d)** | $184 | 13.0% | **-16.1%** | 2.0 | 0 | yes |
| sma-filter QQQ (200d) | ~$214 | 16.4% | -13.6% | — | 0 | yes |
| overnight-only SPY | $116 | 3.0% | -23.2% | 0.75 | 0 | yes (costs eat it) |
| orb30 QQQ (PDT-capped) | $111 (1y) | 10.8% | -6.5% | 1.2 | ≤3 | yes (thin data) |

Robustness: ALL SMA windows (20/50/100/200) positive on SPY/QQQ/NVDA — not a cherry-picked
window; matches the trend-following literature. Per-year: the filter LAGS in up years and
saved -22pts in 2022 — it is drawdown insurance on beta, NOT alpha, and is stated as such.
Single names whipsaw (sma50 NVDA trailing-12m: $82 vs bh $119) — index only.

**Honest bottom line for tomorrow:** at $100 real money the right strategy is not the ML
system at all — it is long-only index exposure (QQQ) with a once-daily SMA trend filter:
0 day trades, ~4-14 trades/yr, costs in cents, at most ONE quiet Telegram message a day,
and 5 years of daily data behind it instead of 6 sessions. The intraday-ML research
continues as research (capture + shadow book + tally), separated from money.

### 2026-07-22 — MODEL sweep FULL CONFIRM (6 sessions, 8 phases): nothing beats `ev`. The wall is not a model-choice artifact.

Completion of the 07-21 model review ("is the modelling algorithm the problem — can't we
find something better?"). All candidates, same discipline (`scripts/model_sweep.py`,
phase-swept 8, dz4 spread<2bp, 5s, all 6 clean sessions):

| sym | model | net (phase-mean) | ±phase | net/±ph | d-best | trades | sess>0 |
|-----|-------|------------------|--------|---------|--------|--------|--------|
| NVDA | **ev (tracked)** | +2.71 | 4.14 | **0.65** | +0.057 | 145 | 6/6 |
| NVDA | adaptive | +1.13 | 1.56 | 0.73 | −0.007 | 482 | 6/6 |
| NVDA | hoeffding | +1.31 | 1.91 | 0.69 | ~0 | 415 | 5/6 |
| NVDA | meta | +1.79 | 2.71 | 0.66 | ~0 | 224 | 6/6 |
| NVDA | ev_tree | +1.57 | 6.43 | 0.24 | +0.134 | 23 | 5/6 |
| AAPL | **ev (tracked)** | +1.74 | 2.18 | **0.80** | +0.029 | 69 | 6/6 |
| AAPL | hoeffding | +0.89 | 1.52 | 0.59 | ~0 | 208 | 5/6 |
| AAPL | meta | +1.16 | 2.12 | 0.55 | ~0 | 106 | 5/6 |
| AAPL | adaptive | +0.44 | 1.79 | 0.25 | −0.002 | 220 | 5/6 |
| AAPL | ev_tree | +1.34 | 6.81 | 0.20 | +0.088 | 10 | 5/6 |
| (both) | forest, linear | dropped after lean screen: no promise / consistently negative | | | | | |

**Verdict: the tracked `ev` remains the best config on the pre-stated bar (clear net/±ph>1
AND beat ev by a real margin — nothing does either).** Three specific outcomes worth the ink:

1. **`ev_tree` (my 07-21 candidate): refuted.** It wins on the synthetic kink it was
   designed for and loses on real data — ultra-selective (10-23 trades/session vs ev's
   69-145), phase-fragile (±6.4-6.8). Noted, not chased: its d-best is the highest ever
   measured (+0.134 NVDA) — direction WHEN it commits is real, but it commits ~15x too
   rarely for the net to rise above phase noise. A selectivity/coverage trade-off, not a
   better model.
2. **`adaptive`: the lean screen's NVDA 3.92 collapsed to 0.73 on the full run, d-best
   NEGATIVE.** Its positive net is cadence/trade-count structure, not directional skill.
   This is the third time this project's lean→full protocol has caught exactly this
   shrinkage (AAPL 2-session 1.55→0.91→0.80; now this). The protocol is doing its job.
3. `ev`'s full-run numbers reproduce RESEARCH 07-16 to the decimal — deterministic replay
   verified end-to-end again.

**Ops lessons (both self-inflicted, both now guarded):** (a) River's tree
memory-estimation ZeroDivisions when drift-pruning empties a HoeffdingAdaptiveTree —
killed the first 8h run; `memory_estimate_period` pushed out of reach on every tree kind.
(b) Running 5 sweep processes in PARALLEL on the same session DBs made DuckDB spill to
the SAME per-database `.tmp` directory and they corrupted each other's temp files
(hoeffding/meta died on 07-14) — replays of a shared DB must run sequentially, or copy
the DB per process. The incremental `[partial]`-line output added after crash (a) meant
crash (b) lost only one session per model instead of everything.

**Closes the model-algorithm question: the equities wall (net/±ph < 1 on every config)
holds across seven model families and is a property of the signal-vs-cost structure, not
of the learner.** Active levers remaining: none. The pre-registered kill/continue rule and
the capture/shadow-book tally continue unattended; real-money strategy work has moved to
the $100 long-only study (07-21 evening entry).

## 2026-07-26 — the pre-registered n=15 decision point ARRIVED, and it says CONTINUE. The live money test says STOP. Both are true.

Surfaced by accident: an alert-noise cleanup (Oli: "lots of useless alerts") required a dry
run of the nightly digest, which revealed the rolling history had quietly reached **n=15
clean sessions** — the decision point pre-registered on 2026-07-16, before any of this data
existed. The tally, under the amended bar (σ′ = sqrt(σ² + σ_ph²), RESEARCH 07-17):

| | mean nightly phase-mean net | σ (across days) | net/σ | **net/σ′** | green |
|---|---|---|---|---|---|
| NVDA | +2.6 | 1.9 | +1.38 | **+1.15** | **15/15** |
| AAPL | +2.3 | 2.1 | +1.08 | **+1.01** | 14/15 |

**Both tracked names clear the pre-registered bar. By the rule as written, the verdict is
CONTINUE.** NVDA is positive on every one of 15 sessions.

**And the live test contradicts it.** On 2026-07-21 the same config traded 356 real paper
round trips and lost -$47.49 at **47.5% gross direction** — a coin flip — with prediction
magnitude anti-informative. That is one session against fifteen, but it is the only one
measured with real fills instead of a cost model.

**No action taken, and that is deliberate.** The honest reading is that these two results
are not actually in conflict — they measure different things, and the gap between them IS
the finding:
- The backtest tally says the *phase-mean net across a whole session* is reliably positive.
- The live test says that *acting on it, trade by trade, at real fill latency* is not.
- The known mechanism connecting them: entry latency (1-2.4s of a 5s horizon, RESEARCH
  07-21) and the ~1.5bp toll. The tally's cost model charges the spread but cannot charge
  what it does not simulate — the signal decaying while the order is in flight.

So the pre-registered rule has done its job (it stopped the research from being abandoned
on a hunch, and from being deployed on one) and is now superseded by better evidence. **The
rule's CONTINUE applies to RESEARCH, which continues free and unattended (capture + shadow
book + nightly screen). It is not a mandate to trade money, and nothing has been
re-enabled.** Real orders stay off (07-21) until something clears the bar *with real fills*,
which is a different and harder test than the one just passed.

The digest now computes σ′ per name, states the bar, and announces the decision point with
both facts side by side, so this never depends on someone remembering to check. Also fixed
before it ever sent: `capture_line` read `rows` while the capture writes `rows_written`,
which would have put a false "unwritten 19.9M ⚠" on every nightly message — caught by
dry-running the real message instead of trusting it.

## 2026-08-07 — RESOLVED: the edge is real, and it is gone in 500 milliseconds. Intraday track closed.

Two carefully-measured results had been contradicting each other for two weeks: a phase-swept
backtest saying NVDA +2.6bps/session, net/σ′ 1.11, positive on 22 of 23 sessions **and not
decaying as n grew** (the first ratio in this project's history to survive that test), versus
one live session of 356 real fills that lost -$47.49 at 47.5% gross direction. `scripts/
latency_sweep.py` re-prices every entry at the mid actually available L seconds after the
signal — the measured fill latency on this account is 1.0-2.4s — holding the exit at
signal+horizon. L=0 reproduces the standard sim exactly (130 trades, +3.1536bps, verified
before trusting anything else). 6 sessions, 8 phases:

| sym | L=0 | L=0.5s | L=1.0s | L=1.5s | L=2.0s | L=2.4s |
|-----|-----|--------|--------|--------|--------|--------|
| NVDA | **+2.71** (6/6 green) | −1.98 | −1.94 | −1.56 | −1.65 | −1.69 (0/6) |
| AAPL | **+1.74** (6/6 green) | −1.43 | −1.44 | −1.10 | −1.46 | −1.21 (0/6) |

**The entire edge is destroyed by HALF A SECOND of delay, and the damage does not grow after
that.** −4.7bps (NVDA) / −3.2bps (AAPL) arrives by L=0.5s and is then flat out to 2.4s. This
is not a cost that scales with latency; it is a cliff.

**Both measurements were right.** The latency-adjusted backtest lands in the same place as
the live book: modelled NVDA −1.65..−1.94 and AAPL −1.10..−1.46 vs live NVDA −0.81, AAPL
−1.13, all trades −1.53bps. The sim was never flattering the strategy's *direction* — it was
entering at a price that no longer exists by the time an order can reach the exchange.

**Mechanism, stated plainly:** the signal fires on a momentary quote dislocation and the
dislocation reverts within ~500ms. The edge IS the first half-second. Anything slower than
that is trading the aftermath, which is why live direction measured 47.5% — a coin flip —
while the same model scored d-best +0.057 offline. Nothing is wrong with the model.

**This also closes the one remaining hope (a longer horizon to dilute a fixed latency cost),
by arithmetic on two established results rather than a new run:** the entry penalty is a
property of the ENTRY price, essentially independent of how long you then hold, while
RESEARCH 07-15/16 measured 30s-horizon net at only +0.2..+0.4bps with instantaneous fills.
+0.3 − 4.7 is deeply negative. There is no horizon at which this reaches profitability
through a retail broker path.

**Verdict: the intraday equities track is CLOSED — not by a wall, but by an explanation.**
The pre-registered rule's CONTINUE (n=15, and still 1.11 at n=23) was a verdict on a number
that this experiment now shows is unreachable: it was measuring an edge that exists only
inside a latency budget we do not have and cannot buy at this scale. That is a better
outcome than the ambiguity it replaces, and it retires the question honestly rather than
leaving it to accrue sessions forever.

**What the project actually produced:** a methodology that refused six consecutive false
positives (cadence, phase, horizon, spread-gate, cross-sectional, model-family) and then, when
a result finally DID survive every statistical test it could throw at it, went and found the
physical reason it still wouldn't make money. The negative result is trustworthy in both
directions, which was the point.

**Consequent decisions:** real orders stay off (since 07-21). Daily capture and the nightly
screen are now consumerless — the question they fed is answered — and should stop; keeping
them running would be accruing evidence for a decision already made. Real-money work moves
to the $100 long-only QQQ trend filter (RESEARCH 07-21 evening), which depends on none of
this.
