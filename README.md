# ShadowFill

**How wrong are passive-fill backtests? Measured against a ground truth that is
computed, not modelled.**

Every market-making and passive-execution backtest rests on a fill model that
nobody validates, because you cannot observe the fills of an order you never
sent. Market-by-order data breaks that deadlock. It carries an id per order, so
for a hypothetical order you know exactly which real orders were ahead of it and
can watch each one trade or cancel. The fill time of a hypothetical order that
never cancels is then a matter of arithmetic on one integer -- the queue ahead
of it -- and that gives a ground truth to measure every fill estimator against.

This is a measurement instrument. It proposes no strategy and claims no PnL.

## What was found

AAPL on Nasdaq, 2019-12-30, regular hours: 1,581,219 events, 791,477 real
orders, each shadowed by a hypothetical order that never cancels. 95% intervals
from a block bootstrap over half-hour blocks *within* that session.

| question | answer | verdict |
|---|---|---|
| Does treating cancellation as censoring bias the fill curve? (H1) | Kaplan–Meier says 16% of passive orders fill within a minute; the truth is **43%** | **yes**, every interval excludes zero |
| Does the sign depend on tick regime? (H2) | the sign flips for SAP, but not along the tick axis | **not supported** |
| Does adverse selection offset it? (H3) | the offset is absent; edge error is 0.056 bps at 60 s, all of it from the fill rate | **not supported** |
| What does an L2 feed cost? (H4) | even the expected-value L2 model promises **25%** more fills than it gets at 1 s; the three standard heuristics bracket the truth | **yes** |
| Does correcting the bias change the decision? (H5) | policy rankings do not invert (0 of 10 pairs) | **not supported** |
| Is it an artefact of assuming zero latency? | at 60 s the bias is −0.269 at 0 ms and −0.253 at 20 ms; at 1 s it flips sign | **headline robust** |
| Does standard reweighting (IPCW) fix it? | on queue position and order age, it removes 3% at 60 s | **no** |

Three of the five pre-registered hypotheses came back negative, and they are
reported as such. The effects they were meant to explain are large.

**Two failure tests gate all of it.** On a synthetic stream whose cancellation
is independent by construction, the measured bias must vanish -- and it does,
after it caught and killed an earlier design. And on a stream with a biased
canceller injected on purpose, the measurement must find the bias with the
right sign -- which it does for one mechanism, and not for the other.

**What this is not.** One symbol and one day for most of it (six symbols for
H2). The intervals cover variation within that session, not between sessions.
The shadow order is assumed not to change anyone else's behaviour, and that is
not yet stress-tested. Every limitation is listed at the end.

## What this repository contains

A validated ground-truth engine: given an MBO event stream and a schedule of
hypothetical passive orders, it computes -- by exact queue arithmetic, not by a
model -- whether each order would have filled, when, and how much queue sat
ahead of it throughout its life. It exists twice, as a readable Python oracle
and a C++20 engine held to byte-identical output on synthetic and real data.

On top of it, the pipeline that turns that into a measurement: a
TotalView-ITCH adapter, Parquet materialisation, extraction of the real orders
that rested in the same book, the estimators a passive backtest relies on and
the corrections a careful one would apply, each with block-bootstrap intervals
and a manifest pinning the commit, input hash and configuration that produced
every number.

## The first measurement

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

## Does the correction change the decision? (H5)

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

## Does the sign depend on the tick regime? (H2)

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

## The headline: error in expected passive edge (H3)

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

## What a level-2 feed costs (H4)

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

## Is it just latency?

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

## Can a standard correction repair it?

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

## The test that nearly killed this

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

## Quick start

    make install
    make test          # Python suite, runs with no external data
    make test-cpp      # C++ suite
    make bench         # throughput gate
    python -m shadowfill.ground_truth \
        --message-path tests/fixtures/synthetic_mbo_v1.csv \
        --out-dir results/synthetic

The full measurement, on the same fixture:

    python -m shadowfill.experiment \
        --message-path tests/fixtures/synthetic_mbo_v1.csv \
        --out-dir results/h1-synthetic \
        --horizon-ns 10000000000 --block-ns 2000000000 --no-window

The fixture spans about 200 seconds, so read only its short horizons. At 60 s
the truth curve has nothing left to observe and flattens, and the rows show
large "errors" on a stream whose cancellation is independent by construction --
they measure the window, not the estimator. The placebo test uses horizons up
to 5 s for exactly this reason.

## Using real data

Nasdaq publishes complete TotalView-ITCH 5.0 trading days with no account, no
key and no charge, and the server honours HTTP range requests, so a prefix is
enough to validate the adapter without pulling 3.5 GB:

    ./scripts/fetch_itch_sample.sh 20
    pytest -m needs_itch -v

ITCH is the raw feed LOBSTER is itself derived from. Its `Order Executed`
message names the resting order reference that was consumed, which is what lets
queue position be computed rather than inferred; on a 20 MB sample, 1,569 of
1,569 executions resolved to a live order. The LOBSTER path still works if you
have a sample:

    ./scripts/fetch_lobster_sample.sh data/lobster
    SHADOWFILL_LOBSTER_DIR=data/lobster pytest -m needs_lobster -v

Neither vendor's files are redistributed here, and CI touches neither.

A day is parsed once and materialised, so nothing downstream re-reads 3.5 GB
of gzip:

    python -m shadowfill.dataset \
        --itch-path data/itch/12302019.NASDAQ_ITCH50.gz \
        --symbols AAPL,MSFT --out-root data/parquet

    python -m shadowfill.experiment \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h1-aapl

