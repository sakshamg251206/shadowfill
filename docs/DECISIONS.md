# ShadowFill — Methodological Decisions

Why the project is built the way it is. Each entry records the decision, the
alternative that was rejected, and the reason — so a future reader can
disagree with the reasoning rather than guess at it.

`PLAN-AMENDMENTS.md` is the chronological record of deviations. This file is
the thematic one: the decisions that shape what the numbers *mean*.

---

## Measurement design

### D1. A shadow order shadows a real order

**Decision.** One hypothetical order is placed on each real order, at that
order's own timestamp, price, side and size.

**Rejected.** Placing shadows on a fixed time grid at the prevailing best
price, and comparing them against real orders arriving at the touch.

**Why.** The grid design failed the placebo test, measuring +0.26 of bias on a
stream where cancellation is independent by construction and the answer must be
zero. The cause was population composition: grid shadows sat behind a median
queue of 259 shares while real orders at the touch sat behind 42.5, six times
shallower. Shadows are placed on a clock regardless of the book; real orders
arrive when someone chose to send them. Queue position is the dominant
determinant of a fill, so the unmatched comparison reported a composition
difference as the cancellation bias.

Matching makes the two populations identical by construction, so the only
difference between them is that the shadow never cancels — which is the only
condition under which the measured gap *is* the cancellation bias.

Stratifying on queue-ahead was tried and is insufficient: the populations
differ *within* a stratum as well as across.

### D2. The placement is timestamped one nanosecond early

**Decision.** A matched shadow carries `ts_ns = order.ts_ns - 1`.

**Why.** Activation is strict (`effective_ts < now_ts`) and shadow accounting
runs before the book applies each event. The offset makes the shadow activate
on the arriving ADD itself and see the queue *without* the order it is
shadowing. Without it the shadow would see that queue plus the order's own
size, and the difference between real and hypothetical would be bookkeeping.
A test pins the equality for >90% of pairs; the remainder are events sharing a
timestamp, where activation can land on a neighbour.

### D3. The event of interest is the first fill, on both sides

**Decision.** An order "filled" when it traded at all, at `first_fill_ts`, for
both real orders and shadows.

**Rejected.** Requiring the full size to be executed.

**Why.** A shadow that traded part of its size did get filled. Using a
different definition on the two sides would make the curves answer different
questions and the gap between them meaningless.

### D4. Fill rates come from a product-limit fit; conditional markouts do not

**Decision.** `F(h)` is estimated by Kaplan–Meier; `E[m|fill]` is the plain
mean over fills observed inside the horizon.

**Why.** Orders still resting when the recording window closes are censored,
not failures, and the product-limit estimator is what handles that. A censored
order contributes to `F` and, having no fill, contributes no markout. Mixing
the two is standard; it is stated rather than hidden because it is an
approximation.

### D5. Markout reads prices after the fill, deliberately

**Decision.** Markout is `side * (mid(t_fill + h) - fill_price)`, in basis
points, positive meaning profit to the passive side on either side of the book.

**Why this is not look-ahead bias.** A markout is a realised forward return
used to evaluate a decision already taken, not a feature available at decision
time. Nothing computed from it feeds back into the fill computation, which
remains strictly prefix-only (invariant 4).

A one-sided book yields NaN rather than an invented mid. A fabricated price
inside a PnL number is worse than a missing one.

### D6. Policies are evaluated without re-simulating

**Decision.** A cancel-at-T policy's outcome is derived from the never-cancel
outcome: the order fills under the policy exactly when its never-cancel fill
time is at or before T.

**Why, and what it rests on.** Your own cancellation cannot move the queue
ahead of you. This is sound only under the project's one standing assumption —
that the shadow does not change other participants' behaviour — which is
stated in the README and is not yet stress-tested.

### D7. The H5 policy class is rest-then-cross, not cancel-at-T

**Decision.** Policies are "rest passively at the touch for T, cross the spread
if still unfilled."

**Rejected.** Ranking pure cancel-at-T policies by markout.

**Why.** Passive markouts are negative here, so ranking on markout alone makes
"never get filled" the winner and the test is empty. Rest-then-cross contains a
genuine trade-off — waiting earns passive fills but suffers adverse selection,
not waiting pays the spread — so the optimum is interior and the ranking is
informative.

### D8. Crossing cost is a session-median half-spread

**Decision.** One number per session, the median of `(ask-bid)/2/mid` in bps.

**Rejected.** The spread prevailing at each order's decision time.

