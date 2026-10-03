# ShadowFill — Current Status

**As of 2026-10-04.** This is a handoff document: it records
where the project actually stands, including what is broken, unverified, or
withdrawn. It is written to be read by someone with no memory of how any of it
was arrived at.

Read alongside `RESEARCH-SPEC.md` (the hypotheses and evaluation design) and
`PLAN-AMENDMENTS.md` (every deviation from plan and why) and `RESULTS.md`
(every experiment in full). Sections 4 and 5 are the record of the 2026-09-24
rerun of H1–H5 on 2019-12-30; the IPCW, latency and three-heuristic H4 results
that came after it are in `RESULTS.md` and amendments Z–AC. The second session
(2019-03-27), the cross-day transfer run and the decision not to bootstrap over
two sessions are in §5.1, `RESULTS.md` and amendment AG.

---

## 1. What the project is trying to establish

Every passive-execution and market-making backtest rests on a fill model:
given that I rest a limit order at price *p*, do I get filled, when, and what
is the price doing when I do. That model is almost never validated, for a
structural reason — **you cannot observe the fills of an order you never
sent.**

The literature works around this by fitting survival models to *real* resting
orders, treating cancellation as censoring. That assumes an order someone
withdrew would have filled at the same rate as the orders still resting. For
limit orders that is false in a specific, directional way, and it is the error
this project measures.

**The opening.** L3/MBO data carries an order id per order. Under price–time
priority, a hypothetical order placed at *p* at time *t* has everything already
resting at *p* ahead of it and everything later behind it. Maintain one
integer, `ahead`:

- a cancel or delete of an order ahead → `ahead -= removed_size`
- a trade at *p* eats the front → `consumed = min(size, ahead)`; any residual
  would have reached the shadow, which is a fill
- an add at *p* is behind, and is ignored

`ahead` is monotone. The fill time of a never-cancel hypothetical order is
therefore **computed by arithmetic, not modelled**. That is a ground truth for
a counterfactual that was never traded, and every number in this repository is
measured against it.

**The one assumption:** the shadow order does not change other participants'
behaviour. Defensible for small sizes in liquid books. It is stated in the
README and is *not* yet stress-tested — see §7.

This is a measurement project. It never claims PnL and no strategy is proposed.

---

## 2. What has been completed

| | state |
|---|---|
| **Plan 1** — ground-truth engine | Built. 4 of 6 definition-of-done items pass; 2 are externally blocked (§7) |
| **Plan 2** — data layer | Built on Nasdaq TotalView-ITCH. **Two** sessions acquired, verified and materialised (2019-12-30, 2019-03-27) |
| **Plan 3** — estimators + L2 ablation | Partly built. KM, Aalen–Johansen, matched comparison, block bootstrap, L2 ablation under three queue heuristics, and IPCW with baseline and time-varying covariates all exist. Cox, Fine–Gray, IPCW policy re-targeting, dependent-censoring bounds and the ML baseline do **not** |
| **Plan 4** — failure tests + paper README | Placebo and known-bias injection built and gating CI. Latency sweep run on AAPL. Cross-book transfer built, tested and **run across both sessions** (amendment AG). The impact stress test does not exist |

**Concretely present:** 21 Python modules, 29 test files, 253 tests, a C++20
engine with pybind11 bindings that agrees with the Python oracle byte-for-byte
on real exchange data, and 14 committed result directories, each pinning a manifest.

**Repository:** `https://github.com/sakshamg251206/shadowfill.git`.
`main` carries local, **unpushed** commits for the second-session work
(see `git log origin/main..main`); nothing from it is on `origin` yet.

**Parked:** branch `plan-2a-recorder` (not on `origin`) holds a complete, tested Coinbase L3
recorder that has no venue to record from (amendment U). Deliberately unmerged:
merging code that cannot run would misrepresent what the repository does.

---

## 3. Dataset and its validation status

### Source