## What the output means

`outcomes.parquet` holds one row per hypothetical order. Five terminal states:

| status | meaning |
|---|---|
| `FILLED` | the queue ahead was consumed and the order's full size traded |
| `EXPIRED` | still resting when its horizon elapsed |
| `TRUNCATED` | still resting, inside its horizon, when the data ran out |
| `NOT_ACTIVATED` | its effective timestamp fell past the last event |
| `RESTING` | in-flight only; never written |

Each row also records the first time the order's queue-ahead fell below 1000,
100, 10 and 1 shares (`ahead_lt_1000_ts` … `ahead_lt_1_ts`): its insert time if
it started below, `-1` if it never got that close to the front. Queue-ahead is
monotone, so these four times are its whole trajectory at that resolution; the
time-varying IPCW model is built on them.

`TRUNCATED` and `NOT_ACTIVATED` are kept apart deliberately. A truncated order
existed and we stopped watching, which is a censored observation. A
not-activated one never existed, and must not enter a fill-rate denominator.
Every numeric column of a `NOT_ACTIVATED` row is written as a Parquet null
rather than a sentinel, because a null propagates or raises downstream where a
`-1` silently regresses as a covariate. `first_fill_ts` and `full_fill_ts` do
stay `-1` on activated orders that never filled: that is a real observation
about an order that existed, not a missing one.

Three diagnostics go in every manifest:

- `unknown_order_events` — events referencing ids added before the recording
  window. Expected to be non-zero on real data, and not suppressed.
- `unknown_order_assumed_ahead` — times a queue was decremented on the
  unverifiable assumption that an absent id was resting ahead of the shadow.
  `assumed_ahead_events` reports the same thing per row, so "drop every order
  whose outcome leaned on an assumption and rerun" is a one-line filter.
- `fifo_violations` — executions that hit an order while one that arrived
  earlier was still resting at the same price and side. Zero on the synthetic
  fixture by construction; on real data a non-zero count means hidden
  liquidity, an order type this reconstruction does not model, or a gap in it.
  It is a statement about the data, never about a shadow order.

## Design notes

- Two implementations, one behaviour. `shadowfill.replay` is the readable
  oracle; `shadowfill._core` is the fast path. `tests/python/test_cpp_equivalence.py`
  holds them to identical output field by field.
- Hidden executions never consume the visible queue.
- Cancellations are resolved as ahead-or-behind by arrival sequence, which is
  only possible because MBO data carries order ids.
- Shadow accounting runs before the book applies each event, so cancelled
  orders can still be looked up.
- Every run writes `manifest.json` pinning the git SHA, input SHA-256 and config.

## Assumptions, stated plainly

**The shadow order does not change anyone else's behaviour.** Nothing here
models the market reacting to an order that was never sent. That is defensible
at small sizes and is stress-tested in a later stage of the project; it is not
free, and it is the assumption most likely to matter.

**Orders whose ids predate the recording window are assumed to be ahead.** They
cannot be resolved either way. The count is reported per run and per row, so
the sensitivity of any result to the assumption is measurable rather than
hypothetical.

**A tie goes against the shadow.** An order arriving at exactly the shadow's
effective timestamp is treated as ahead of it. Sequence numbers decide priority
and a hypothetical order has none, so the pessimistic reading is the honest
one.

**The synthetic fixture is a model, not a market.** Executions cross to the
best price and consume the oldest order resting there, so it respects
price-time priority — but adds outpace cancels, so the book deepens
monotonically through a long run and the process is non-stationary. It exists
so CI never depends on third-party data and so the censoring mechanism is known
by construction. It is not evidence about real markets, and no claim in this
repository rests on it.

**Book reconstruction from ITCH is validated less strongly than from
LOBSTER.** LOBSTER ships an orderbook file alongside its messages — a third
party's reconstruction of the same session — so the book could be checked row
by row against one this project did not build. Raw ITCH has no such file, and
cannot: ITCH is the input that file is derived from, so any book built from it
is our own reconstruction and comparing it to itself proves nothing. What the
ITCH path checks instead is internal consistency — the book never crosses, every
order reference resolves on each of the six materialised symbols (0 unresolved,
from a parse of all 268,744,780 messages in the 2019-12-30 file), no removal exceeds the shares resting — measured, but
necessary rather than sufficient. Level aggregation
below the best price is not checked against an outside observer at all. The
free LOBSTER sample is no longer published, so this is a limitation of what is
obtainable, not a choice; `docs/PLAN-AMENDMENTS.md` §V.1 records it in full.

**Matched placement makes the populations identical, up to one known gap.**
Each shadow is placed on a real order, at that order's own time, price, side and
size, so the truth and the estimators describe the same orders and differ only
in that the shadow never cancels. (An earlier design placed shadows on a clock
and compared them with real orders at the touch; the placebo rejected it, and
every number it produced is withdrawn -- see "The test that nearly killed
this".) The known gap: events sharing a timestamp can activate a shadow on a
neighbouring event, so about 10% of shadow/order pairs start with slightly
different queue-ahead. A test pins the exact-match share above 90%.

**Uncertainty is currently within-session only.** The bootstrap resamples
contiguous blocks of time within one session. Day-to-day variation needs more
than one session and is not covered by the intervals reported.

**Nothing here is a trading strategy.** This is a measurement instrument. It
reports whether a hypothetical passive order would have filled and when. It
never claims PnL.