**Why.** A per-order spread would make the crossing cost depend on exactly the
queue state the policies differ in, mixing the cost model into the thing being
measured. This is an acknowledged approximation.

---

## Statistics

### D9. The bootstrap resamples contiguous time blocks, never orders

**Decision.** Half-hour blocks, resampled with replacement; shadows and real
orders drawn with the *same* blocks.

**Why.** Orders within a session are strongly dependent — they queue behind
each other and a single trade settles many at once. An order-level bootstrap
would treat that shared fate as independent evidence and report an interval
several times too narrow. Shadows and orders share the drawn blocks because the
estimate is a paired difference over one period; drawing them independently
would add variation that cancels in the estimate.

### D10. The bootstrap is within-session, and says so

**Decision.** Intervals quantify within-day sampling variation only, and every
manifest carries a `bootstrap_scope` field saying so.

**Why.** `RESEARCH-SPEC.md` §6 specifies blocks over sessions, which needs more
than one day. Reporting a within-session interval as though it covered
day-to-day variation would overstate the evidence.

### D11. Too few blocks is an error, not a narrow interval

**Decision.** Fewer than two time blocks raises.

**Why.** One block resampled with replacement is the same block every time,
producing a zero-width interval that looks like enormous precision.

### D12. Estimators are written out, not imported from `lifelines`

**Decision.** Kaplan–Meier and Aalen–Johansen are ~20 lines of numpy each,
pinned against hand-computed examples.

**Why.** The comparison between the two estimators *is* the result. A reader
checking the headline number should be able to check the arithmetic that
produced it, and the tie-handling and risk-set conventions should be visible
rather than inherited from a library's defaults. The Aalen–Johansen
implementation carries the classic off-by-one explicitly: it needs
`S(t_(i-1))`, survival just *before* each event time; using `S(t_i)` silently
deflates every incidence.

### D13. Symbols below 50,000 in-window events are excluded from H2

**Decision.** A hard floor, with the skip recorded in the manifest.

**Why.** A regime point built on a few hundred orders is worse than no point.
SAP had 8,131 in-window events on the 46-minute window and was excluded; on
the full session it has 151,819 and is included — and reverses sign, which is
the most interesting result in H2.

---

## Data

### D14. Nasdaq TotalView-ITCH, not Databento or LOBSTER

**Decision.** The external ground-truth source is Nasdaq's free public daily
ITCH files.

**Why.** Zero cost, no account, no key, and it is the raw feed LOBSTER is
itself derived from. Databento priced correctly but returned `402
account_insufficient_funds` on every download with pay-as-you-go disabled;
nothing was ever spent. LOBSTER's free sample is no longer published.

Bitstamp was rejected on research grounds rather than cost: its `order_deleted`
event does not distinguish a fill from a cancel, and that distinction *is* the
mechanism this project measures.

### D15. The ITCH adapter is an adapter, not a second order book

**Decision.** `itch.py` keeps one table, `order_ref -> (side, price, remaining
shares)`, and nothing else.

**Why.** Only `A`/`F` carry a symbol, side and price; `E`, `C`, `X`, `D` and
`U` carry an order reference and nothing else, and `D` carries no size either.
A canonical event needs all those fields, so the table undoes the feed's
compression and stops there — no levels, no aggregation, no best bid or ask, no
priority logic. All book and queue reasoning stays in `replay.py` and the C++
engine, which see only canonical events.

### D16. Order Replace (`U`) becomes DELETE + ADD

**Decision.** A replace emits two canonical events, and the new order inherits
side and stock from the original.

**Why.** The exchange treats a replace as a fresh arrival. Modelling it as an
amendment would wrongly preserve queue priority and inflate every fill rate
computed behind it. `U` is ~11% of order flow in the measured sample, so this
is not an edge case. Tracking `U` is also what took execution resolution from
1,485/1,569 to **1,569/1,569**.

### D17. Unresolvable references are dropped and counted, never guessed

**Decision.** An `E`/`C`/`X`/`D`/`U` naming an order added before the recording
window produces no canonical event and increments `unresolved_refs`.

**Why.** Its side and price were established by an `A` the window never
contained. The LOBSTER adapter can fall back on the event's own price and side
fields; ITCH has neither, and a guessed side would corrupt every queue position
behind it.

### D18. Truncation is opt-in and reported

**Decision.** `parse_itch(..., allow_truncated=True)` stops cleanly at a
partial final message and sets `diag.truncated`; the default raises.

**Why.** A byte-range prefix always ends mid-message and mid-gzip-member, and
prefixes were the only obtainable sample for a long time. Tolerating truncation
by default would make a failed download indistinguishable from a short session.