**Nasdaq TotalView-ITCH 5.0**, published free and account-free at
`https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/`. 15 full trading days are
available. No payment, no API key, no terms gate.

ITCH is the raw feed LOBSTER is itself derived from. Its `Order Executed` (`E`)
message names the order reference of the resting order that was consumed, which
is the property the whole project depends on.

### The one session acquired

```
file    data/itch/12302019.NASDAQ_ITCH50.gz
size    3,524,013,057 bytes   (matches the advertised content-length exactly)
sha256  ef03df46a27e6bda4dead017f84c2e3979df7211f02c7868b51d53fceb99c689
```

### Validation status of the 2019-12-30 session: fully verified

| check | result |
|---|---|
| `gzip -t` | **passes** — complete member, trailer and CRC verified |
| Parse with `allow_truncated=False` | **completes without raising**; `truncated=False` on every symbol |
| Messages decoded | **268,744,780**, zero framing errors |
| Unresolved order references | **0** on every symbol |
| Oversized removals | **0** |
| Zero-size messages dropped | **0** |
| Session span | **04:00 → 20:00 ET**, the full 16-hour day |

Framing integrity is the strong check: ITCH uses a 2-byte big-endian length
prefix, so a single corrupted byte desynchronises every subsequent message and
raises immediately. 268 million clean messages is not a soft assurance.

**Earlier, during download:** `E` executions resolving to a live resting order
were **1,569 / 1,569 (100.00%)** on a 20 MB prefix. That is the measurement
that established the project was viable on this source at all.

### The second session acquired

```
file    data/itch/03272019.NASDAQ_ITCH50.gz
size    5,510,131,732 bytes
sha256  7997025b9e09dd6c2ecb0bfa48a856197e6e800711ab67367ee0f2ab724b9ba8
```

`gzip -t` passes; 422,264,305 messages; zero unresolved references, zero
oversized removals, `truncated=False` on all six symbols. Events per symbol:
AAPL 2,146,794, MSFT 2,291,059, INTC 1,508,273, CSCO 1,178,134, UN 719,644,
SAP 480,371.

**Provenance caveat.** The first assembled copy of this file was corrupt
(`gzip -t` failed with a CRC error; sha256 `0cdaf47d…`). The most likely
cause is two fetcher processes writing the same part files at once, which the
script's size check does not catch (the damaged part was written while both
were running; the later single-fetcher download was clean; not proven). One 8 MiB part (byte offset 243,269,632) differed from a
fresh fetch of the same range in 763,931 bytes; it was replaced, the file was
rescanned end to end with no framing anomaly, and `gzip -t` and the sha256 then
matched the value recorded in amendment AE. The 2019-12-30 file was re-fetched
with a single fetcher and matched its recorded sha256 first time. Detail in
amendment AG.

### Materialised dataset

`data/parquet/date=<date>/symbol=<SYM>/events.parquet` for both dates,
Hive-partitioned,
with a `manifest.json` per day pinning that day's input sha256. The table is
2019-12-30; 2019-03-27's counts are above. The AAPL file for 2019-12-30 was
re-materialised on 2026-10-04 and hashes to `302d1302390e18f0…`, identical to
the input hash pinned in the committed 2019-12-30 manifests.

| symbol | events (full day) |
|---|---|
| AAPL | 1,616,327 |
| MSFT | 1,275,844 |
| INTC | 792,985 |
| CSCO | 578,560 |
| UN | 454,636 |
| SAP | 151,819 |

Timestamps are nanoseconds since midnight US/Eastern, as the feed defines them,
and are never converted to wall-clock instants — every downstream calculation is
a difference inside one session, so a timezone conversion could only add error.

**No third-party data is committed to the repository.** `data/` is gitignored
and CI touches none of it.

### Other files on disk (not committed)

- `12302019.NASDAQ_ITCH50.snap614mb.gz` — a frozen 614 MB partial kept because
  the first round of results was computed from it. Those results are withdrawn,
  but the file makes their manifests checkable.
