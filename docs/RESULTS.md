# Results

Every experiment that has been run, with the numbers it produced and the
command that reproduces it. The [README](../README.md#what-was-found) has the
one-table summary; this page has the detail and the reasoning.

**Provenance.** Every number on this page is copied from a committed manifest
under [`results/`](../results), which pins the git commit, the SHA-256 of the
input and the full configuration of the run that produced it. The inputs are the
Nasdaq TotalView-ITCH 5.0 files for 2019-12-30 and 2019-03-27, materialised to
Parquet with `python -m shadowfill.dataset` (see the README, "Using real
data"). Market data is not redistributed, so the outcome tables themselves are
not committed.

**Scope.** The sections H1–H5 and the robustness sections below are the
2019-12-30 session: one symbol (AAPL), except H2, which uses six symbols.
[A second session](#a-second-session--2019-03-27) repeats H1–H5 on 2019-03-27,
and [one section](#across-both-sessions--does-the-correction-transfer) applies a
correction learned on one book to another. Intervals are 95% block-bootstrap
intervals over half-hour blocks *within* a session. **No interval in this
repository covers day-to-day variation**: two sessions cannot support one (see
amendment AG), so across days the repository reports side-by-side point
estimates and says no more than that.

**Known gap.** The engine does not credit a shadow with a fill when an
aggressor trades through its price to a worse one
([docs/IDEAS.md](IDEAS.md)). That can only lower the computed truth, so the
understatements below are, on that account alone, lower bounds. **It has now
been sized and it is not small**: see
[the trade-through gap](#the-trade-through-gap--sized-not-fixed). Every number
on this page is still the engine's current output.

## Contents

- [H1 — Does treating cancellation as censoring bias the fill curve?](#h1--does-treating-cancellation-as-censoring-bias-the-fill-curve)
- [H2 — Does the sign depend on the tick regime?](#h2--does-the-sign-depend-on-the-tick-regime)
- [H3 — The error in expected passive edge](#h3--the-error-in-expected-passive-edge)
- [H4 — What a level-2 feed costs](#h4--what-a-level-2-feed-costs)
- [H5 — Does the correction change the decision?](#h5--does-the-correction-change-the-decision)
- [A second session — 2019-03-27](#a-second-session--2019-03-27)
- [Across both sessions — does the correction transfer?](#across-both-sessions--does-the-correction-transfer)
- [The trade-through gap — sized, not fixed](#the-trade-through-gap--sized-not-fixed)
- [Robustness — is it just latency?](#robustness--is-it-just-latency)
- [Correction — can standard reweighting repair it?](#correction--can-standard-reweighting-repair-it)
- [Validation — the test that nearly killed this](#validation--the-test-that-nearly-killed-this)

## H1 — Does treating cancellation as censoring bias the fill curve?

AAPL, 2019-12-30, the full regular session 09:30–16:00 ET. 1,581,219 events;
791,477 real orders, each shadowed by a hypothetical order that never cancels.
Thirteen half-hour bootstrap blocks.

| horizon | never-cancel truth | Kaplan–Meier | error | 95% CI |
|---|---|---|---|---|
| 100 ms | 0.0172 | 0.0140 | −0.0031 | [−0.0037, −0.0024] |
| 1 s | 0.0506 | 0.0372 | −0.0134 | [−0.0172, −0.0082] |
| 10 s | 0.2059 | 0.1085 | −0.0974 | [−0.1176, −0.0665] |
| 60 s | 0.4331 | 0.1644 | **−0.2688** | [−0.2925, −0.2253] |

A passive order resting at AAPL's touch and never cancelled fills **43%** of
the time within a minute. Kaplan–Meier fitted on the real orders in the same
book says **16%**. Every interval excludes zero.

**Why it understates.** Real orders are cancelled within seconds, so by 60 s
the risk set is almost entirely orders nobody bothered to pull — and nobody
pulls an order that was never going to fill. The survivors are adversely
selected for *not* filling, and the estimator extrapolates their hazard to the
whole population. Treating cancellation as independent censoring assumes the
cancelled orders would have filled at the survivors' rate; here they would have
filled at nearly three times it.

**What this is not.** One symbol, one day. The interval covers within-session
variation only; day-to-day variation needs more than one session and is not
claimed. Reproduce with:

    python -m shadowfill.experiment \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h1-aapl-2019-12-30

Every number is in `results/h1-aapl-2019-12-30/manifest.json` with the git
commit, input hash and full config that produced it.

## H2 — Does the sign depend on the tick regime?

The spec predicted the bias would read high in small-relative-tick, thin-queue
books and low in large-relative-tick, deep ones. The contrast is taken within
one venue and one session, from relative tick size, so the matching rules are
held constant.

Naive Kaplan–Meier error at a 60-second horizon, ordered by regime:

| symbol | price | tick (bps) | F* | KM | error | 95% CI |
|---|---|---|---|---|---|---|
| AAPL | 289.70 | 0.35 | 0.4331 | 0.1644 | −0.2688 | [−0.2922, −0.2236] |
| MSFT | 157.75 | 0.63 | 0.4877 | 0.1332 | −0.3545 | [−0.3769, −0.3103] |
| SAP | 133.33 | 0.75 | 0.0725 | 0.1532 | **+0.0806** | [+0.0678, +0.0961] |
| INTC | 59.66 | 1.68 | 0.4071 | 0.1592 | −0.2479 | [−0.3028, −0.1640] |
| UN | 57.82 | 1.73 | 0.1201 | 0.0406 | −0.0795 | [−0.0948, −0.0584] |
| CSCO | 47.52 | 2.10 | 0.3234 | 0.1012 | −0.2222 | [−0.2682, −0.1337] |

**The sign does flip — but not along the tick axis.** SAP reverses, and its
interval excludes zero comfortably. It sits at 0.75 bps, between MSFT at 0.63
(−0.35) and INTC at 1.68 (−0.25), so the flip is not ordered by relative tick
and the magnitude is not monotone in it either. **H2 as stated is not
supported.**

What separates SAP is not its tick but its book. It and UN are the two
cross-listed names here, and they have by far the lowest ground-truth fill
rates — 0.07 and 0.12 against 0.32–0.49 for the domestically-listed names —
because Nasdaq sees only a slice of their liquidity. SAP is the one case where
a never-cancel order does *worse* than the observational estimate predicts,
which is what a sign flip means. Whether venue fragmentation is the mechanism
is a hypothesis this session cannot test; it is offered as the obvious
candidate, not as a finding.

**Correction.** On a 46-minute window this table showed no flip at all, and
SAP was excluded for having only 8,131 events. The full session gives it
151,819, and it reverses. A negative result on a short window was the wrong
answer, not merely a weaker one.

    python -m shadowfill.regimes --session-date 2019-12-30 \
        --symbols AAPL,MSFT,SAP,INTC,UN,CSCO \
        --out-dir results/h2-regimes-2019-12-30

## H3 — The error in expected passive edge

A fill rate is not a decision. What a desk trades on is

    E[edge] = F(h) x E[m_h | filled]

the chance of being filled times what the fill was worth. Markout is signed so
positive is profit to the passive side, measured against the mid one second
after the fill.

| horizon | true edge | estimated edge | error | 95% CI |
|---|---|---|---|---|
| 100 ms | −0.000 | −0.000 | +0.000 | [−0.000, +0.000] |
| 1 s | −0.005 | −0.004 | +0.001 | [−0.001, +0.003] |
| 10 s | −0.039 | −0.020 | +0.019 | [+0.008, +0.030] |
| 60 s | **−0.087** | **−0.031** | **+0.056** | [+0.033, +0.076] |

All in basis points. Resting passively at AAPL's touch and never cancelling
costs **0.087 bps** to adverse selection over a minute. An estimator fitted on
the observable orders says **0.031 bps** — it understates the cost by a factor
of nearly three, and the interval excludes zero from 10 s onward.

**H3's mechanism does not survive contact with the data.** The spec predicted
the conditional markout would be biased in the opposite direction and partly
offset the fill-rate error. Decomposed at 60 s:

| | F* vs F̂ | m* vs m̂ |
|---|---|---|
| 60 s | 0.4331 vs 0.1644 | −0.200 vs −0.190 |

The fill-rate gap is a factor of 2.6; the markout gap is 5%. The offset the
hypothesis relies on is not there. The whole error in expected edge is the
fill-rate error, priced.

    python -m shadowfill.markout \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h3-edge-aapl-2019-12-30

## H4 — What a level-2 feed costs

Every open-source queue-aware backtester runs on level-2 data, which reports
size per price level and no order identities. When size leaves a level, an L2
simulator cannot know whether it sat ahead of your order or behind it, so it
guesses. The guess has never been validated, because validating it needs
exactly the L3 ground truth computed here.

Same session, same hypothetical orders, one field removed -- the order id on
every cancel, which is precisely what aggregation destroys. With the id gone, a
simulator has to guess how much of each cancel sat ahead of you. The three
standard guesses, each implemented as a queue model in the engine itself:

| horizon | L3 truth | front (optimistic) | proportional (expected) | back (pessimistic) |
|---|---|---|---|---|
| 100 ms | 0.0172 | +0.0032 [+0.0026, +0.0036] | +0.0026 [+0.0022, +0.0029] | −0.0015 [−0.0020, −0.0011] |
| 1 s | 0.0506 | +0.0149 [+0.0123, +0.0165] | +0.0124 [+0.0101, +0.0139] | −0.0062 [−0.0074, −0.0046] |
| 10 s | 0.2059 | +0.0385 [+0.0352, +0.0411] | +0.0338 [+0.0307, +0.0364] | −0.0200 [−0.0215, −0.0185] |
| 60 s | 0.4331 | +0.0333 [+0.0292, +0.0382] | +0.0290 [+0.0251, +0.0339] | −0.0221 [−0.0240, −0.0200] |

Error in fill rate, L2 guess minus L3 truth, with 95% intervals from the same
half-hour blocks for all three.

**The truth sits strictly between the pessimistic guess and the other two, at
every horizon.** The informative column is *proportional* -- cancelled shares
uniform over the level, the expected-value assumption a careful simulator would
make. It still promises **25% more fills than it gets** at one second, with an
interval clear of zero. So real cancellations at AAPL's touch come
disproportionately from *behind* the typical resting order, not uniformly: the
back of the queue is where orders get pulled. The fourth heuristic in the
literature, uniform over *orders*, needs an order count that an L2 feed does
not carry, so on L2 it collapses into proportional and is not offered
separately.

The front-model errors are the same order of magnitude as the cancellation bias
above, which is what H4 predicted. They point in opposite directions, so an L2
backtest that also treats cancellation as censoring gets a partial offset of
its own. That is luck, not correctness, and nothing guarantees it holds in
another regime.

Three properties are proven rather than measured, and tested: per shadow, fills
order front ≥ proportional ≥ back (by induction -- each model removes the most,
middle and least from ahead, and every later operation is monotone in it); the
default front model is bit-identical to the engine's original behaviour, so
nothing committed before the other two existed moved; and the Python oracle and
the C++ engine agree field by field under all three.

    python -m shadowfill.l2 \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h4-l2-aapl-2019-12-30

## H5 — Does the correction change the decision?

A bias can be real, large, and irrelevant. The test is whether acting on the
uncorrected estimate leads anywhere different. The policy class is the choice
an execution desk actually faces — rest passively at the touch for T, cross the
spread if still unfilled.

| wait | F* | edge* (bps) | F̂ | edgê (bps) |
|---|---|---|---|---|
| 100 ms | 0.0172 | −0.5061 | 0.0140 | −0.5077 |
| 1 s | 0.0506 | −0.4938 | 0.0372 | −0.4996 |
| 5 s | 0.1396 | −0.4670 | 0.0830 | −0.4864 |
| 30 s | 0.3382 | −0.4054 | 0.1421 | −0.4683 |
| 60 s | 0.4331 | **−0.3786** | 0.1644 | **−0.4614** |

Crossing immediately costs 0.515 bps. Both methods rank the policies
identically — **0 of 10 pairs invert**, both pick 60 s, and the two disagree in
**0%** of bootstrap replicates.

**H5's claim fails, cleanly.** The spec anticipated this and said to report it:
the correction is, on this evidence, academically true and practically
irrelevant to *which* policy you pick.

What survives is smaller than it looked on a short window. The truth says the
choice between best and worst policy is worth **0.128 bps**; the estimator says
**0.046 bps**, understating the stakes by **2.8×**. A desk would still pick the
right policy and still underrate how much the choice matters.

    python -m shadowfill.policies \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h5-policies-aapl-2019-12-30

## A second session — 2019-03-27

Every table above is one day. This section reruns H1–H5 unchanged on a second
Nasdaq session, 2019-03-27, to see which conclusions repeat. Same code, same
parameters (regular hours, matched placement, 13 half-hour blocks, 200
replicates — 150 for H2 — seed 0, C++ engine); only the input differs. Input:
sha-256 `7997025b9e09dd6c2ecb0bfa48a856197e6e800711ab67367ee0f2ab724b9ba8`,
422,264,305 messages, no unresolved order references on any of the six symbols.

**What this does and does not establish.** It shows whether the *direction* of a
result repeats on a second day. It does not give an interval over days: with two
sessions the spec's session-level bootstrap is degenerate (amendment AG), so
the comparison below is two point estimates side by side and nothing more.
Every interval in this section is, as before, within-session.

### H1 — AAPL, 03-27

2,097,418 events in the window; 1,056,435 real orders, each shadowed.

| horizon | never-cancel truth | Kaplan–Meier | error | 95% CI |
|---|---|---|---|---|
| 100 ms | 0.0189 | 0.0152 | −0.0037 | [−0.0042, −0.0031] |
| 1 s | 0.0611 | 0.0407 | −0.0204 | [−0.0243, −0.0157] |
| 10 s | 0.2466 | 0.0882 | −0.1584 | [−0.1727, −0.1385] |
| 60 s | 0.4909 | 0.1200 | **−0.3709** | [−0.3845, −0.3477] |

Same sign at every horizon, every interval excludes zero. The magnitude is
larger than on 12-30 (−0.2688 at 60 s), and the two days' 60 s intervals do not
overlap. That is a statement about two sessions' within-session variation; it
cannot say whether the difference is noise, regime or something else. AAPL
traded at $188.55 here against $289.70 on 12-30 — a different relative tick,
0.53 bps against 0.35 — and whether that matters is untested.

### H2 — six symbols, 03-27, 60 s

| symbol | price | tick (bps) | F* | KM | error | 95% CI |
|---|---|---|---|---|---|---|
| AAPL | 188.55 | 0.53 | 0.4909 | 0.1200 | −0.3709 | [−0.3845, −0.3472] |
| MSFT | 116.71 | 0.86 | 0.6108 | 0.1360 | −0.4748 | [−0.4910, −0.4508] |
| SAP | 113.27 | 0.88 | 0.0943 | 0.0918 | −0.0026 | [−0.0217, +0.0295] |
| UN | 58.07 | 1.72 | 0.1243 | 0.0441 | −0.0802 | [−0.1015, −0.0654] |
| INTC | 53.18 | 1.88 | 0.5708 | 0.1315 | −0.4394 | [−0.4632, −0.4025] |
| CSCO | 52.98 | 1.89 | 0.5408 | 0.1377 | −0.4031 | [−0.4209, −0.3736] |

**SAP's sign reversal does not reappear.** On 12-30 SAP read +0.0806 with an
interval well clear of zero; here it reads −0.0026 with an interval that
straddles zero, so on this day its sign is not established either way. Five of
six names are negative with intervals excluding zero. SAP and UN again have the
lowest ground-truth fill rates (0.09 and 0.12 against 0.49–0.61), which is
consistent with the cross-listing candidate, but a flip that appears on one of
two symbol-days is not evidence for or against it. H2 as stated — a sign
ordered by tick — is still not supported. The open question about SAP is
narrower now (one reversal, one non-reversal) and not closed.

### H3 — error in expected passive edge, AAPL 03-27, bps

| horizon | true edge | estimated edge | error | 95% CI |
|---|---|---|---|---|
| 100 ms | −0.001 | −0.001 | +0.000 | [−0.000, +0.000] |
| 1 s | −0.010 | −0.006 | +0.003 | [+0.002, +0.005] |
| 10 s | −0.046 | −0.017 | +0.029 | [+0.023, +0.034] |
| 60 s | **−0.096** | **−0.024** | **+0.072** | [+0.063, +0.080] |

Decomposed at 60 s: the fill-rate gap is 0.4909 against 0.1200 (a factor of
4.1) and the markout gap is −0.195 against −0.198 (1.6%). As on 12-30, **the
offsetting markout bias H3 rests on is not there**; the edge error is the
fill-rate error, priced.

### H4 — the level-2 penalty, AAPL 03-27

| horizon | L3 truth | front | proportional | back |
|---|---|---|---|---|
| 100 ms | 0.0189 | +0.0052 [+0.0046, +0.0060] | +0.0042 [+0.0038, +0.0047] | −0.0029 [−0.0034, −0.0025] |
| 1 s | 0.0611 | +0.0254 [+0.0234, +0.0276] | +0.0219 [+0.0203, +0.0235] | −0.0123 [−0.0141, −0.0110] |
| 10 s | 0.2466 | +0.0472 [+0.0423, +0.0526] | +0.0432 [+0.0386, +0.0483] | −0.0287 [−0.0321, −0.0256] |
| 60 s | 0.4909 | +0.0327 [+0.0285, +0.0377] | +0.0300 [+0.0261, +0.0349] | −0.0259 [−0.0291, −0.0227] |

The same ordering as 12-30 at every horizon: the truth lies between the
pessimistic guess and the other two. The proportional guess promises **36% more
fills than it gets** at one second (0.0219 on 0.0611), against 25% on 12-30.

### H5 — decision relevance, AAPL 03-27

| wait | F* | edge* (bps) | F̂ | edgê (bps) |
|---|---|---|---|---|
| 100 ms | 0.0189 | −0.5213 | 0.0152 | −0.5232 |
| 1 s | 0.0611 | −0.5077 | 0.0407 | −0.5153 |
| 5 s | 0.1704 | −0.4715 | 0.0737 | −0.5051 |
| 30 s | 0.3943 | −0.3970 | 0.1089 | −0.4943 |
| 60 s | 0.4909 | **−0.3660** | 0.1200 | **−0.4907** |

Crossing immediately costs 0.531 bps. **Inversions 0 / 10 pairs**; both methods
pick 60 s; they disagree in **0.0%** of bootstrap replicates. H5's claim fails
again, cleanly. The truth puts the choice between best and worst policy at
0.155 bps and the estimator at 0.033 bps — understated **4.8×** (from the
four-decimal table values; 2.8× on 12-30).

### Side by side: AAPL at 60 s, two sessions

| | 12-30 | 03-27 |
|---|---|---|
| H1 error in the fill rate | −0.2688 | −0.3709 |
| H3 error in expected edge (bps) | +0.056 | +0.072 |
| H4 proportional guess at 1 s, excess fills | +25% | +36% |
| H5 stakes understated by | 2.8× | 4.8× |
| H5 inversions | 0 / 10 | 0 / 10 |

Every qualitative conclusion — the sign of the H1 bias, the absence of an H3
offset, the H4 ordering, the absence of H5 inversions — is the same on both
days. The magnitudes are not, and every magnitude here is larger on 03-27. Two
days cannot say how far the magnitudes vary in general.

    python -m shadowfill.experiment \
        --message-path data/parquet/date=2019-03-27/symbol=AAPL/events.parquet \
        --out-dir results/h1-aapl-2019-03-27

The H2–H5 commands are the 12-30 ones with `2019-03-27` substituted; every
number is in the matching `results/*-2019-03-27/manifest.json`.

## Across both sessions — does the correction transfer?

A desk that believed H1 would not recompute ground truth for every name and day
it trades. It would calibrate once and carry the correction over. This section
measures whether that works: a per-(horizon, queue-ahead stratum) factor
`R = F* / KM`, learned on one book and applied to another's own stratified
Kaplan–Meier. The correction and the baseline were fixed before any real data
was looked at (amendment AD, `shadowfill/transfer.py`).

Twelve books — six symbols on each of two days. The baseline is the target's
*stratified* Kaplan–Meier, so calibration is the only thing that differs
between the corrected and uncorrected estimates; its error therefore differs
slightly from the unstratified H1/H2 figures above. 528 (source, target,
horizon) pair cases and 12 leave-one-out targets per horizon.

**Pair cases are point estimates.** Leave-one-out rows carry intervals from the
within-session time-of-day block bootstrap, with each target corrected by the
order-weighted mixture of the other eleven books — which includes the same
symbol on the other day, so leave-one-out is not a pure cross-symbol test.

| absolute error cut, share of cases | 100 ms | 1 s | 10 s | 60 s |
|---|---|---|---|---|
| same symbol, other day (of 12) | 8 | 9 | 11 | 10 |
| other symbol, either day (of 120) | 74 | 73 | 72 | 79 |
| leave-one-out target (of 12) | 8 | 7 | 10 | 9 |

Overall **64%** (336 / 528) of pair cases move closer to the truth. Same-symbol
cross-day pairs at 60 s:

| source → target | truth | KM (strat.) | transferred | error: KM | error: transferred |
|---|---|---|---|---|---|
| AAPL 12-30 → 03-27 | 0.4907 | 0.1402 | 0.3271 | −0.3505 | −0.1637 |
| MSFT 12-30 → 03-27 | 0.6115 | 0.1406 | 0.5153 | −0.4709 | −0.0962 |
| SAP 12-30 → 03-27 | 0.0943 | 0.0889 | 0.0464 | −0.0054 | −0.0479 |
| INTC 12-30 → 03-27 | 0.5719 | 0.1325 | 0.3324 | −0.4394 | −0.2395 |
| UN 12-30 → 03-27 | 0.1244 | 0.0416 | 0.1300 | −0.0828 | +0.0056 |
| CSCO 12-30 → 03-27 | 0.5416 | 0.1374 | 0.4357 | −0.4042 | −0.1059 |
| AAPL 03-27 → 12-30 | 0.4321 | 0.1932 | 0.6319 | −0.2389 | +0.1998 |
| MSFT 03-27 → 12-30 | 0.4883 | 0.1463 | 0.5789 | −0.3421 | +0.0906 |
| SAP 03-27 → 12-30 | 0.0725 | 0.1521 | 0.1425 | +0.0796 | +0.0699 |
| INTC 03-27 → 12-30 | 0.4078 | 0.1626 | 0.6834 | −0.2452 | +0.2756 |
| UN 03-27 → 12-30 | 0.1204 | 0.0388 | 0.1154 | −0.0816 | −0.0050 |
| CSCO 03-27 → 12-30 | 0.3249 | 0.1028 | 0.4014 | −0.2222 | +0.0765 |

**What this shows.**

- A calibration learned elsewhere cuts the absolute error in roughly two cases
  in three, so the bias is partly a stable property of the book and not pure
  noise.
- **It is not a fix.** The factor is not stable across days: carried from 12-30
  to 03-27 it under-corrects (the errors stay negative), and carried the other
  way it over-corrects and the sign flips (AAPL +0.20, INTC +0.28). That is
  what the larger 03-27 bias in H1 would predict, and it means a smaller
  absolute error is not the same as an unbiased estimate.
- **SAP is the failure case.** Its pooled-calibration error grows on both days
  (absolute error up by 0.21 on 12-30 and 0.14 on 03-27, both intervals
  excluding zero); calibration learned on high-fill names overshoots a book
  whose fill rate is 0.07–0.09. Leave-one-out at 60 s: eight targets improve
  with an interval excluding zero, two (both SAP) worsen with one, and two
  (INTC and CSCO on 12-30) have intervals that include zero.

What it does not show: whether a richer calibration would transfer better, and
whether the day-to-day instability is a property of these two days or of the
approach. Both would need more sessions.

    python -m shadowfill.transfer \
        --book AAPL@2019-12-30=data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --book AAPL@2019-03-27=data/parquet/date=2019-03-27/symbol=AAPL/events.parquet \
        ...                                  # all twelve, --seed 0 --n-replicates 200 \
        --out-dir results/transfer-2019-12-30-2019-03-27

## The trade-through gap — sized, not fixed

Both engines credit a shadow with an execution only at its own price. When the
real orders at that price are pulled and an aggressor then executes beyond it
-- a sell hitting bids below a shadow bid -- the shadow was the best-priced
order on its side and would have been filled first. `shadowfill.tradethrough`
measures this from the engine's existing outcome columns, without changing
either engine, as the earlier of the engine's first fill and the first
`EXECUTE`/`EXECUTE_HIDDEN` on the shadow's side at a strictly worse price inside
its live window.

Two bounds. **Lower** credits only executions after the shadow's queue-ahead
had reached zero, so nothing real was ahead of it at its price and any fixed
engine must fill it. **All trade-throughs** credits every one. They agree to
within 0.0003 everywhere, so the distinction does not matter here.

Never-cancel F* at 60 s, matched placement, regular hours:

| book | engine F* | trade-throughs credited | change | shadows with an earlier fill | KM | KM error, engine → credited |
|---|---|---|---|---|---|---|
| AAPL 12-30 | 0.4331 | 0.5530 | **+0.120** | 179,857 of 791,477 | 0.1644 | −0.269 → −0.389 |
| AAPL 03-27 | 0.4909 | 0.5947 | **+0.104** | 240,080 of 1,056,435 | 0.1200 | −0.371 → −0.475 |
| SAP 12-30 | 0.0725 | 0.0988 | +0.026 | 698 of 26,059 | 0.1532 | +0.081 → +0.054 |
| SAP 03-27 | 0.0943 | 0.1835 | +0.089 | 11,184 of 111,987 | 0.0918 | −0.003 → −0.092 |

The "credited" column is the lower bound; the shadow counts are lower-bound
counts. KM errors use the committed Kaplan–Meier estimates and carry no
interval.

**Checked against the book, not just the arithmetic.** For 300 randomly drawn
AAPL 12-30 shadows credited by the lower bound, the oracle's book was replayed
to the credited event: in all 300 the real book held nothing at the shadow's
price and the best real price on its side was already worse. These are real
missed fills, not a windowing artefact.

**What it changes.** The H1 understatement gets larger on AAPL, by about 45%
on 12-30. SAP's 12-30 reversal shrinks from +0.081 to +0.054 without changing
sign, and on 03-27 SAP becomes clearly negative. H3–H5 are not recomputed: the
missed fills are exactly the picked-off ones, so their markouts are probably
adverse and the edge figures could move in either direction. Only a rerun can
say.

**Still not the whole gap.** While a shadow bid sits alone above the visible
best bid, a new *sell order* at or below its price would also have hit it.
Neither the engine nor this measurement credits that, so even with every
trade-through credited, F* is a lower bound.

    python -m shadowfill.tradethrough \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/tradethrough-aapl-2019-12-30

## Robustness — is it just latency?

Every real participant is slower than a hypothetical order that appears at the
exact instant it is wanted. The latency sweep reruns the H1 comparison with the
shadow submitted Δ after the real order it twins. Same session, same blocks:

| latency | error at 1 s | 95% CI | error at 60 s | 95% CI |
|---|---|---|---|---|
| 0 | −0.0134 | [−0.0172, −0.0082] | −0.2688 | [−0.2925, −0.2253] |
| 1 ns | +0.0035 | [+0.0004, +0.0077] | −0.2570 | [−0.2816, −0.2125] |
| 100 µs | +0.0055 | [+0.0025, +0.0098] | −0.2561 | [−0.2806, −0.2116] |
| 1 ms | +0.0080 | [+0.0052, +0.0119] | −0.2550 | [−0.2795, −0.2107] |
| 5 ms | +0.0104 | [+0.0076, +0.0139] | −0.2538 | [−0.2782, −0.2096] |
| 20 ms | +0.0125 | [+0.0096, +0.0159] | −0.2527 | [−0.2770, −0.2085] |

**The headline survives.** At 60 s the bias shrinks by about 6% across 0–20 ms
and every interval stays far from zero.

**The short-horizon bias changes sign.** At one second, any latency of at least
1 ns turns an understatement into an overstatement, with intervals clear of
zero. Read the step from 0 to 1 ns as a change of *question*, not of speed. At
zero latency the shadow takes its real twin's exact queue slot, so it asks
"would *this* order have filled had it not cancelled" -- the question survival
estimators claim to answer, and the one H1 reports. From 1 ns on, the twin
reaches the book first and the shadow sits behind it by exactly the twin's own
size (checked per shadow: 100% of 65,949 across two seeds). That is the
position a real participant is actually in. For such a participant, Kaplan–Meier
fitted on real orders **over**-predicts fills at one second and
**under**-predicts them by a quarter of the population at a minute.

One property is proven and tested rather than measured: at every instant both
are live, a later shadow has at least as much queue ahead as its earlier twin,
so it never fills first. Two properties that look similar are *not* theorems,
and an earlier version of the tests wrongly asserted them: mean queue-ahead at
insertion need not rise with latency (on AAPL it falls from 776.7 to 774.8
between 1 ms and 5 ms), and the true fill rate need not fall.

    python -m shadowfill.latency \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/latency-aapl-2019-12-30

## Correction — can standard reweighting repair it?

If traders cancel only on things the data shows, the bias is fixable:
reweight each observed fill by the inverse probability that its order was still
uncancelled when it filled (IPCW), with that probability modelled on the
observables. Two censoring models, both refitted inside every bootstrap
replicate:

- **Queue at arrival** -- Kaplan–Meier of cancellation within strata of
  queue-ahead at arrival. With this model IPCW is algebraically the size-weighted
  average of per-stratum Kaplan–Meier curves (Satten & Datta 2001), which the
  tests check to 1e-12. That check caught a real bug: tie handling that left a
  `d·c/Y²` error at every time a fill and a cancel coincided.
- **Queue now, plus age** -- a piecewise-constant cancellation hazard over cells
  of *current* queue-ahead × order age, the maximum-likelihood estimate per cell.
  Current queue position comes free from the engine: a matched shadow sits in
  its twin's slot, so its queue-ahead *is* the real order's, and because it is
  monotone, four first-passage times describe the whole trajectory.

| horizon | truth | Kaplan–Meier | IPCW, queue at arrival | IPCW, queue now + age | share removed |
|---|---|---|---|---|---|
| 100 ms | 0.0172 | −0.0031 | −0.0027 | −0.0029 [−0.0035, −0.0022] | 6% |
| 1 s | 0.0506 | −0.0134 | −0.0102 | −0.0100 [−0.0143, −0.0047] | 26% |
| 10 s | 0.2059 | −0.0974 | −0.0850 | −0.0905 [−0.1119, −0.0572] | 7% |
| 60 s | 0.4331 | −0.2688 | −0.2399 | −0.2615 [−0.2818, −0.2187] | 3% |

**Standard reweighting on queue position and order age does not repair the
bias.** At a minute it removes 3%, and every interval still excludes zero.

**What this does not show** -- stated because it is the obvious over-reading:
that the bias is driven by information the data cannot contain. The same
estimator was run on synthetic streams with a *known*, observable cancellation
mechanism injected (next section), and it recovered only 17–74% of that, too.
The injected canceller responds to a joint state -- the front of the queue *at
the best price* -- and queue bucket cannot tell the front of the touch from the
front of a level five ticks deep. The covariate that could, "is my level
currently the touch", is not monotone, so it cannot be summarised by passage
times. A residual gap after reweighting is therefore a lower bound on what the
correction misses, not a measurement of hidden information.

    python -m shadowfill.ipcw \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/ipcw-aapl-2019-12-30

## Validation — the test that nearly killed this

On the synthetic stream, cancellation is uniform over live orders and therefore
independent of fill prospects by construction. Kaplan–Meier is *correct* there,
so ShadowFill must measure no bias. The first design measured **+0.26**.

The fault was the comparison, not the arithmetic. Shadows were being placed on
a time grid, so they sat behind a median queue of 259 shares while real orders
at the touch sat behind 42.5 — six times shallower. Queue position dominates
fill probability, so the difference in *populations* was being reported as the
cancellation bias.

The fix is what the project is named for: each shadow now shadows a real order,
at that order's own time, price, side and size. The populations are identical
by construction and the only difference is that the shadow never cancels. The
placebo then reads −0.0000, −0.0002 and −0.0015 at 100 ms, 1 s and 10 s.

It runs on every commit:

    make placebo

If it fails, nothing else in this repository means anything, and any result
measured before it passed is withdrawn — as the earlier grid-based numbers were.

### The other half: a bias put there on purpose

A pipeline that always reported zero would pass the placebo too. So the second
failure test injects a cancellation mechanism whose direction is known by
construction, and requires the measurement to find it. The synthetic
generator's `informed_cancel` knob makes cancels target the front of the best
level -- picked-off avoidance, which removes fill-prone orders from the risk
set and must make Kaplan–Meier *understate*.

Error at 5 s on a 60,000-event stream (seed 101):

| injection strength | 0 | +0.3 | +0.6 | +0.9 |
|---|---|---|---|---|
| error | −0.0035 | −0.0117 | −0.0270 | −0.0450 |

Recovered with the right sign at every horizon, monotone in strength, clear of
the noise floor, and reproduced on three seeds. Making the injection inert fails
two of the four tests, so they bite.

**The half that did not work.** The spec's H2 names an opposing mechanism --
cancelling *hopeless* orders at the back of the queue -- and predicts it flips
the sign. On this generator it does not: it produces a negative error of much
the same shape. The test pins that negative result instead of asserting the
prediction. A confound is known -- the injection removes depth from the book as
well as orders from the risk set -- so it is not evidence against H2 on real
data; `docs/PLAN-AMENDMENTS.md` §Y has the full account.

    make placebo injection