### D19. Zero-size messages are dropped, in one place, and counted

**Decision.** `emit()` refuses any event with size ≤ 0 and increments
`zero_size_messages`.

**Why.** Nasdaq prints a `Q` cross with zero shares and zero price at 09:30 for
a symbol with no auction interest — observed once for UN on 2019-12-30. It is
valid data and it is not an event: it moves no queue, and carried through it
would appear in every aggregate as a trade that never happened. The guard sits
in `emit()` rather than at each call site so no future message type can
reintroduce it.

### D20. Executions keep their identity under L2 ablation; cancels do not

**Decision.** `ablate_to_l2` blanks the order id on `CANCEL_PARTIAL` and
`DELETE` only.

**Why.** Price-time priority tells an L2 simulator that a trade took the front
of the queue — that much needs no identity. The ambiguity an aggregated feed
creates is specifically about *whose* cancel it was.

### D21. The ablation is a data transform, not a second simulator

**Decision.** The identical engine runs on the degraded stream.

**Why.** A second simulator would risk measuring the difference between two
implementations instead of between two data feeds.

### D22. Regular trading hours by default

**Decision.** Experiments window to 09:30–16:00 ET unless told otherwise, and
record the window and the pre-filter event count in the manifest.

**Why.** Measured: UN's entire 04:00–09:30 pre-market on 2019-12-30 contains no
executions at all, so the first unwindowed run reported 0.0000 at every horizon
and every stratum. Pre-market shadows are un-fillable noise in the denominator.

### D23. Timestamps stay as nanoseconds since local midnight

**Decision.** No conversion to wall-clock instants.

**Why.** Every downstream calculation is a difference inside one session, so a
timezone conversion could only introduce error. The manifest records the basis
explicitly.

### D24. The Arrow schema is written out field by field

**Decision.** `dataset.py` pins each column's Arrow type rather than inferring.

**Why.** pandas widens uint64, and an order reference that loses its top bits
stops matching the executions that name it.

---

## Engineering

### D25. The Python reference implementation is the oracle

**Decision.** Where `shadowfill.replay` and the C++ engine disagree, the C++
engine has the bug unless proven otherwise.

**Why.** One implementation must be authoritative or a disagreement is
unresolvable. The oracle optimises for being obviously correct — `RefBook`'s
best-price lookup is a deliberate linear scan — and the C++ engine is the fast
path.

Where a second, faster implementation of something in the oracle was genuinely
needed (the touch, read after every event, which makes the linear scan
quadratic), it lives outside `RefBook` and a test pins the two to identical
output.

### D26. The placebo gates everything

**Decision.** `make placebo` runs on every commit, and a failure invalidates
every number in the repository.

**Why.** It has already earned this: it rejected the original comparison
design before any result was published. A pipeline that cannot detect a known
zero cannot be trusted to report a non-zero.

### D27. The benchmark gates on speedup, not absolute throughput

**Decision.** CI times the pure-Python reference on the same slice in the same
process and gates on the ratio (≥200×, observed ~900–1270×).

**Why.** An absolute events/s floor encodes a machine. The engine cleared a
2M/s floor by 18% on developer hardware and landed at 1.42M/s on a GitHub
runner, so CI was failing on hardware rather than on a regression. Lowering the
floor would have re-encoded a different machine and drifted again on the next
runner image. A ratio divides runner speed out.

### D28. No third-party data in the repository or in CI

**Decision.** `data/` is gitignored; CI deselects both `needs_lobster` and
`needs_itch`; tests run on a committed synthetic fixture.

**Why.** Licensing, repository size, and the requirement that a reader be able
to run the suite without acquiring anything.

### D29. Result manifests are committed; result data is not

**Decision.** `results/*/manifest.json` is tracked; Parquet outputs are not.

**Why.** A manifest is small, and it is the only thing that makes a reported
number checkable — it pins the git commit, input sha256, full config and seed.
A number nobody can trace back to an input is not evidence.

### D30. Negative results are published as prominently as positive ones

**Decision.** H2, H3's mechanism and H5 are reported as failures in the README
at the same weight as H1 and H4.

**Why.** Three of five pre-registered claims came back negative while the
headline effects they were meant to explain are large and significant. A
reviewer who sees a failed mechanism of the author's own hypothesis reported
plainly will trust the rest more. The alternative — quietly dropping the
mechanisms and keeping the effects — is the standard failure mode this project
exists to argue against.
