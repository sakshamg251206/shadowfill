# Plan amendments

Deviations from
`docs/superpowers/plans/2026-09-19-shadowfill-ground-truth-engine.md`.
Where this file and the plan disagree, this file wins.

Each entry records the date, what the plan said, what was found, and what
changed.

## Agreed 2026-09-19, during Task 1

### A. Build gating (Task 1, done) and the cpp CI job (Task 9)

The C++ targets in `CMakeLists.txt` are gated behind
`option(SHADOWFILL_BUILD_CORE "Build the C++ engine" OFF)`, pinned OFF from
`[tool.scikit-build.cmake.define]`. Task 9 flips it to `"ON"` in that task's
commit. Rationale: without a gate, `pip install -e .` fails at CMake configure
from Task 1 until Task 10, because `book.cpp` and `shadow.cpp` do not yet
exist. An explicit option is preferred over `if(EXISTS ...)` because a mistyped
source path must be a loud CMake error — with filesystem probing it would yield
a wheel silently missing `shadowfill._core`, `test_cpp_equivalence.py` would
skip itself via `importorskip`, and CI would go green over an unvalidated C++
engine. `find_package(pybind11)` is `REQUIRED`, not `QUIET`, for the same
reason. The `cpp` CI job moves from Task 1 to Task 9.

### B. Anti-skip guard (Task 11)

From Task 11 onward, `conftest.py` must **fail** rather than skip when the
environment variable `SHADOWFILL_REQUIRE_CORE=1` is set, and the CI cpp job
sets it. A skipped equivalence test must never be able to pass for the wrong
reason.

### C. Event ordering inside `on_event` (Tasks 6 and 10)

Replaces the four-step ordering in Task 6. Per event `ev`, in exactly this
order:

1. **activate** every pending placement with `effective_ts < ev.ts_ns`:
   `insert_ts = placement.effective_ts` (*not* `ev.ts_ns`),
   `insert_seq = ev.seq`, `ahead = book.level_size(side, price)` read from the
   book state *before* `ev`, `expiry_ts = insert_ts + horizon_ns`
2. **expire** every active with `expiry_ts < ev.ts_ns` → `EXPIRED`
3. **match** `ev` against the remaining actives
4. **harvest** filled
5. `book.apply(ev)`

`finalize()`: remaining actives → `TRUNCATED`; never-activated pending →
`NOT_ACTIVATED` (see D).

Two things the original ordering got wrong. Activating before `book.apply` on a
timestamp tie made a placement at `ts=0` see an empty book, giving
`ahead_at_insert == 0` where the Task 6 tests assert `30`. And expiring before
activating meant a placement whose entire life falls in a quiet gap between two
events came out `TRUNCATED` instead of `EXPIRED`.

**Tie convention** (state this in the `RefShadowTracker` docstring): an order
arriving at exactly the placement's effective timestamp is **ahead** of the
shadow. Sequence numbers decide priority and the shadow has none, so we take
the pessimistic side. Never flatter the hypothetical order.

Two test corrections follow:

- `test_hidden_execution_never_consumes_the_queue` asserts `EXPIRED` but places
  a 10 s horizon against 2 s of data, which is `TRUNCATED`. Change the
  placement to `horizon=1 * SEC`; keep `ahead_at_end == 10`. Mirror in the C++
  case.
- Add a test pinning the tie convention:

```python
def test_order_added_at_exactly_the_placement_timestamp_is_ahead():
    events = make_events([
        (0, 1, 100, 30, EventType.ADD, Side.BID),
        (1 * SEC, 2, 100, 40, EventType.EXECUTE, Side.BID),
    ])
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.ahead_at_insert == 30
```

All remaining Task 6 and Task 10 expectations were worked through this rule by
hand and are unchanged.

### D. Fifth status: `NOT_ACTIVATED` (Tasks 6, 10, 12)

```
RESTING = 0, FILLED = 1, EXPIRED = 2, TRUNCATED = 3, NOT_ACTIVATED = 4
```

`TRUNCATED` now means only "activated, still resting when the stream ended".
`NOT_ACTIVATED` means "`effective_ts` fell past the last event". These are
different events in the world and must not share a code: a never-activated
placement is not a censored observation, it is a placement that never happened,
and it must not enter any fill-rate denominator in Plan 3. Mirror in C++.

### E. Nulls, not sentinels, in Parquet (Task 12)