- `12302019.prefix2mb.gz`, `12302019.prefix20mb.gz` — small prefixes used by
  the `needs_itch` test suite.

---

## 4. Experiment status, H1 through H5

All five ran on AAPL, 2019-12-30, regular hours 09:30–16:00 ET, 1,581,219
events, 791,477 real orders each shadowed by a never-cancel hypothetical order,
13 half-hour bootstrap blocks, 200 replicates, seed 0, C++ engine. H2
additionally ran across six symbols. **All five were then rerun unchanged on
AAPL, 2019-03-27** (2,097,418 events, 1,056,435 orders; §5.1). The verdicts
below are the 12-30 verdicts; on 03-27 the direction of H1, H3, H4 and H5 is the
same and H2 still fails, with one change to its SAP finding (§5.1).

| | status | verdict |
|---|---|---|
| **H1** the bias exists and is material | **Complete** | **Supported.** All intervals exclude zero |
| **H2** the sign is regime-dependent | **Complete** | **Not supported.** The sign does flip, but not along the tick axis |
| **H3** adverse selection offsets it | **Complete** | **Number delivered, mechanism not supported.** The offset does not appear |
| **H4** the L2 penalty | **Complete** for front, proportional and back (amendment Z); uniform-over-orders needs an order count L2 does not carry | **Supported.** The three heuristics bracket the truth |
| **H5** decision relevance | **Complete** | **Not supported.** Rankings do not invert |

Three of five pre-registered claims came back negative. The headline effects
they were meant to explain are large and significant.

---

## 5. Numerical results from the latest rerun

### H1 — never-cancel truth versus Kaplan–Meier

| horizon | truth F* | KM | error | 95% CI |
|---|---|---|---|---|
| 100 ms | 0.0172 | 0.0140 | −0.0031 | [−0.0037, −0.0024] |
| 1 s | 0.0506 | 0.0372 | −0.0134 | [−0.0172, −0.0082] |
| 10 s | 0.2059 | 0.1085 | −0.0974 | [−0.1176, −0.0665] |
| 60 s | 0.4331 | 0.1644 | **−0.2688** | [−0.2925, −0.2253] |

A passive order at AAPL's touch that never cancels fills **43%** of the time
within a minute. KM fitted on the real orders in the same book says **16%**.

**Mechanism.** By 60 s the risk set is almost entirely orders nobody bothered
to pull, and nobody pulls an order that was never going to fill. The survivors
are adversely selected for *not* filling and the estimator extrapolates their
hazard to everyone.

Engine diagnostics: `unknown_order_assumed_ahead` 530, `fifo_violations` 686
(0.043% of events), `unknown_order_events` 380. All expected on real data.

### H2 — across tick regimes, 60 s horizon

| symbol | price | tick (bps) | F* | KM | error | 95% CI |
|---|---|---|---|---|---|---|
| AAPL | 289.70 | 0.35 | 0.4331 | 0.1644 | −0.2688 | [−0.2922, −0.2236] |
| MSFT | 157.75 | 0.63 | 0.4877 | 0.1332 | −0.3545 | [−0.3769, −0.3103] |
| **SAP** | 133.33 | **0.75** | 0.0725 | 0.1532 | **+0.0806** | [+0.0678, +0.0961] |
| INTC | 59.66 | 1.68 | 0.4071 | 0.1592 | −0.2479 | [−0.3028, −0.1640] |
| UN | 57.82 | 1.73 | 0.1201 | 0.0406 | −0.0795 | [−0.0948, −0.0584] |
| CSCO | 47.52 | 2.10 | 0.3234 | 0.1012 | −0.2222 | [−0.2682, −0.1337] |

SAP reverses sign with an interval well clear of zero. It sits mid-range at
0.75 bps between two negatives, so the flip is **not** ordered by relative tick
and the magnitude is not monotone in it either.

