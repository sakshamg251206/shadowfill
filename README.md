# ShadowFill

Counterfactual fill estimation from market-by-order data.

## What this repository currently contains

A validated ground-truth engine: given an MBO event stream and a schedule of
hypothetical passive orders, it computes — by exact queue arithmetic, not by a
model — whether each order would have filled, when, and how much queue sat ahead
of it throughout its life.

## Quick start

    make install
    make test          # Python suite, runs with no external data
    make test-cpp      # C++ suite
    make bench         # throughput gate
    python -m shadowfill.ground_truth \
        --message-path tests/fixtures/synthetic_mbo_v1.csv \
        --out-dir results/synthetic

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

**Nothing here is a trading strategy.** This is a measurement instrument. It
reports whether a hypothetical passive order would have filled and when. It
never claims PnL.
