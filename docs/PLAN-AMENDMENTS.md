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