The distinguishing feature of SAP and UN is cross-listing: they have the lowest
ground-truth fill rates here, 0.07 and 0.12 against 0.32–0.49, because Nasdaq
sees only a slice of their liquidity. **That is a candidate mechanism, not a
finding** — this session cannot test it.

### H3 — error in expected passive edge, basis points

| horizon | true edge | estimated edge | error | 95% CI |
|---|---|---|---|---|
| 100 ms | −0.000 | −0.000 | +0.000 | [−0.000, +0.000] |
| 1 s | −0.005 | −0.004 | +0.001 | [−0.001, +0.003] |
| 10 s | −0.039 | −0.020 | +0.019 | [+0.008, +0.030] |
| 60 s | **−0.087** | **−0.031** | **+0.056** | [+0.033, +0.076] |

Resting passively and never cancelling costs **0.087 bps** to adverse selection
over a minute; the observational estimate says **0.031 bps**.

Decomposed at 60 s: the fill-rate gap is 0.4331 vs 0.1644 (a factor of 2.6);
the markout gap is −0.200 vs −0.190 (5%). **The offsetting markout bias the
hypothesis rests on is not there.** The entire edge error is the fill-rate
error, priced.

### H4 — the level-2 penalty (cancel-from-front)

| horizon | L3 truth | L2 guess | error | 95% CI |
|---|---|---|---|---|
| 100 ms | 0.0172 | 0.0204 | +0.0032 | [+0.0026, +0.0036] |
| 1 s | 0.0506 | 0.0655 | +0.0149 | [+0.0123, +0.0165] |
| 10 s | 0.2059 | 0.2444 | +0.0385 | [+0.0352, +0.0411] |
| 60 s | 0.4331 | 0.4664 | +0.0333 | [+0.0292, +0.0382] |

At one second an L2 simulator promises **29% more fills than it gets** — the
same order of magnitude as the cancellation bias, which is what H4 predicted.
The two errors point in opposite directions, so an L2 backtest that also
censors on cancellation gets a partial offset. That is luck, not correctness.

### H5 — decision relevance

| wait | F* | edge* (bps) | F̂ | edgê (bps) |
|---|---|---|---|---|
| 100 ms | 0.0172 | −0.5061 | 0.0140 | −0.5077 |
| 1 s | 0.0506 | −0.4938 | 0.0372 | −0.4996 |
| 5 s | 0.1396 | −0.4670 | 0.0830 | −0.4864 |
| 30 s | 0.3382 | −0.4054 | 0.1421 | −0.4683 |
| 60 s | 0.4331 | **−0.3786** | 0.1644 | **−0.4614** |

Crossing immediately costs 0.5147 bps. **Inversions 0 / 10 pairs.** Both
methods pick 60 s, and they agree on the best policy in **100%** of bootstrap
replicates.

Truth says the choice between best and worst policy is worth **0.128 bps**;
the estimator says **0.046 bps** — understated **2.8×**, but not enough to
change which policy a desk would pick.

---

### 5.1 The second session, 2019-03-27

Same code, same parameters, same seed; only the input differs. Every number is
in `results/*-2019-03-27/manifest.json`; the full tables are in `RESULTS.md`.
The manifests carry a `-dirty` suffix on the commit hash: tracked files were
unmodified, and the flag reflects untracked result directories written by the
earlier steps of the same run (amendment AG).

| AAPL, 60 s | 12-30 | 03-27 |
|---|---|---|
| H1 truth F* / KM / error | 0.4331 / 0.1644 / −0.2688 | 0.4909 / 0.1200 / **−0.3709** |
| H3 error in expected edge | +0.056 bps | +0.072 bps |
| H4 proportional guess at 1 s, excess fills | +25% | +36% |
| H5 stakes understated by | 2.8× | 4.8× |
| H5 inversions | 0 / 10 | 0 / 10 |

- The sign and ordering of every H1, H3, H4 and H5 result repeats. Magnitudes
  are all larger on 03-27 and the 60 s H1 intervals do not overlap. Two days
  cannot say whether that difference is noise or regime.
