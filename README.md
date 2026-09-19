# ShadowFill

Counterfactual fill estimation from market-by-order data.

## What this repository currently contains

A validated ground-truth engine: given an MBO event stream and a schedule of
hypothetical passive orders, it computes — by exact queue arithmetic, not by a
model — whether each order would have filled, when, and how much queue sat ahead
of it throughout its life.

On top of it, the pipeline that turns that into a measurement: a
TotalView-ITCH adapter, Parquet materialisation, extraction of the real orders
that rested in the same book, and a comparison of the never-cancel truth
against the estimators a passive backtest relies on, with block-bootstrap
intervals.

## The first measurement

AAPL, 2019-12-30, 09:30–10:07 ET. 346,841 events; 180,934 real orders, each
shadowed by a hypothetical order that never cancels.

| horizon | never-cancel truth | Kaplan–Meier | error | 95% CI |
|---|---|---|---|---|
| 100 ms | 0.0190 | 0.0147 | −0.0043 | [−0.0053, −0.0028] |
| 1 s | 0.0606 | 0.0392 | −0.0215 | [−0.0244, −0.0167] |
| 10 s | 0.2268 | 0.0789 | −0.1480 | [−0.1557, −0.1230] |
| 60 s | 0.4203 | 0.1027 | **−0.3176** | [−0.3259, −0.2616] |

A passive order resting at AAPL's touch and never cancelled fills **42%** of
the time within a minute. Kaplan–Meier fitted on the real orders in the same
book says **10%**. Every interval excludes zero.

**Why it understates.** Real orders are cancelled within seconds, so by 60 s
the risk set is almost entirely orders nobody bothered to pull — and nobody
pulls an order that was never going to fill. The survivors are adversely
selected for *not* filling, and the estimator extrapolates their hazard to the
whole population. Treating cancellation as independent censoring assumes the
cancelled orders would have filled at the survivors' rate; here they would have
filled at four times it.

Note the sign. The research spec's leading story was that traders cancel
hopeless queues, which would make censoring-based estimators read *high*. In
this window the opposite mechanism dominates. That is H2's "the sign is
regime-dependent, not universal" showing up unprompted, and it is one window,
not a finding.

**What this is not.** Thirty-seven minutes, one symbol, one day, starting at
the opening auction — the least representative window of the session. Eight
bootstrap blocks is thin, and the interval covers within-session variation
only. Reproduce it with:

    python -m shadowfill.experiment \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h1-aapl-2019-12-30 --block-ns 300000000000

Every number above is in `results/h1-aapl-2019-12-30/manifest.json` with the
git commit, input hash and full config that produced it.

## What a level-2 feed costs (H4)

Every open-source queue-aware backtester runs on level-2 data, which reports
size per price level and no order identities. When size leaves a level, an L2
simulator cannot know whether it sat ahead of your order or behind it, so it
guesses. The guess has never been validated, because validating it needs
exactly the L3 ground truth computed here.

Same session, same hypothetical orders, one field removed — the order id on
every cancel, which is precisely what aggregation destroys:

| horizon | L3 truth | L2 cancel-from-front | error | 95% CI |
|---|---|---|---|---|
| 100 ms | 0.0190 | 0.0230 | +0.0040 | [+0.0031, +0.0050] |
| 1 s | 0.0606 | 0.0798 | +0.0192 | [+0.0151, +0.0237] |
| 10 s | 0.2268 | 0.2611 | +0.0343 | [+0.0273, +0.0416] |
| 60 s | 0.4203 | 0.4449 | +0.0246 | [+0.0188, +0.0306] |

At one second the L2 simulator promises **32% more fills than it gets**. That
is the same order of magnitude as the cancellation bias above — which is what
H4 predicted, and it is measured rather than argued.

The two errors point in opposite directions, so an L2 backtest that also treats
cancellation as censoring gets a partial cancellation of its own. That is luck,
not correctness, and nothing guarantees it holds in another regime.

**Scope.** Only cancel-from-front is implemented, because it is what the
engine's unknown-order path already does. It is the most *optimistic* of the
four standard heuristics, so this is a one-sided bound: a simulator that
assumes the best is wrong by at least this much. A test pins the direction —
the L2 curve can never sit below the truth.

    python -m shadowfill.l2 \
        --message-path data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
        --out-dir results/h4-l2-aapl-2019-12-30 --block-ns 300000000000

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
        --grid-ns 50000000 --size 5 --horizon-ns 10000000000 \
        --engine python --block-ns 2000000000

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
ITCH path checks instead is internal consistency — the book never crosses, all
1,569 executions resolve to a live order, no removal exceeds the shares
resting — measured, but necessary rather than sufficient. Level aggregation
below the best price is not checked against an outside observer at all. The
free LOBSTER sample is no longer published, so this is a limitation of what is
obtainable, not a choice; `docs/PLAN-AMENDMENTS.md` §V.1 records it in full.

**The observational population is matched, not identical.** Shadow orders are
placed at the touch on a fixed clock; real orders arrive at all depths when
their senders choose. The comparison restricts real orders to the touch and
stratifies on queue-ahead, which is the dominant determinant of a fill and is
measured identically on both sides. Size and arrival timing remain
uncontrolled, and the unconditional row is reported beside the strata so the
size of the composition effect is visible rather than assumed away.

**Uncertainty is currently within-session only.** The bootstrap resamples
contiguous blocks of time within one session. Day-to-day variation needs more
than one session and is not covered by the intervals reported.

**Nothing here is a trading strategy.** This is a measurement instrument. It
reports whether a hypothetical passive order would have filled and when. It
never claims PnL.
