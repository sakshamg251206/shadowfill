# ShadowFill — Research Specification

Status: design frozen for Plan 1. Sections marked *(Plan 3)* and *(Plan 4)* are
not yet implemented and must not be built ahead of schedule.

---

## 1. The problem

Every passive-execution and market-making backtest rests on a fill model: given
that I rest a limit order at price *p*, do I get filled, when, and what is the
price doing at the moment I do? That model is almost never validated, for a
structural reason — **you cannot observe the fills of an order you never sent.**

The literature works around this by fitting survival models to the population of
*real* resting orders visible in L3 data, treating cancellation as censoring or
as a competing risk. Both moves are known to be shaky:

- Cancellation is endogenous. Informed traders cancel ahead of adverse moves;
  market makers cancel to avoid being picked off. Kaplan–Meier's independent-
  censoring assumption fails, and the leading deep-survival papers say so in
  their own limitations sections rather than fixing it.
- Even a correct competing-risks treatment answers "what happened to *their*
  orders under *their* cancellation policies." No desk wants that number. They
  want the fill law of *their* order under *their* policy.

Open-source tooling is worse off than the literature. Queue-aware backtesters
ship a fill model as a user-selectable assumption with no validation target at
all.

## 2. The opening

MBO/L3 data carries an order ID and an arrival sequence per order. Under strict
price–time priority, a hypothetical order placed at price *p* at time *t* has
**every order resting at *p* at time *t* ahead of it, and everything later
behind it.** Maintain one integer, `ahead`:

- cancel/delete of an order ahead → `ahead -= removed_size`
- trade at *p* → eats the front → `consumed = min(size, ahead)`; any residual
  would have hit the shadow → that is a fill
- add at *p* → ignore, it is behind

`ahead` is monotone. The fill time of a never-cancel hypothetical order is
therefore **computed, not modelled**. That is a ground truth for a counterfactual
that was never traded, and it is the benchmark the whole project is built on.

The one assumption: the shadow order does not change other participants'
behaviour. Defensible for small sizes in liquid books; stress-tested explicitly
in Plan 4, not waved away.

## 3. Hypotheses

Pre-registered, falsifiable, sign-agnostic where the sign is genuinely unknown.

Let `F*(h | x)` be the never-cancel ground truth at horizon *h* computed by the
Plan 1 engine, and `F̂_obs` any estimator fitted on the observational population
of real resting orders.

**H1 — the bias exists and is material.**
`E |F̂_obs(h|x) − F*(h|x)|` is large relative to estimator sampling error, at
h ∈ {100 ms, 1 s, 10 s, 60 s}.
*Null:* the gap sits inside a block-bootstrap confidence interval.

**H2 — the sign is regime-dependent, not universal.**
Two mechanisms oppose each other. Traders cancel when the queue is hopeless
(→ censoring-based estimators *overstate* fill probability) and cancel to avoid
being picked off (→ they *understate* it). Prediction: the first dominates in
small-tick/thin-queue regimes, the second in large-tick/deep-queue regimes.
Cheap to falsify, and interesting whichever way it lands.

**H3 — adverse selection partially offsets it.**
Passive edge is `E[PnL] = F(h) · E[m_h | fill]` for markout `m_h`. The
conditional markout estimated observationally is *also* biased, in the opposite
direction. **The headline number is the net error in expected passive edge in
basis points**, not the fill-rate error on its own.

**H4 — the L2 penalty.**
Cancel-position heuristics used by every L2-based simulator (cancel-from-front /
back / uniform / proportional) induce a queue-position error whose effect on
estimated passive edge is of the same order as H1's bias. Tested by ablating
true L3 down to L2 and re-running the identical pipeline.

**H5 — decision relevance.**
Under the corrected estimator, the ranking of a small set of passive execution
policies inverts at a non-trivial rate. **If rankings never invert, the
correction is academically true and practically irrelevant, and the paper says
exactly that.**

## 4. Method *(Plan 3)*

- Non-parametric: Kaplan–Meier (as the naive baseline to be beaten),
  Aalen–Johansen cumulative incidence under competing risks.
- Semi-parametric: cause-specific Cox with time-varying covariates
  (queue ahead, order-flow imbalance, spread, realised volatility, trade
  intensity); Fine–Gray subdistribution hazard.
- Policy re-targeting: IPCW reweighting from the observational cancellation law
  to a specified policy π, with π drawn from a small declared policy class
  (never-cancel, time-based, queue-decay, adverse-OFI, price-move).
- Sensitivity: independent censoring is untestable in general, so report
  Archimedean-copula dependent-censoring bounds plus Peterson worst-case bounds.
  The ground truth then lets us check whether the bounds actually contain the
  truth — a check the survival literature almost never gets to run.
- ML baseline: discrete-time hazard network (DeepHit-style), evaluated with
  IPCW-Brier and time-dependent AUC, then re-evaluated against the ground truth.

## 5. Data

| Source | Role | Notes |
|---|---|---|
| LOBSTER free sample (NASDAQ) | equities regime, adapter validation | message file carries order IDs; orderbook file gives an independent snapshot to validate reconstruction against |
| Self-recorded Coinbase L3 | scale + out-of-venue generalisation | one of the few venues exposing per-order IDs and full lifecycle events |
| Databento MBO (free credit) | second equity regime | optional |
| Synthetic generator (in repo) | CI, and the placebo test | censoring independent by construction |

Three regimes with different tick sizes, which is exactly what H2 needs.
No third-party data is committed to the repository.

## 6. Evaluation

Against the ground truth: calibration error (reliability curves of predicted vs
realised fill), IPCW-Brier, negative log-likelihood.
Decision-relevant: error in expected passive edge (bps), and rank-inversion rate
across the policy class.
Uncertainty: block bootstrap over sessions/days, never over individual orders —
order outcomes within a session are strongly dependent.

## 7. Failure tests *(Plan 4)*

Each of these can kill a claim, which is the point.

- **Placebo.** Synthetic stream where censoring is independent by construction.
  The measured bias *must* vanish. If it does not, the pipeline is broken and no
  result from it means anything.
- **Known-bias injection.** Inject a synthetic informed-canceller agent with a
  known ground-truth bias; the estimator must recover its sign and magnitude.
- **Impact stress.** Sweep shadow-order size and locate where the no-impact
  assumption breaks, by comparison against real orders of similar size at
  similar queue positions.
- **Latency sweep.** Δ from 0 to several milliseconds; report sensitivity rather
  than a single number.
- **Cross-regime generalisation.** Fit on one ticker/venue, test on another.
- **Purged, embargoed cross-validation across days.**
- **Prefix invariance.** Already enforced as a unit test in Plan 1.

## 8. What this project deliberately does not do

- It does not claim a profitable strategy, and no PnL curve appears anywhere.
- It does not model the reaction of other participants to the shadow order.
- It does not attempt cross-asset or cross-venue impact.
- It does not use proprietary or paid-tier data that a reader cannot obtain.

## 9. Plan sequence

1. **Plan 1 — ground-truth engine.** MBO reconstruction, exact queue arithmetic,
   never-cancel outcomes, C++/Python equivalence. *Plan written.*
2. **Plan 2 — multi-venue data layer.** Coinbase L3 recorder with gap detection,
   Databento adapter, Parquet partitioning, dataset manifests.
3. **Plan 3 — estimators and the L2 ablation.**
4. **Plan 4 — experiments, failure tests, and the paper-style README.**