- **H2: SAP's reversal does not reappear.** On 03-27 SAP reads −0.0026 with
  interval [−0.0217, +0.0295], which straddles zero. The other five names are
  negative with intervals excluding zero. One reversal in two symbol-days leaves
  the cross-listing question open (§7 item 13).
- **There is no interval over days.** The spec's session-level bootstrap is
  degenerate at two sessions; see amendment AG. Cross-day statements in this
  repository are side-by-side point estimates.

**Cross-day transfer** (`results/transfer-2019-12-30-2019-03-27`, twelve
symbol-day books). A per-(horizon, queue-ahead stratum) calibration learned on
one book and applied to another cuts the absolute error in 64% (336 / 528) of
pair cases and in 10 of 12 same-symbol cross-day pairs at 60 s. It is not a
fix: carried from 12-30 to 03-27 it under-corrects, carried the other way it
over-corrects and flips the sign (AAPL +0.20, INTC +0.28), and on SAP the
pooled calibration worsens the error on both days with intervals excluding zero.

---

## 6. Tests and checks

### Passing

```
253 tests collected
CI selection (-m "not needs_lobster and not needs_itch"):
  245 passed, 1 xfailed, 7 deselected   on 3.11 (locked), 3.12 and 3.13 (latest)
ruff check   clean        ruff format --check   clean
mypy (strict, python/shadowfill)   clean, 23 source files
```

The one `xfail` is strict and deliberate: it pins the trade-through gap
(§7, `IDEAS.md`).

| check | status |
|---|---|
| **Placebo** (`make placebo`) | **passes** — −0.0000, −0.0002, −0.0015 at 100 ms / 1 s / 10 s. This gates everything |
| **Known-bias injection** (`make injection`) | **passes** — an injected picked-off canceller is recovered with the right sign, monotone in strength. The opposite mechanism does *not* reverse sign; pinned as a negative result, amendment Y |
| C++ / Python engine equivalence, synthetic | passes, five independent seeds |
| C++ / Python engine equivalence, **real ITCH data** | passes — all ten outcome fields and all three diagnostics byte-identical |
| Prefix invariance (no look-ahead) | passes |
| ITCH parser layout tests (synthetic binary) | 44 passing across parser + dataset + real-sample |
| Benchmark | passes. Gated on **speedup over the Python reference** (≥200×, observed ~900–1270×), not absolute events/s |

### Skipped, not failing

Two tests in `test_refbook_vs_lobster_orderbook.py` are marked `needs_lobster`
and skip for want of a sample. **They do not pass and must never be reported as
passing.** `needs_itch` tests run when a sample is present.

### Failing

**None.** The strict `xfail` above is a known limitation, not a failure; it
turns into one when the engine is fixed.

---

## 7. Known limitations and unresolved questions

### Data

1. **Two sessions.** Results are AAPL (six symbols for H2 and transfer) on
   2019-12-30 and 2019-03-27. The block bootstrap is **within-session only**: it
   resamples half-hour blocks inside one day and says nothing about day-to-day
   variation. `RESEARCH-SPEC.md` §6 specifies blocks over sessions, which with
   two sessions is degenerate (three distinct resamples), so it is not
   implemented as an interval (amendment AG). It needs materially more days —
   in the order of ten, of which the source offers 15 — before a session-level
   interval means anything.
2. **One venue, one asset class.** No futures, no crypto, no non-US equities.
3. **Survivorship** is not yet considered: the six symbols were chosen for
   liquidity and price spread, not sampled from a point-in-time universe.

### Validation

