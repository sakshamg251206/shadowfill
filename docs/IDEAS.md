# Ideas and open engineering items

Things noticed while working that are outside the current plan. Each entry says
what it is, why it matters, and what doing it would cost. Nothing here is built.

## Trade-through fills are not credited to a shadow

**What.** The engine matches an execution against a shadow only when the two
share a price and side. If the shadow is the best-priced order on its side and
an aggressor trades *through* it -- a sell executing against bids at 99 while
the shadow bids 100 -- the shadow is not filled, although under price priority
it would have been hit first. The usual way to get there is for the real
orders at the shadow's price to be cancelled, leaving the shadow alone at a
level the visible book no longer has.

**Direction.** It can only miss fills, never invent them. The computed
never-cancel fill rate is therefore a lower bound on what the stated
assumptions imply, and a reported Kaplan-Meier *understatement* is, on this
account alone, a lower bound on the true one. It shrinks a positive error such
as SAP's in H2. How much any of this moves is not measured.

**Pinned.** `tests/python/test_shadow_tracker.py::test_a_trade_through_a_better_priced_shadow_fills_it`
is a strict `xfail`. When the engine is fixed it starts passing, strict mode
turns that into a failure, and whoever fixed it is forced to update this entry
and the README.

**Cost of fixing.** Both engines change together. In the C++ engine shadows are
bucketed by exact level, so a trade-through check needs an ordered index of
live shadow prices per side rather than one hash lookup per event; the
throughput gate has to be re-checked. Every committed manifest is produced by
the old semantics, so the fix has to ship with a rerun of H1-H5 on the
2019-12-30 session, and a cheaper first step is a diagnostic counter of how
often a live shadow is traded through, which sizes the problem before paying for
the fix.