`-1` stays in the in-memory `Outcome` struct, so the C++/Python equivalence
test keeps comparing raw integers. The Parquet writer converts every numeric
field to an Arrow null on rows where `status == NOT_ACTIVATED`. Nulls propagate
or raise downstream; `-1` silently regresses as a covariate.

`first_fill_ts` and `full_fill_ts` stay `-1` on activated-but-unfilled rows —
that is a real observation, not a missing one. Document the convention in the
`Outcome` docstring. Two tests to add in Task 12:
`test_not_activated_rows_are_null_not_sentinel` and
`test_no_negative_values_survive_to_parquet` (the latter over `insert_ts`,
`insert_seq`, `ahead_at_insert`, `ahead_at_end`, `filled_qty`, excluding the
two fill-timestamp columns).

### F. Per-shadow assumption accounting (Tasks 6, 10, 12)

`_is_ahead` must not increment `unknown_order_assumed_ahead` when called for
the `fifo_violations` diagnostic; split out a non-counting lookup. Note this
narrows the tracker-level counter: on the `EXECUTE` path the queue arithmetic
uses `ahead` directly and only the diagnostic looks the order up, so after the
split that counter increments on cancel/delete only. Keep it, keep reporting it
in the manifest, and name it accordingly.

Add a per-shadow column to `Outcome`:

```
assumed_ahead_events: int64  # times this shadow's `ahead` was decremented
                             # on an order id absent from the book
```

Increment only when the arithmetic actually depended on the assumption: a
cancel or delete at this shadow's price and side, unknown id, `ahead`
decremented. Never from the `fifo_violations` lookup. Counted per
(shadow, event), so one unknown order touching three actives increments three
rows. This makes "drop every shadow whose outcome depended on an unverifiable
assumption, rerun, does the result hold" a one-line filter in Plan 4 rather
than a rebuild; that robustness check is going in the paper, so the column
exists from the start.

### G. Dependency pinning (Task 3)

`pip` resolves the plan's floors to numpy 2.4.x / pandas 3.0.x / mypy 2.x.
pandas 3.0 changed string and NA handling, which is exactly what
`parse_seconds_to_ns` leans on. Add `pandas-stubs` to the dev extra, keep loose
ranges in `pyproject.toml`, and commit exact pins in `requirements-dev.lock`
from `pip freeze`. CI installs from the lock; `make install` uses
`pyproject.toml`. Do **not** cap pandas to make the stubs happy. mypy stays
`strict`; if one function still fights, scope the override to that module with
a comment explaining why — never a blanket relaxation.

### H. Environment provenance in the manifest (Task 12)

The run manifest records the resolved environment: Python version, installed
versions of numpy / pandas / pyarrow, and the compiler id and version used for
the C++ engine. A result that cannot be traced to an environment is not
reproducible, and that claim is load-bearing in this repository.

## Added 2026-09-19, during Tasks 5-12

### I. Coverage for `top_of_book_series` without LOBSTER data (Task 5)

**Plan said:** Task 5 validates `top_of_book_series` solely against LOBSTER's
own orderbook snapshot file, and adds no other test for it. Its step 4 warns
"do not proceed on skips alone".

**Found:** the LOBSTER sample is not present in `data/lobster/`, so both
`needs_lobster` tests skip and the function would ship with zero executed
coverage.

**Changed:** added `test_top_of_book_series_reports_state_after_each_event` to
`tests/python/test_refbook.py`, a hand-built five-event stream checking best
bid/ask after each event including a delete that uncovers a worse level and a
hidden execution that changes nothing. This does **not** substitute for the
snapshot comparison -- an independent external reference is a different and
stronger check -- and the LOBSTER tests remain in place, unweakened, to be run
once the sample is downloaded.

### J. `test_still_resting_at_end_of_data_is_truncated` split in two (Task 6)

**Plan said:** one test, asserting a placement at `ts=0` against a single event
at `ts=0` comes out `TRUNCATED`.

**Found:** under amendment D that placement is `NOT_ACTIVATED`, not
`TRUNCATED` -- its effective timestamp never falls strictly before any event,
so it never activates. The plan's test would have passed only by conflating the
two states amendment D exists to separate.

**Changed:** renamed to `test_still_resting_at_end_of_data_is_not_activated`
asserting `NOT_ACTIVATED`, and added
`test_activated_and_still_resting_at_end_of_data_is_truncated` with a second
event, which activates the shadow and then ends the stream under it -- the
genuine TRUNCATED case, which the plan never covered. Net: one more assertion
than the plan, not one fewer. Mirrored in the C++ suite.