4. **Book reconstruction is validated less strongly than under LOBSTER.**
   LOBSTER ships an orderbook file — an independent third-party reconstruction
   — so the book could be checked row by row against one this project did not
   build. Raw ITCH has no such file and cannot: ITCH is the *input* that file
   is derived from, so any book built from it is our own reconstruction and
   comparing it to itself proves nothing. The ITCH path checks internal
   consistency instead (book never crosses, all references resolve, no
   oversized removals) — necessary conditions, not sufficient ones. Level
   aggregation below the best price is not checked against any outside
   observer. Full detail in amendment V.1.
5. **Plan 1's definition of done has two permanently unchecked items**, both
   requiring the LOBSTER sample, which is no longer published free. They remain
   unchecked in the plan rather than quietly ticked.

### Methodology

6. **The no-impact assumption is untested.** The shadow order is assumed not to
   change anyone else's behaviour. Plan 4's impact stress test does not exist.
7. **Three of four L2 heuristics are implemented** (front, proportional,
   back; amendment Z), and they bracket the truth. Uniform-over-orders needs
   an order count that an L2 feed does not carry, so on L2 it collapses into
   proportional.
8. **The markout horizon is fixed at 1 s** and its sensitivity is unexplored.
9. **The crossing cost in H5 is a session-median half-spread**, not the spread
   at each decision point. Deliberate — a per-order spread would make the cost
   model depend on the queue state the policies differ in — but it is an
   approximation.
10. **~10% of matched shadow/order pairs have slightly differing queue-ahead**,
    because events sharing a timestamp can activate a shadow on a neighbouring
    event. Pinned by test at >90% exact.
11. **Plan 4's impact stress test does not exist.** The latency sweep has run
    (amendment AA); cross-book transfer has now run on both sessions (amendment AG). Known-bias injection exists (amendment Y) but
    recovers only one of the two mechanisms H2 names.
12. **Trade-through fills are not credited.** The engine matches an execution
    against a shadow only at the shadow's own price, so a shadow left alone at
    a better price than the level an aggressor trades through is not filled.
    It can only lower the computed truth. Unmeasured; pinned by a strict
    `xfail` and written up in `IDEAS.md`. Fixing it changes every committed
    result, so it must ship with a rerun.

### Open research questions

13. **Why does SAP reverse sign?** Cross-listing and fragmented liquidity is
    the obvious candidate. Untested. On 2019-03-27 SAP does not reverse (error
    −0.0026, interval straddling zero), so the reversal is one of two
    symbol-days. It is still the most interesting open thread in the project.
14. **Does the tick-regime prediction hold over a wider range?** 0.35–2.10 bps
    may be too narrow to contain the regimes H2 is about.
15. **Is the H1 bias stable across days and across regimes?** Partly answered:
    on two days its sign and the qualitative H3–H5 conclusions repeat, and its
    size does not (AAPL 60 s error −0.269 and −0.371). Whether the spread is
    noise, regime or tick is untested, and two days cannot say.
16. **Would a correct competing-risks or IPCW estimator close the gap?**
    Aalen–Johansen answers a different question (fills under *their*
    cancellation policies). IPCW on queue position and order age removes 3% of
    the bias at 60 s (amendment AC); whether a richer censoring model would do
    better is open, and the injection results say a residual is a lower bound
    on what the correction misses, not a measure of hidden information.

---

## 8. Retracted or changed claims

Recorded because a reader who discovers these is worse than one who is told.

### Retracted: every result computed with grid placement

The first comparison placed shadow orders on a **time grid** and compared them
against real orders at the touch. The **placebo test rejected it**: on the
synthetic stream, whose cancellation is uniform over live orders and therefore
independent of fill prospects by construction, the measured bias must vanish.
It was **+0.26**.

Cause: grid shadows sat behind a median queue of **259** shares while real
orders at the touch sat behind **42.5** — six times shallower. Queue position
dominates fill probability, so a difference in *populations* was being reported
as the cancellation bias. Coarse strata could not fix a sixfold difference
inside a bin.

Fix: `place_matched_to_orders` puts one shadow on each real order at that
order's own time, price, side and size. Populations are then identical by
construction and the only difference is that the shadow never cancels. The
placebo then reads −0.0000 / −0.0002 / −0.0015. Amendment X.

