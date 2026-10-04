# Results

Every experiment that has been run, with the numbers it produced and the
command that reproduces it. The [README](../README.md#what-was-found) has the
one-table summary; this page has the detail and the reasoning.

**Provenance.** Every number on this page is copied from a committed manifest
under [`results/`](../results), which pins the git commit, the SHA-256 of the
input and the full configuration of the run that produced it. Every result
manifest was regenerated at commit `0ae5196` from a clean tree. The inputs are
the Nasdaq TotalView-ITCH 5.0 files for 2019-12-30 and 2019-03-27, materialised
to Parquet with `python -m shadowfill.dataset` (see the README, "Using real
data"). Market data is not redistributed, so the outcome tables themselves are
not committed.

**Scope.** The sections H1–H5 and the robustness sections are the 2019-12-30
session: one symbol (AAPL), except H2, which uses six symbols.
[A second session](#a-second-session--2019-03-27) repeats H1–H5 on 2019-03-27,
and [one section](#across-both-sessions--does-the-correction-transfer) applies a
correction learned on one book to another. Intervals are 95% block-bootstrap
intervals over half-hour blocks *within* a session. **No interval in this
repository covers day-to-day variation**: two sessions cannot support one (see
amendment AG), so across days the repository reports side-by-side point
estimates and says no more than that.

**The engine changed on 2026-10-04, and the numbers changed with it.** Until
then both engines missed a class of fills: a shadow left alone at a better
price than the one an aggressor traded through was never filled. That was
measured (+0.12 on AAPL's 60 s truth), fixed, and every experiment rerun.
[What the fix changed](#what-crediting-trade-throughs-changed) lists every
conclusion that moved. One related route is still not credited, so the truth
remains a lower bound on the stated assumptions; see
[the trade-through section](#trade-throughs--measured-then-fixed).

## Contents

- [H1 — Does treating cancellation as censoring bias the fill curve?](#h1--does-treating-cancellation-as-censoring-bias-the-fill-curve)
- [H2 — Does the sign depend on the tick regime?](#h2--does-the-sign-depend-on-the-tick-regime)
- [H3 — The error in expected passive edge](#h3--the-error-in-expected-passive-edge)
- [H4 — What a level-2 feed costs](#h4--what-a-level-2-feed-costs)
- [H5 — Does the correction change the decision?](#h5--does-the-correction-change-the-decision)
- [A second session — 2019-03-27](#a-second-session--2019-03-27)
- [Across both sessions — does the correction transfer?](#across-both-sessions--does-the-correction-transfer)
- [Trade-throughs — measured, then fixed](#trade-throughs--measured-then-fixed)
- [What crediting trade-throughs changed](#what-crediting-trade-throughs-changed)
- [Robustness — is it just latency?](#robustness--is-it-just-latency)
- [Correction — can standard reweighting repair it?](#correction--can-standard-reweighting-repair-it)
- [Validation — the test that nearly killed this](#validation--the-test-that-nearly-killed-this)

## H1 — Does treating cancellation as censoring bias the fill curve?

AAPL, 2019-12-30, the full regular session 09:30–16:00 ET. 1,581,219 events;
791,477 real orders, each shadowed by a hypothetical order that never cancels.
Thirteen half-hour bootstrap blocks.

| horizon | never-cancel truth | Kaplan–Meier | error | 95% CI |
|---|---|---|---|---|
| 100 ms | 0.0207 | 0.0140 | −0.0067 | [−0.0081, −0.0049] |
| 1 s | 0.0760 | 0.0372 | −0.0387 | [−0.0494, −0.0244] |
| 10 s | 0.3204 | 0.1085 | −0.2119 | [−0.2437, −0.1659] |
| 60 s | 0.5531 | 0.1644 | **−0.3887** | [−0.4183, −0.3371] |

A passive order resting at AAPL's touch and never cancelled fills **55%** of
the time within a minute. Kaplan–Meier fitted on the real orders in the same
book says **16%**. Every interval excludes zero.

**Why it understates.** Real orders are cancelled within seconds, so by 60 s
the risk set is almost entirely orders nobody bothered to pull — and nobody
pulls an order that was never going to fill. The survivors are adversely
selected for *not* filling, and the estimator extrapolates their hazard to the
whole population. Treating cancellation as independent censoring assumes the
cancelled orders would have filled at the survivors' rate; here the
never-cancel population fills at 3.4 times it.

A large part of that difference is orders pulled just before the market moved
through their price. A never-cancel order is filled when that happens — see
[trade-throughs](#trade-throughs--measured-then-fixed) — and the real order,
having been pulled, is not. That is the "cancel to avoid being picked off"
mechanism the spec names, observed directly.

**What this is not.** One symbol, one day here; the second day is
[below](#a-second-session--2019-03-27). The interval covers within-session
variation only. Reproduce with:

    python -m shadowfill.experiment \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h1-aapl-2019-12-30

## H2 — Does the sign depend on the tick regime?

The spec predicted the bias would read high in small-relative-tick, thin-queue
books and low in large-relative-tick, deep ones. The contrast is taken within
one venue and one session, from relative tick size, so the matching rules are
held constant.

Naive Kaplan–Meier error at a 60-second horizon, ordered by regime:

| symbol | price | tick (bps) | F* | KM | error | 95% CI |
|---|---|---|---|---|---|---|
| AAPL | 289.70 | 0.35 | 0.5531 | 0.1644 | −0.3887 | [−0.4169, −0.3378] |
| MSFT | 157.75 | 0.63 | 0.5404 | 0.1332 | −0.4073 | [−0.4368, −0.3564] |
| SAP | 133.33 | 0.75 | 0.0988 | 0.1532 | **+0.0544** | [+0.0424, +0.0702] |
| INTC | 59.66 | 1.68 | 0.4288 | 0.1592 | −0.2696 | [−0.3298, −0.1786] |
| UN | 57.82 | 1.73 | 0.1733 | 0.0406 | −0.1327 | [−0.1520, −0.0944] |
| CSCO | 47.52 | 2.10 | 0.3436 | 0.1012 | −0.2425 | [−0.2931, −0.1490] |

**The sign does flip — but not along the tick axis.** SAP reverses, and its
interval excludes zero. It sits at 0.75 bps, between MSFT at 0.63 (−0.41) and
INTC at 1.68 (−0.27), so the flip is not ordered by relative tick and the
magnitude is not monotone in it either. **H2 as stated is not supported.**

What separates SAP is not its tick but its book. It and UN are the two
cross-listed names here, and they have by far the lowest ground-truth fill
rates — 0.10 and 0.17 against 0.34–0.55 for the domestically-listed names —
because Nasdaq sees only a slice of their liquidity. Whether venue
fragmentation is the mechanism is a hypothesis this session cannot test. On
the second day SAP does **not** reverse
([below](#h2--six-symbols-03-27-60-s)), so the reversal is a property of one
symbol-day so far, not of SAP.

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
| 100 ms | −0.002 | −0.000 | +0.002 | [+0.000, +0.004] |
| 1 s | −0.022 | −0.004 | +0.018 | [+0.006, +0.028] |
| 10 s | −0.137 | −0.020 | +0.117 | [+0.066, +0.161] |
| 60 s | **−0.230** | **−0.031** | **+0.199** | [+0.126, +0.260] |

All in basis points. Resting passively at AAPL's touch and never cancelling
costs **0.230 bps** to adverse selection over a minute. An estimator fitted on
the observable orders says **0.031 bps** — it understates the cost more than
sevenfold, and the interval excludes zero at every horizon.

**H3's mechanism is not supported — the markout bias exists, but it compounds
the error instead of offsetting it.** The spec predicted the conditional
markout would be biased in the direction that partly offsets the fill-rate
error. Decomposed:

| horizon | F* vs F̂ | m* vs m̂ (bps) |
|---|---|---|
| 1 s | 0.0760 vs 0.0372 | −0.289 vs −0.107 |
| 10 s | 0.3204 vs 0.1085 | −0.427 vs −0.184 |
| 60 s | 0.5531 vs 0.1644 | −0.415 vs −0.190 |

At 60 s the fill rate is understated by a factor of 3.4 and the adverse
selection in each fill by a factor of 2.2. Both push the estimated edge the
same way. The fills the observational estimator never sees are disproportionately
the ones where the market moved through the order — the worst fills there are.

**Correction.** Before trade-throughs were credited, this table read +0.056
bps at 60 s with a markout gap of 5% (−0.200 vs −0.190), and this page said
"the whole error in expected edge is the fill-rate error, priced". That was
wrong: the missing fills were exactly the adversely selected ones.

    python -m shadowfill.markout \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h3-edge-aapl-2019-12-30

## H4 — What a level-2 feed costs

Every open-source queue-aware backtester runs on level-2 data, which reports
size per price level and no order identities. When size leaves a level, an L2
simulator cannot know whether it sat ahead of your order or behind it, so it
guesses. The guess has never been validated, because validating it needs
exactly the L3 ground truth computed here.

Same session, same hypothetical orders, one field removed — the order id on
every cancel, which is precisely what aggregation destroys. With the id gone, a
simulator has to guess how much of each cancel sat ahead of you. The three
standard guesses, each implemented as a queue model in the engine itself:

| horizon | L3 truth | front (optimistic) | proportional (expected) | back (pessimistic) |
|---|---|---|---|---|
| 100 ms | 0.0207 | +0.0021 [+0.0017, +0.0024] | +0.0016 [+0.0013, +0.0018] | −0.0010 [−0.0014, −0.0008] |
| 1 s | 0.0760 | +0.0081 [+0.0073, +0.0091] | +0.0063 [+0.0056, +0.0068] | −0.0034 [−0.0042, −0.0028] |
| 10 s | 0.3204 | +0.0096 [+0.0081, +0.0114] | +0.0075 [+0.0062, +0.0090] | −0.0054 [−0.0061, −0.0047] |
| 60 s | 0.5531 | +0.0053 [+0.0041, +0.0068] | +0.0041 [+0.0032, +0.0052] | −0.0038 [−0.0044, −0.0033] |

Error in fill rate, L2 guess minus L3 truth, with 95% intervals from the same
half-hour blocks for all three.

**The truth sits strictly between the pessimistic guess and the other two, at
every horizon.** The informative column is *proportional* — cancelled shares
uniform over the level, the expected-value assumption a careful simulator would
make. It promises **8% more fills than it gets** at one second, with an
interval clear of zero. So real cancellations at AAPL's touch come
disproportionately from *behind* the typical resting order. The fourth
heuristic in the literature, uniform over *orders*, needs an order count that
an L2 feed does not carry, so on L2 it collapses into proportional and is not
offered separately.

**H4's claim is not supported.** The spec predicted the L2 penalty would be the
same order of magnitude as the cancellation bias. It is real — every interval
excludes zero — but it is an order of magnitude smaller: at 1 s the front model
is off by 0.008 against an H1 bias of 0.039, and at 60 s by 0.005 against 0.389.
The reason is mechanical: trade-through fills depend on price, not on queue
position, so an L2 simulator gets them right too, and they are most of what
the cancellation bias is made of at long horizons.

**Correction.** Before trade-throughs were credited, the front-model error at
1 s (0.0149) was about the same size as the H1 bias (0.0134), and H4 was marked
supported. Both numbers were distorted by the same missing fills.

Three properties are proven rather than measured, and tested: per shadow, fills
order front ≥ proportional ≥ back (by induction — each model removes the most,
middle and least from ahead, and every later operation is monotone in it); the
default front model is bit-identical to the engine's behaviour without an L2
ablation; and the Python oracle and the C++ engine agree field by field under
all three.

    python -m shadowfill.l2 \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h4-l2-aapl-2019-12-30

## H5 — Does the correction change the decision?

A bias can be real, large, and irrelevant. The test is whether acting on the
uncorrected estimate leads anywhere different. The policy class is the choice
an execution desk actually faces — rest passively at the touch for T, cross the
spread if still unfilled.

| wait | F* | m* (bps) | edge* (bps) | F̂ | m̂ (bps) | edgê (bps) |
|---|---|---|---|---|---|---|
| 100 ms | 0.0207 | −0.105 | −0.5062 | 0.0140 | −0.012 | −0.5077 |
| 1 s | 0.0760 | −0.289 | −0.4976 | 0.0372 | −0.107 | −0.4996 |
| 5 s | 0.2256 | −0.407 | −0.4904 | 0.0830 | −0.173 | −0.4864 |
| 30 s | 0.4691 | −0.418 | −0.4691 | 0.1421 | −0.188 | −0.4683 |
| 60 s | 0.5531 | −0.415 | **−0.4598** | 0.1644 | −0.190 | **−0.4614** |

Crossing immediately costs 0.515 bps. Both methods rank the policies
identically — **0 of 10 pairs invert** and both pick 60 s. The two disagree on
the best policy in **15%** of bootstrap replicates.

**H5's claim fails.** The spec anticipated this and said to report it: the
correction is, on this evidence, academically true and practically irrelevant
to *which* policy you pick.

The reason is worth stating. The truth fills far more often (0.55 against
0.16) *and* each fill is far worse (−0.415 against −0.190 bps). The two errors
nearly cancel in edge, so the estimator gets the edge of each policy, and the
gap between best and worst (0.046 bps by both), approximately right — for the
wrong reasons. A desk using it would pick the right policy while misjudging
both how often it fills and how badly.

**Correction.** Before trade-throughs were credited, this section said the
estimator "understates the stakes by 2.8×". With the missing fills restored
the stakes agree, and that claim is withdrawn.

    python -m shadowfill.policies \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h5-policies-aapl-2019-12-30

## A second session — 2019-03-27

This section reruns H1–H5 unchanged on a second Nasdaq session, 2019-03-27, to
see which conclusions repeat. Same code, same parameters (regular hours,
matched placement, 13 half-hour blocks, 200 replicates — 150 for H2 — seed 0,
C++ engine); only the input differs. Input: sha-256
`7997025b9e09dd6c2ecb0bfa48a856197e6e800711ab67367ee0f2ab724b9ba8`,
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
| 100 ms | 0.0213 | 0.0152 | −0.0062 | [−0.0073, −0.0049] |
| 1 s | 0.0876 | 0.0407 | −0.0469 | [−0.0552, −0.0363] |
| 10 s | 0.3624 | 0.0882 | −0.2742 | [−0.2912, −0.2497] |
| 60 s | 0.5947 | 0.1200 | **−0.4747** | [−0.4818, −0.4533] |

Same sign at every horizon, every interval excludes zero. The magnitude is
larger than on 12-30 (−0.3887 at 60 s), and the two days' 60 s intervals do not
overlap. That is a statement about two sessions' within-session variation; it
cannot say whether the difference is noise, regime or something else. AAPL
traded at $188.55 here against $289.70 on 12-30 — a different relative tick,
0.53 bps against 0.35 — and whether that matters is untested.

### H2 — six symbols, 03-27, 60 s

| symbol | price | tick (bps) | F* | KM | error | 95% CI |
|---|---|---|---|---|---|---|
| AAPL | 188.55 | 0.53 | 0.5947 | 0.1200 | −0.4747 | [−0.4821, −0.4528] |
| MSFT | 116.71 | 0.86 | 0.6613 | 0.1360 | −0.5253 | [−0.5373, −0.5013] |
| SAP | 113.27 | 0.88 | 0.1835 | 0.0918 | −0.0918 | [−0.1314, −0.0259] |
| UN | 58.07 | 1.72 | 0.1821 | 0.0441 | −0.1380 | [−0.1607, −0.1178] |
| INTC | 53.18 | 1.88 | 0.5963 | 0.1315 | −0.4649 | [−0.4909, −0.4249] |
| CSCO | 52.98 | 1.89 | 0.5608 | 0.1377 | −0.4231 | [−0.4420, −0.3891] |

**Every name is negative, SAP included, and every interval excludes zero.** On
12-30 SAP read +0.0544; here it reads −0.0918. So SAP's sign differs between
the two days with both intervals clear of zero. SAP and UN again have the
lowest ground-truth fill rates (0.18 against 0.56–0.66), which keeps the
cross-listing candidate alive as an explanation of their *magnitude*, but the
reversal itself is one symbol-day in two. H2 as stated — a sign ordered by tick
— is still not supported.

### H3 — error in expected passive edge, AAPL 03-27, bps

| horizon | true edge | estimated edge | error | 95% CI |
|---|---|---|---|---|
| 100 ms | −0.002 | −0.001 | +0.002 | [+0.001, +0.003] |
| 1 s | −0.031 | −0.006 | +0.024 | [+0.016, +0.033] |
| 10 s | −0.175 | −0.017 | +0.158 | [+0.128, +0.178] |
| 60 s | **−0.278** | **−0.024** | **+0.255** | [+0.217, +0.278] |

Decomposed at 60 s: fill rate 0.5947 against 0.1200 (a factor of 5.0),
markout −0.468 against −0.198 bps (a factor of 2.4). As on 12-30, the markout
bias compounds the fill-rate error.

### H4 — the level-2 penalty, AAPL 03-27

| horizon | L3 truth | front | proportional | back |
|---|---|---|---|---|
| 100 ms | 0.0213 | +0.0044 [+0.0039, +0.0052] | +0.0035 [+0.0032, +0.0038] | −0.0024 [−0.0029, −0.0021] |
| 1 s | 0.0876 | +0.0170 [+0.0151, +0.0193] | +0.0140 [+0.0126, +0.0157] | −0.0082 [−0.0097, −0.0073] |
| 10 s | 0.3624 | +0.0154 [+0.0126, +0.0189] | +0.0133 [+0.0110, +0.0165] | −0.0090 [−0.0106, −0.0076] |
| 60 s | 0.5947 | +0.0069 [+0.0055, +0.0090] | +0.0061 [+0.0048, +0.0080] | −0.0050 [−0.0062, −0.0041] |

The same ordering as 12-30 at every horizon. Proportional promises **16% more
fills than it gets** at one second, against 8% on 12-30. Again an order of
magnitude below the H1 bias at 60 s.

### H5 — decision relevance, AAPL 03-27

| wait | F* | m* (bps) | edge* (bps) | F̂ | m̂ (bps) | edgê (bps) |
|---|---|---|---|---|---|---|
| 100 ms | 0.0213 | −0.111 | −0.5216 | 0.0152 | −0.045 | −0.5232 |
| 1 s | 0.0876 | −0.352 | −0.5149 | 0.0407 | −0.155 | −0.5153 |
| 5 s | 0.2623 | −0.469 | −0.5144 | 0.0737 | −0.184 | −0.5051 |
| 30 s | 0.5150 | −0.481 | −0.5052 | 0.1089 | −0.197 | −0.4943 |
| 60 s | 0.5947 | −0.468 | **−0.4935** | 0.1200 | −0.198 | **−0.4907** |

Crossing immediately costs 0.531 bps. **Inversions 0 / 10 pairs**; both methods
pick 60 s; they disagree in **6%** of bootstrap replicates. The truth puts the
gap between best and worst policy at 0.028 bps and the estimator at 0.033 — the
estimator now slightly *over*states the stakes.

### Side by side: AAPL at 60 s, two sessions

| | 12-30 | 03-27 |
|---|---|---|
| H1 error in the fill rate | −0.3887 | −0.4747 |
| H3 error in expected edge (bps) | +0.199 | +0.255 |
| H3 markout, truth vs estimate (bps) | −0.415 vs −0.190 | −0.468 vs −0.198 |
| H4 proportional guess at 1 s, excess fills | +8% | +16% |
| H5 inversions | 0 / 10 | 0 / 10 |
| H5 stakes, truth vs estimate (bps) | 0.046 vs 0.046 | 0.028 vs 0.033 |

Every qualitative conclusion — the sign of the H1 bias, the compounding markout
bias in H3, the H4 bracket, the absence of H5 inversions — is the same on both
days. The magnitudes are not, and the H1, H3 and H4 magnitudes are all larger on
03-27. Two days cannot say how far the magnitudes vary in general.

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

| absolute error cut, count of cases | 100 ms | 1 s | 10 s | 60 s |
|---|---|---|---|---|
| same symbol, other day (of 12) | 9 | 11 | 10 | 9 |
| other symbol, either day (of 120) | 77 | 79 | 77 | 86 |
| leave-one-out target (of 12) | 8 | 7 | 10 | 10 |

Overall **68%** (358 / 528) of pair cases move closer to the truth. Same-symbol
cross-day pairs at 60 s:

| source → target | truth | KM (strat.) | transferred | error: KM | error: transferred |
|---|---|---|---|---|---|
| AAPL 12-30 → 03-27 | 0.5946 | 0.1402 | 0.4172 | −0.4544 | −0.1774 |
| MSFT 12-30 → 03-27 | 0.6620 | 0.1406 | 0.5683 | −0.5214 | −0.0937 |
| SAP 12-30 → 03-27 | 0.1835 | 0.0889 | 0.0599 | −0.0946 | −0.1236 |
| INTC 12-30 → 03-27 | 0.5975 | 0.1325 | 0.3496 | −0.4650 | −0.2479 |
| UN 12-30 → 03-27 | 0.1822 | 0.0416 | 0.1869 | −0.1407 | +0.0047 |
| CSCO 12-30 → 03-27 | 0.5616 | 0.1374 | 0.4629 | −0.4243 | −0.0987 |
| AAPL 03-27 → 12-30 | 0.5518 | 0.1932 | 0.7659 | −0.3585 | +0.2141 |
| MSFT 03-27 → 12-30 | 0.5412 | 0.1463 | 0.6274 | −0.3949 | +0.0863 |
| SAP 03-27 → 12-30 | 0.0988 | 0.1521 | 0.2743 | +0.0533 | +0.1756 |
| INTC 03-27 → 12-30 | 0.4295 | 0.1626 | 0.7148 | −0.2670 | +0.2852 |
| UN 03-27 → 12-30 | 0.1737 | 0.0388 | 0.1740 | −0.1349 | +0.0003 |
| CSCO 03-27 → 12-30 | 0.3453 | 0.1028 | 0.4163 | −0.2425 | +0.0710 |

**What this shows.**

- A calibration learned elsewhere cuts the absolute error in about two cases in
  three, so the bias is partly a stable property of the book and not pure
  noise.
- **It is not a fix.** The factor is not stable across days: carried from 12-30
  to 03-27 it under-corrects (the errors stay negative), and carried the other
  way it over-corrects and the sign flips (AAPL +0.21, INTC +0.29). That is
  what the larger 03-27 bias would predict, and it means a smaller absolute
  error is not the same as an unbiased estimate.
- **SAP is the failure case.** Both same-symbol transfers make it worse, and on
  12-30 the pooled calibration raises its absolute error by 0.27 with an
  interval excluding zero; calibration learned on high-fill names overshoots a
  book whose fill rate is 0.10. Leave-one-out at 60 s: eight targets improve
  with an interval excluding zero, two improve with an interval that includes
  it (CSCO 12-30, SAP 03-27), INTC 12-30 worsens with an interval including
  zero, and SAP 12-30 worsens with one excluding it.

What it does not show: whether a richer calibration would transfer better, and
whether the day-to-day instability is a property of these two days or of the
approach. Both would need more sessions.

    python -m shadowfill.transfer \
        --book AAPL@2019-12-30=data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --book AAPL@2019-03-27=data/parquet/date=2019-03-27/symbol=AAPL/events.parquet \
        ...                                  # all twelve, --seed 0 --n-replicates 200 \
        --out-dir results/transfer-2019-12-30-2019-03-27

## Trade-throughs — measured, then fixed

Until 2026-10-04 both engines credited a shadow with an execution only at its
own price. When the real orders at that price are pulled and an aggressor then
executes beyond it — a sell hitting bids below a shadow bid — the shadow was the
best-priced order on its side and, under price priority, would have been filled
first. The engines missed every such fill.

**Measured first.** `shadowfill.tradethrough` reads the outcome columns the
engines produce and finds, per shadow, the first `EXECUTE` or `EXECUTE_HIDDEN`
on its side at a strictly worse price inside its live window. Run against the
old engine (manifests `results/tradethrough-*-20??-??-??`, commit `0e2f0b8`):

| book | old engine F* (60 s) | trade-throughs credited | change | shadows with an earlier fill |
|---|---|---|---|---|
| AAPL 12-30 | 0.4331 | 0.5530 | +0.120 | 179,857 of 791,477 |
| AAPL 03-27 | 0.4909 | 0.5947 | +0.104 | 240,080 of 1,056,435 |
| SAP 12-30 | 0.0725 | 0.0988 | +0.026 | 698 of 26,059 |
| SAP 03-27 | 0.0943 | 0.1835 | +0.089 | 11,184 of 111,987 |

For 300 randomly drawn AAPL 12-30 credited shadows, the oracle's book was
replayed to the credited event; in all 300 the real book held nothing at the
shadow's price and the best real price on its side was already worse.

**Then fixed, in both engines.** An execution on a shadow's side at a strictly
worse price credits `min(size, remaining)` to it, but only once its queue-ahead
is 0. With real orders still ahead of it at its own price, the data says the
book itself was traded through — an anomaly — and the shadow is not flattered
with a fill they did not get. `ahead` is never touched, so a hidden execution
still consumes no visible queue.

**And checked against the measurement.** The fixed engine's 60 s F* is 0.5531,
0.5947, 0.0988 and 0.1835 on the four books above — the values the measurement
predicted from the old engine's output — and the same measurement run on the
new engine's output credits **zero** further shadows on all four
(`results/tradethrough-*-after-fix`). The engines agree byte for byte on real
ITCH data (a 15-minute AAPL slice, 55,804 shadows) as well as on the synthetic
suite.

**Still not the whole gap.** While a shadow bid sits alone above the visible
best bid, a new *sell order* arriving at or below its price would also have hit
it. That route is neither credited nor measured. Crediting it means treating
the incoming order as executing rather than resting, which leans on the
no-impact assumption harder. Until it is measured, F* is a lower bound on what
the stated assumptions imply.

    python -m shadowfill.tradethrough \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/tradethrough-aapl-2019-12-30-after-fix

## What crediting trade-throughs changed

Kaplan–Meier, Aalen–Johansen and every engine diagnostic are unchanged: the fix
touches only the never-cancel truth. Everything measured against the truth
moved. AAPL, 2019-12-30, 60 s unless stated:

| | before | after | consequence |
|---|---|---|---|
| H1 truth / error | 0.4331 / −0.2688 | 0.5531 / −0.3887 | bias ~45% larger; verdict unchanged |
| H2 SAP 12-30 / 03-27 | +0.0806 / −0.0026 | +0.0544 / −0.0918 | SAP's sign now differs across days with both intervals clear of zero |
| H3 edge error | +0.056 bps | +0.199 bps | **the "markout is unbiased" finding is retracted**; the markout bias compounds |
| H4 front error at 1 s vs H1 bias at 1 s | 0.0149 vs 0.0134 | 0.0081 vs 0.0387 | **H4 moves from supported to not supported** |
| H5 stakes, truth vs estimate | 0.128 vs 0.046 | 0.046 vs 0.046 | **"understated 2.8×" is retracted**; rankings still never invert |
| Latency, sign flip at short horizon | at 1 s | at 100 ms | the 1 s flip was an artefact of the missing fills |
| IPCW share removed | 3% | 2% | verdict unchanged |
| Transfer, pairs improved | 64% | 68% | verdict unchanged |

Three of the five hypotheses are now negative for a different reason than
before, and one (H4) has changed verdict. The headline — that treating
cancellation as censoring badly understates a passive order's fill rate — is
stronger.

## Robustness — is it just latency?

Every real participant is slower than a hypothetical order that appears at the
exact instant it is wanted. The latency sweep reruns the H1 comparison with the
shadow submitted Δ after the real order it twins. Same session, same blocks:

| latency | error at 100 ms | 95% CI | error at 1 s | 95% CI | error at 60 s | 95% CI |
|---|---|---|---|---|---|---|
| 0 | −0.0067 | [−0.0081, −0.0049] | −0.0387 | [−0.0494, −0.0244] | −0.3887 | [−0.4183, −0.3371] |
| 1 ns | −0.0005 | [−0.0018, +0.0011] | −0.0294 | [−0.0403, −0.0150] | −0.3870 | [−0.4167, −0.3352] |
| 100 µs | +0.0004 | [−0.0008, +0.0021] | −0.0283 | [−0.0391, −0.0140] | −0.3869 | [−0.4165, −0.3351] |
| 1 ms | +0.0016 | [+0.0005, +0.0034] | −0.0270 | [−0.0377, −0.0128] | −0.3867 | [−0.4163, −0.3349] |
| 5 ms | +0.0031 | [+0.0018, +0.0054] | −0.0256 | [−0.0363, −0.0117] | −0.3864 | [−0.4161, −0.3347] |
| 20 ms | +0.0052 | [+0.0036, +0.0078] | −0.0242 | [−0.0350, −0.0098] | −0.3861 | [−0.4158, −0.3343] |

**The headline survives.** At 60 s the bias moves by less than 1% across
0–20 ms and every interval stays far from zero. At 1 s it shrinks by about a
third and stays clearly negative.

**The shortest horizon changes sign.** At 100 ms, from 1 ms of latency up, an
understatement becomes a small overstatement with intervals clear of zero. Read
the step from 0 to 1 ns as a change of *question*, not of speed. At zero
latency the shadow takes its real twin's exact queue slot, so it asks "would
*this* order have filled had it not cancelled" — the question survival
estimators claim to answer, and the one H1 reports. From 1 ns on, the twin
reaches the book first and the shadow sits behind it by exactly the twin's own
size (checked per shadow: 100% of 65,949 across two seeds). That is the
position a real participant is actually in. For such a participant, Kaplan–Meier
fitted on real orders slightly **over**-predicts fills at 100 ms and
**under**-predicts them by 0.39 at a minute.

**Correction.** Before trade-throughs were credited, the sign flip appeared at
1 s rather than 100 ms. It was an artefact of the missing fills.

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

- **Queue at arrival** — Kaplan–Meier of cancellation within strata of
  queue-ahead at arrival. With this model IPCW is algebraically the size-weighted
  average of per-stratum Kaplan–Meier curves (Satten & Datta 2001), which the
  tests check to 1e-12. That check caught a real bug: tie handling that left a
  `d·c/Y²` error at every time a fill and a cancel coincided.
- **Queue now, plus age** — a piecewise-constant cancellation hazard over cells
  of *current* queue-ahead × order age, the maximum-likelihood estimate per cell.
  Current queue position comes free from the engine: a matched shadow sits in
  its twin's slot, so its queue-ahead *is* the real order's, and because it is
  monotone, four first-passage times describe the whole trajectory.

| horizon | truth | Kaplan–Meier | IPCW, queue at arrival | IPCW, queue now + age | share removed |
|---|---|---|---|---|---|
| 100 ms | 0.0207 | −0.0067 | −0.0063 | −0.0065 [−0.0079, −0.0047] | 3% |
| 1 s | 0.0760 | −0.0387 | −0.0355 | −0.0353 [−0.0466, −0.0209] | 9% |
| 10 s | 0.3204 | −0.2119 | −0.1995 | −0.2050 [−0.2391, −0.1552] | 3% |
| 60 s | 0.5531 | −0.3887 | −0.3599 | −0.3814 [−0.4097, −0.3291] | 2% |

**Standard reweighting on queue position and order age does not repair the
bias.** At a minute it removes 2%, and every interval still excludes zero.

**What this does not show** — stated because it is the obvious over-reading:
that the bias is driven by information the data cannot contain. The same
estimator was run on synthetic streams with a *known*, observable cancellation
mechanism injected (next section), and it recovered only 13–26% of that, too
(strengths 0.3–0.9, 1 s and 5 s; amendment AI). The injected canceller responds
to a joint state — the front of the queue *at the best price* — and queue
bucket cannot tell the front of the touch from the front of a level five ticks
deep. The covariate that could, "is my level currently the touch", is not
monotone, so it cannot be summarised by passage times. A residual gap after
reweighting is therefore a lower bound on what the correction misses, not a
measurement of hidden information.

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
placebo reads −0.0001, −0.0007 and −0.0037 at 100 ms, 1 s and 5 s, against a
tolerance of 0.02.

It runs on every commit:

    make placebo

If it fails, nothing else in this repository means anything, and any result
measured before it passed is withdrawn — as the earlier grid-based numbers were.

### The other half: a bias put there on purpose

A pipeline that always reported zero would pass the placebo too. So the second
failure test injects a cancellation mechanism whose direction is known by
construction, and requires the measurement to find it. The synthetic
generator's `informed_cancel` knob makes cancels target the front of the best
level — picked-off avoidance, which removes fill-prone orders from the risk
set and must make Kaplan–Meier *understate*.

Error at 5 s on a 60,000-event stream (seed 101):

| injection strength | 0 | +0.3 | +0.6 | +0.9 |
|---|---|---|---|---|
| error | −0.0037 | −0.0125 | −0.0284 | −0.0474 |

Recovered with the right sign at every horizon, monotone in strength, clear of
the noise floor, and reproduced on three seeds. Making the injection inert fails
two of the four tests, so they bite.

**The half that did not work.** The spec's H2 names an opposing mechanism —
cancelling *hopeless* orders at the back of the queue — and predicts it flips
the sign. On this generator it does not: it produces a negative error of much
the same shape. The test pins that negative result instead of asserting the
prediction. A confound is known — the injection removes depth from the book as
well as orders from the risk set — so it is not evidence against H2 on real
data; `docs/PLAN-AMENDMENTS.md` §Y has the full account.

    make placebo injection
