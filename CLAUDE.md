# CLAUDE.md — ShadowFill

Read this file, then `docs/RESEARCH-SPEC.md`, then
`docs/superpowers/plans/2026-09-19-shadowfill-ground-truth-engine.md`.
Do not start writing code until you have read all three.

## What this project is

A research repository measuring how wrong passive-fill backtests are.

Every market-making and passive-execution backtest depends on a fill model that
nobody validates, because you cannot observe the fills of an order you never
sent. ShadowFill exploits the fact that market-by-order (L3) data carries order
IDs: for a hypothetical passive order, you know exactly which real orders were
ahead of it and can watch them get traded or cancelled. So the fill time of a
never-cancel hypothetical order is **computed by arithmetic, not modelled** —
and that gives a ground truth to measure every fill estimator against.

This is a measurement project, not a trading strategy. It never claims PnL.

## Who I am and what I need from you

Undergraduate targeting quant research/trading roles. This repository is the
portfolio piece that top trading firms will read. That changes the bar:

- **Correctness beats features.** A wrong number here is worse than a missing one.
- **Never invent results.** Every figure and every number in any README, doc or
  commit message must come from a run that actually happened, traceable to a
  manifest. If an experiment has not been run, say so; do not write a plausible
  number as a placeholder.
- **Honesty about limitations is a feature.** Assumptions get stated in the
  README, not buried. A limitation I documented is a strength in an interview; a
  limitation a reader discovers is fatal.
- **Explain the reasoning, not just the diff.** When you make a modelling or
  statistical choice, say why in the commit message or a code comment.

## Non-negotiable invariants

Violating any of these silently invalidates the research. Treat them as load-bearing.

1. **Hidden executions never consume the visible queue.** LOBSTER event type 5
   is a hidden-order execution; it never sat in the visible book.
2. **Shadow accounting runs before the book applies each event.** Resolving
   whether a cancelled order was ahead requires looking it up before it is deleted.
3. **`ahead` is monotone non-increasing and never negative.**
4. **Strictly streaming, prefix-only.** No feature, label or outcome may depend
   on an event later than the one being processed. There is a prefix-invariance
   test for this; do not weaken it.
5. **Python `shadowfill.replay` is the oracle.** If it and the C++ engine
   disagree, the C++ engine has the bug — unless you can prove otherwise, in
   which case fix the Python first and say so.
6. **No third-party data in the repository or in CI.** Tests run on the committed
   synthetic fixture. LOBSTER-dependent tests are marked `needs_lobster`.
7. **Every run writes a manifest** pinning git SHA, input SHA-256 and full config.

## Working agreement

- **TDD, strictly.** Write the failing test, run it and watch it fail, implement
  the minimum, run it and watch it pass, commit. The plan is already written in
  this shape — follow it step by step, do not batch steps together.
- **Commit at every step boundary** using the exact messages in the plan.
- **Never mark a step complete without running the command and reading the
  output.** "Should pass" is not evidence.
- **If a test fails in a way the plan did not anticipate, stop and tell me.**
  Do not redesign around it silently, and never weaken a test to make it green.
- **If you think the plan is wrong, say so before implementing it.** The plan is
  a strong prior, not an order. Pushing back with a reason is welcome; quietly
  deviating is not.
- Update the plan's `- [ ]` checkboxes as you complete steps.

## Stack and commands

Python 3.11, NumPy, pandas, pyarrow, pytest, Hypothesis.
C++20, CMake ≥ 3.24, pybind11, Catch2 v3, scikit-build-core.
ruff, mypy, pre-commit, GitHub Actions.

```
make install     # pip install -e ".[dev]"
make test        # Python suite — must pass with no external data
make test-cpp    # CMake + Catch2
make lint        # ruff + mypy
make bench       # throughput gate (≥ 2M events/s)
```

## Repository layout

```
src/shadowfill_core/      C++20 engine (book, shadow tracker)
src/shadowfill_bindings/  pybind11 module -> shadowfill._core
python/shadowfill/        schema, adapters, reference implementation, runners
tests/python/             pytest suite (oracle + invariants + equivalence)
tests/cpp/                Catch2 suite
tests/fixtures/           committed synthetic MBO stream
benchmarks/               throughput gate
docs/RESEARCH-SPEC.md     hypotheses, novelty argument, evaluation design
docs/superpowers/plans/   implementation plans, one per subsystem
```

## Scope discipline

The project is four plans. Plan 1 (the ground-truth engine) is the only one with
an implementation plan written. **Do not build ahead into Plans 2–4.** No
estimators, no plots, no crypto recorder, no strategy layer until Plan 1's
definition of done is met and I have reviewed it.

If you find yourself wanting to add a feature that is not in the current plan,
write it down in `docs/IDEAS.md` and move on.

## Things that will look like bugs but are not

- `unknown_order_events > 0` on real data is expected: cancels and executions can
  reference orders added before the recording window. They are assumed to be
  ahead of the shadow, and the count is reported in the manifest. Do not suppress it.
- Top-of-book reconstruction will disagree with LOBSTER snapshots on rows where
  the true best price sits outside the requested level band. Those rows are
  excluded by design, not patched around.
- `RefBook.best_bid()` is a linear scan. That is deliberate; the reference
  implementation optimises for obviousness and the C++ engine is the fast path.