**All AAPL numbers published before that fix are withdrawn.**

### Retracted: "the sign never flips" (H2)

Reported on a 46-minute window, where SAP was excluded for holding only 8,131
events. The full session gives SAP **151,819** events and it **reverses to
+0.0806** with an interval excluding zero. H2 is still unsupported, but for a
different reason — the flip exists and is not ordered by tick.

### Retracted: "the correction flattens the decision into near-indifference" (H5)

On the 46-minute window the two methods disagreed on the best policy in
**38.7%** of bootstrap replicates. On the full session that is **0.0%**. The
instability was small-sample noise and the claim was overstated.

### Retracted: "the sign flips across horizon" (H1)

An artefact of the grid design. Under matched placement AAPL's error is
−0.0031 → −0.0134 → −0.0974 → −0.2688, monotone and never flipping.

### Changed: Plan 2's data source, twice

Coinbase L3 → Databento → Nasdaq ITCH. Coinbase `level3` turned out to exist
only on Coinbase Exchange, which is gated behind a business application
(amendment U). Databento priced correctly but returned `402
account_insufficient_funds` on every download because pay-as-you-go was
disabled; **nothing was ever spent**. Nasdaq publishes the same underlying feed
free (amendment V).

### Changed: `fifo_violations`

Originally measured the normal path — it fired whenever a shadow reached the
front of its queue and filled. Redefined as a property of the event stream
alone.

### Changed: the benchmark gate

An absolute 2M events/s floor encoded a machine and failed on CI hardware. Now
gated on **speedup over the Python reference engine** measured in the same
process on the same box, so runner speed divides out.

---

## 9. Exact next steps

In priority order.

1. **Acquire more sessions** (the source offers 15; two are in hand). Two is
   enough for a direction check and not for an interval over days. With
   roughly ten, the session-level bootstrap `RESEARCH-SPEC.md` §6 specifies
   becomes meaningful and can be implemented. Fetch with exactly one fetcher
   process (amendment AG).
2. **Size the trade-through gap** (§7 item 12) with a diagnostic counter, then
   fix it in both engines and rerun everything.
3. **Investigate SAP's sign reversal.** The most interesting open question.
   The second day did not reproduce it, so it is not yet established as a
   property of SAP, of cross-listing, or of one symbol-day; more sessions and
   another cross-listed name are needed.
4. **Build the impact stress test**, the last missing Plan 4 failure test.
   Known-bias injection's unresolved half — why hopeless-queue cancellation
   does not reverse the measured sign on synthetic data — needs an injection
   that removes an order from the risk set without removing depth from the
   book.
5. **Plan 3's missing estimators**: cause-specific Cox, Fine–Gray, IPCW policy
   re-targeting, dependent-censoring bounds.

---

## 10. Reproduction commands

Everything below is run from the repository root with the project venv active.

### Verify the toolchain

```bash
make install        # pip install -e ".[dev]"
make lint           # ruff check + ruff format --check + mypy
make test           # full Python suite, no external data needed
make test-cpp       # CMake + Catch2
make bench          # throughput gate (speedup over the reference engine)
make placebo        # THE falsification test — run this first
make reproduce      # all of the above plus the synthetic ground-truth run
```

### Acquire and verify the session

```bash
./scripts/fetch_itch_chunked.sh 12302019.NASDAQ_ITCH50.gz data/itch 8
gzip -t data/itch/12302019.NASDAQ_ITCH50.gz      # must pass before proceeding
```

Expected: 3,524,013,057 bytes, sha256
`ef03df46a27e6bda4dead017f84c2e3979df7211f02c7868b51d53fceb99c689`.

**Fetch with exactly one fetcher process.** Two concurrent runs of the script
share part-file names and corrupted `03272019` once (amendment AG). If a run is
interrupted, confirm no `xargs` or `curl` from it remains before rerunning, then
verify with `gzip -t` and the sha256 recorded here.