### K. When `assumed_ahead_events` increments (Task 6)

**Plan said (amendment F):** increment when "a cancel or delete at this
shadow's price and side, unknown id, `ahead` decremented".

**Found:** "`ahead` decremented" is ambiguous when `ahead` is already 0, where
`max(0, 0 - size)` leaves it unchanged. Counting there would attribute an
assumption to a shadow whose arithmetic the assumption could not have altered.

**Changed:** increments only when `ahead > 0` at the time, i.e. when the
unverifiable assumption genuinely moved this shadow's queue position. This is
the reading that makes the Plan 4 robustness filter ("drop every shadow whose
outcome depended on an unverifiable assumption") mean what it says.

### L. Synthetic generator was super-linear and had to be re-indexed (Task 7)

**Plan said:** pick a live order to cancel/execute with
`live = [oid for oid, (_, _, s) in resting.items() if s == side]` followed by
`rng.choice(live)`.

**Found:** that rebuild is O(|resting|) per event, and the resting set grows
without bound because adds (55%) outpace removals. Measured:
5k events 0.64 s, 10k 1.6 s, 20k 6.8 s, 40k 43.3 s -- 4x the events for 68x the
time. Generating the 200k committed fixture exceeded two minutes, and Task 12's
benchmark asks for a 2,000,000-event stream, which was unreachable.

**Changed:** per-side dense id lists plus an oid -> slot map, so removal is a
swap with the last element and selection is `live[rng.integers(0, len(live))]`.
40k events now takes 0.198 s (218x faster) and 2M takes 14.0 s. The choice is
still uniform over live orders on that side, so the process is distributionally
unchanged; the concrete stream for a given seed differs from the plan's version
because the RNG is consumed differently, which is harmless -- no committed
artifact predated this change and the tests require determinism per seed, not
specific values.

**Not changed, but flagged:** adds outpace removals, so the resting set and
book depth grow monotonically through the stream. The generator is therefore
non-stationary, and queue-ahead statistics drift over a long run. That is a
property of the plan's process, not of this fix, and changing the
probabilities is a research decision rather than an implementation one.

### M. Invariant tests verified by mutation, not by passing (Task 8)

**Plan said:** "Expected: FAIL -- test_hidden_executions_alone_never_fill_anything
and test_prefix_invariance_no_future_information_leaks are the ones most likely
to expose real bugs. If all nine pass immediately, re-read Task 6 step 3."

**Found:** all nine passed on the first run, because Task 6 was implemented
with the corrected ordering already in place (amendment C). Passing tests are
not evidence that the tests work, so both defects the plan names were injected
deliberately and the suite re-run:

* `book.apply` moved before `_match`: 3 failures
  (`test_cancellation_ahead_reduces_queue_but_behind_does_not`, and both
  `assumed_ahead_events` tests).
* `EXECUTE_HIDDEN` added to the queue-consuming set: 2 failures
  (`test_hidden_execution_never_consumes_the_queue`,
  `test_hidden_executions_alone_never_fill_anything`).

Both mutations were reverted and the suite reverified at 23 passed.

**Changed:** nothing in the source. Recorded because "all nine passed" on its
own would not have met the plan's own bar for evidence.

**Worth noting:** the ordering mutation was caught only by Task 6's unit tests
-- every test in `test_invariants.py`, including `test_prefix_invariance`,
still passed under it. Prefix invariance constrains *when* information may be
used, not *which* book state is consulted within one event, so it is weaker
than the plan's framing suggests. The unit tests are what protect that
invariant.

### N. Task 10's code block predates amendments C, D and F (Task 10)

**Plan said:** Task 10 step 3 gives a complete `shadow.hpp` / `shadow.cpp` to
write, and its prose says to "port `RefShadowTracker` verbatim, including the
four-step event ordering".

**Found:** the prose and the code block disagree. The block was written before
amendments C, D and F and contradicts all three — it expires before activating,
activates on `effective_ts <= now_ts`, takes `insert_ts`/`expiry_ts` from the
current event rather than the placement, has no `NOT_ACTIVATED`, counts the
assumption inside `is_ahead`, and has no `assumed_ahead_events` column. Written
out verbatim and run, it failed 4 of the 8 Task 10 tests.

**Changed:** followed the prose and the amendments, which this file says win on
conflict. The C++ tracker now matches `RefShadowTracker` statement for
statement. Task 11's field-by-field equivalence test over five seeds is what
holds that claim up; this commit only makes it plausible.

**Also:** two Task 10 test scenarios were unsatisfiable and are corrected under
amendment J, mirroring the Python suite rather than relaxing an assertion.
`hidden executions never consume the queue` asserted `EXPIRED` with a 10 s
horizon over 2 s of data (now `horizon = 1 * kSec`). `still resting at end of
stream is truncated` used a lone event at `ts=0` against a placement with
`effective_ts = 0`, which never activates, so it was testing non-activation;
split into the `NotActivated` case and a genuine `Truncated` case with a second
event. Both corrected expectations were checked against `run_reference` first,
which returns `TRUNCATED` and `NOT_ACTIVATED` respectively.

**Worth noting:** the plan's code block would have passed its own suite only
under the pre-amendment semantics. Had it been written out without running the
tests, the C++ engine would have silently disagreed with the oracle on every
timestamp tie, and Task 11 would have been the first thing to notice.

### O. The synthetic generator did not respect price-time priority (Task 7)

**Plan said:** executions pick a live order with
`live[rng.integers(0, len(live))]`, i.e. uniformly over every resting order on
that side, at any depth.

**Found:** that is not a matching engine. Measured on seed 5 over 40k events,
0.3% of `EXECUTE` events landed at the best price and 3.2% hit the oldest order
at their own level. ShadowFill's entire claim is queue arithmetic under
price-time priority, so a fixture that ignores it makes every queue statistic
derived from it unsafe to reason about — and the definition of done asks for
`fifo_violations == 0` on synthetic data, which was unreachable.

**Changed:** `EXECUTE` and `EXECUTE_HIDDEN` now cross to the best price and
consume the oldest order resting there (100% at best price on every seed
checked). Adds and cancels are deliberately left zero-intelligence and uniform
over live orders at any depth: Plan 4 needs a cancellation mechanism
independent of fill outcome by construction, so cancels must not consult the
queue front. Implemented with a per-(side, price) arrival queue plus a heap of
occupied prices, cancelled ids purged lazily from the front, so the generator
stays linear — 200k events in 0.95 s. The committed fixture is regenerated;
per amendment L the tests require determinism per seed, not specific values, so
no expectation moved.

**Not changed, but flagged:** amendment L's observation still stands. Adds
outpace removals, so the book deepens monotonically through a long run and the
generator remains non-stationary.

### P. `fifo_violations` measured the normal path, not a defect (Tasks 6, 10, 12)

**Plan said:** on `EXECUTE`, `if ahead == 0 and not is_ahead(order_id,
insert_seq): fifo_violations += 1`, and the definition of done requires the
count to be 0 on synthetic data.

**Found:** the counter cannot be 0 on a correct stream. Once a shadow's `ahead`
reaches 0, every order still resting at that price arrived after it *by
construction*, so the next execution there necessarily trips the condition —
and that same execution's residual is what fills the shadow. Making executions
FIFO-faithful (amendment O) raised the count from 5 to 99 on seed 5, and of the
75 distinct shadows involved, 74 ended `FILLED` and the remaining one `EXPIRED`
after a partial fill. The counter was measuring "a shadow reached the front of
its queue and was filled there": the desired path, under a name that claims a
data defect.

**Changed:** redefined as a property of the event stream alone, with no shadow
in it. On an `EXECUTE` of a known order id, count it if an order that arrived
earlier is still resting at the same price and side. This is 0 on a FIFO stream
by construction, and on real LOBSTER data a nonzero count means hidden
liquidity, an order type this reconstruction does not model, or a genuine gap
in it — which is what is worth pinning in a manifest. The counter moved to
`RefBook` / `OrderBook`, where the state it needs already lives, and the
trackers delegate to it. Six direct tests per engine pin the definition.

The old shadow-relative quantity is not replaced by a renamed counter: it is
already derivable from the outcome columns as
`status == FILLED and ahead_at_end == 0`.

**Consequence for the definition of done:** "`fifo_violations` is 0 on
synthetic data and reported on real data" is now a meaningful gate rather than
an unreachable one, and is kept as written.

### Q. The tracker was O(live shadows) per event and missed the gate (Tasks 10, 12)

**Plan said:** `match` loops over every active shadow and skips the ones whose
side or price do not match; `expire` partitions the whole active list each
event; `harvest_filled` partitions it again. Task 12's benchmark then asks for
≥ 2,000,000 events/s at a 10 ms grid with a 10 s horizon.

**Found:** those two are incompatible. That configuration keeps ~2000 shadows
live at once, and throughput is set by that number, not by the stream. Measured
on 2,000,000 synthetic events, varying only the placement schedule:

| live shadows | events/s |
|---|---|
| 2000 | 212,564 |
| 200 | 1,784,669 |
| 20 | 3,975,927 |

The plan's benchmark sits in the top row, at 215k events/s — an order of
magnitude under its own gate. This is not only a benchmark artefact:
`configs/ground_truth_synthetic.yaml` runs at roughly 1200 live shadows.

**Changed:** shadows are bucketed by `(side, price)`, since an event can only
touch shadows at its own price and side; expiry is a min-heap keyed on expiry
timestamp; and `harvest_filled` is gone, because a filled outcome is final and
can be settled where it happens. Actives moved to a deque with a `settled`
flag, as the buckets and heap hold indices into it, so memory is O(total
placements) rather than O(live) — ~45 MB at 400k placements, flagged in the
header with the free list that would fix it.

Same stream and placements: **2,437,567 events/s, 11.5x**, past the gate. The
accounting is untouched and the five-seed equivalence against the oracle still
passes, which is the claim that matters. The Python oracle keeps its linear
scans: it optimises for being obviously correct.

### R. Task 12 additions beyond its code block (Task 12)

**Plan said:** `run_ground_truth` leaves `diagnostics` empty for the Python
engine, `OUTCOME_FIELDS` omits `assumed_ahead_events`, the manifest records no
environment, the Parquet writer writes raw `-1`, and `main()` takes no
`--config` although the `reproduce` make target already passed one.

**Found:** each of those contradicts something already agreed — amendment E
(nulls not sentinels), F (the per-shadow assumption column), H (environment
provenance) — or the definition of done, which asks for `fifo_violations` to be
reported and not silently dropped. "Reported only when you happen to use the
fast engine" is not that.

**Changed:** all four honoured. `replay_reference` returns outcomes and
diagnostics together, with `run_reference` now a thin wrapper over it. The
config reader is a strict parser for a flat map of seven scalars rather than a
YAML dependency: an unknown key, a missing colon or a value that will not
coerce raises, so the reader's narrowness costs a clear error rather than a
wrong run that still writes a confident manifest. `_core.pyi` was added so the
fourteen parallel arrays at the binding call site are type-checked rather than
ignored, with the `ignore_missing_imports` override scoped to pyarrow alone per
amendment G. The `cpp` and `bench` CI jobs, overdue from amendment A, landed
here along with `SHADOWFILL_REQUIRE_CORE` on the python job per amendment B.

**Also:** two of the plan's Task 12 tests could skip themselves. The
NOT_ACTIVATED test hoped a stream would produce such a row; it now forces them
with latency and asserts they exist. A test that can pass by not running is not
a test.

### S. Known issue: `make lint` breaks on Python newer than 3.11

**Found:** while verifying the definition of done on a clean clone, a
virtualenv built with the machine's default `python3` (3.14) resolved numpy
2.5.3, whose stubs use `type` statements. mypy is pinned to
`python_version = "3.11"`, so it rejects 3.12+ syntax in those stubs and lint
fails before checking any of this project's code. On Python 3.11 — what CI
uses, and what `requires-python` was written for — lint is clean.

**Not changed.** `requires-python = ">=3.11"` currently advertises support this
repository does not test, so a contributor on 3.12+ hits a confusing failure in
numpy's stubs rather than in their own work. The honest fixes are to cap
`requires-python` to what CI actually exercises, or to add newer interpreters
to the CI matrix and let `python_version` follow. Choosing between those is a
project decision, so it is recorded here rather than made silently.

### T. Definition of done: two items remain unverified (Plan 1)

Items 2 and 3 — `pytest -m needs_lobster`, and top-of-book reconstruction
matching LOBSTER's own snapshots on >95% of in-band rows — require the LOBSTER
sample, which is not present on this machine and is not redistributed here. The
tests exist and skip; they do not pass, and they must not be reported as
passing. Until they are run, this repository has demonstrated that its two
engines agree with each other and with hand-worked cases, not that its
reconstruction matches a real exchange's own book.