Second session, same procedure:

```bash
./scripts/fetch_itch_parallel.sh 03272019.NASDAQ_ITCH50.gz data/itch 4 8
gzip -t data/itch/03272019.NASDAQ_ITCH50.gz
# sha256 7997025b9e09dd6c2ecb0bfa48a856197e6e800711ab67367ee0f2ab724b9ba8
```

### Materialise

```bash
python -m shadowfill.dataset \
    --itch-path data/itch/12302019.NASDAQ_ITCH50.gz \
    --symbols AAPL,MSFT,INTC,CSCO,UN,SAP \
    --out-root data/parquet
```

No `--allow-truncated`: the complete file does not need it, and `gzip -t`
passing is the precondition that proves it.

### The five experiments

```bash
P=data/parquet/date=2019-12-30/symbol=AAPL/events.parquet

# H1 — bias in the fill curve
python -m shadowfill.experiment --message-path $P \
    --out-dir results/h1-aapl-2019-12-30 --n-replicates 200

# H3 — error in expected passive edge, bps
python -m shadowfill.markout --message-path $P \
    --out-dir results/h3-edge-aapl-2019-12-30 --n-replicates 200

# H4 — level-2 ablation
python -m shadowfill.l2 --message-path $P \
    --out-dir results/h4-l2-aapl-2019-12-30 --n-replicates 200

# H5 — decision relevance
python -m shadowfill.policies --message-path $P \
    --out-dir results/h5-policies-aapl-2019-12-30 --n-replicates 200

# H2 — across tick regimes, all symbols
python -m shadowfill.regimes --session-date 2019-12-30 \
    --symbols AAPL,MSFT,SAP,INTC,UN,CSCO \
    --out-dir results/h2-regimes-2019-12-30 --n-replicates 150
```

All default to regular trading hours (09:30–16:00 ET), matched placement, seed
0, and 30-minute bootstrap blocks. Each writes `manifest.json` pinning the git
commit, input sha256, full config and seed. `results/*/manifest.json` is
committed; the Parquet outputs are not.

### The second session and the transfer run

Materialise and run exactly as above with `03272019` / `2019-03-27`
substituted, then:

```bash
# one --book per symbol per date, twelve in all (zsh does not word-split a
# string of flags; build an array)
python -m shadowfill.transfer \
    --book AAPL@2019-12-30=data/parquet/date=2019-12-30/symbol=AAPL/events.parquet \
    --book AAPL@2019-03-27=data/parquet/date=2019-03-27/symbol=AAPL/events.parquet \
    ...  --out-dir results/transfer-2019-12-30-2019-03-27 \
    --n-replicates 200 --seed 0
```

### Runtimes on developer hardware

| step | time |
|---|---|
| Download a full day | hours; server throttles to ~120–220 KB/s |
| `gzip -t` on 3.5 GB | ~1 min |
| Materialise 6 symbols | ~4 min |
| H1 on 1.58 M events | ~5 min |
| H3 / H4 / H5 | ~2–5 min each |
| H2 across 5 symbols | ~15 min |
| Full test suite | ~2 min |

---

## 11. Work in progress and pending

**Nothing is currently running.** No background job and no partial write.

**Pending, in the sense of started-and-not-finished:** nothing. Every
experiment listed above completed and wrote its manifest.

**Pending, in the sense of owed:**

- `README.md` still describes a one-session repository and its headline
  figures; it has not been updated for 2019-03-27 and was outside this task.
- `fetch_itch_parallel.sh` has no concurrency guard (`docs/IDEAS.md`).
- `plan-2a-recorder` remains parked and unmerged, by decision.
- The two `needs_lobster` definition-of-done items remain unchecked, by
  external blockage.
- A **Coinbase CDP API key was pasted into a chat transcript** during the Plan
  2a work and should be rotated. It was never used successfully and grants no
  L3 access, but it was disclosed.
