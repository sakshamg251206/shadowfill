# ShadowFill — Current Status

**As of 2026-10-04.** This is a handoff document: it records
where the project actually stands, including what is broken, unverified, or
withdrawn. It is written to be read by someone with no memory of how any of it
was arrived at.

Read alongside `RESEARCH-SPEC.md` (the hypotheses and evaluation design) and
`PLAN-AMENDMENTS.md` (every deviation from plan and why) and `RESULTS.md`
(every experiment in full). Sections 4 and 5 record the 2026-10-04 rerun of
every experiment on both sessions, after the engines began crediting
trade-through fills (amendments AH, AI); every committed result manifest was
regenerated then, at commit `0ae5196`. What that rerun changed is in §8.

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
| **Plan 1** — ground-truth engine | Built. 4 of 6 definition-of-done items pass; 2 are externally blocked (§7). Credits trade-through fills since 2026-10-04 (amendment AI) |
| **Plan 2** — data layer | Built on Nasdaq TotalView-ITCH. **Two** sessions acquired, verified and materialised (2019-12-30, 2019-03-27) |
| **Plan 3** — estimators + L2 ablation | Partly built. KM, Aalen–Johansen, matched comparison, block bootstrap, L2 ablation under three queue heuristics, and IPCW with baseline and time-varying covariates all exist. Cox, Fine–Gray, IPCW policy re-targeting, dependent-censoring bounds and the ML baseline do **not** |
| **Plan 4** — failure tests + paper README | Placebo and known-bias injection built and gating CI. Latency sweep run on AAPL. Cross-book transfer built, tested and **run across both sessions** (amendment AG). The impact stress test does not exist |

**Concretely present:** 22 Python modules, 30 test files, 268 tests, a C++20
engine with pybind11 bindings that agrees with the Python oracle byte-for-byte
on real exchange data, and 22 committed result directories, each pinning a
manifest.

**Repository:** `https://github.com/sakshamg251206/shadowfill.git`.
`main` is **in sync with `origin/main`**.

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

Every experiment ran on AAPL, regular hours 09:30–16:00 ET, matched placement
(one never-cancel shadow per real order), 13 half-hour bootstrap blocks, 200
replicates (150 for H2), seed 0, C++ engine, on both sessions: 2019-12-30
(1,581,219 events, 791,477 orders) and 2019-03-27 (2,097,418 events,
1,056,435 orders). H2 and the transfer test use six symbols.

| | status | verdict |
|---|---|---|
| **H1** the bias exists and is material | **Complete**, both days | **Supported.** All intervals exclude zero on both days |
| **H2** the sign is regime-dependent | **Complete**, both days | **Not supported.** SAP flips sign on 12-30 only; the flip is not ordered by tick |
| **H3** adverse selection offsets it | **Complete**, both days | **Not supported.** The markout *is* biased, but it compounds the fill-rate error |
| **H4** the L2 penalty | **Complete** for front, proportional and back, both days | **Not supported.** The heuristics bracket the truth, but the penalty is an order of magnitude below H1 |
| **H5** decision relevance | **Complete**, both days | **Not supported.** Rankings never invert |

Four of five pre-registered claims came back negative. The headline effect they
were meant to explain is large, significant and repeats on the second day.

---

## 5. Numerical results from the latest rerun

AAPL, 2019-12-30, unless stated. Full tables for both days in `RESULTS.md`.

### H1 — never-cancel truth versus Kaplan–Meier

| horizon | truth F* | KM | error | 95% CI |
|---|---|---|---|---|
| 100 ms | 0.0207 | 0.0140 | −0.0067 | [−0.0081, −0.0049] |
| 1 s | 0.0760 | 0.0372 | −0.0387 | [−0.0494, −0.0244] |
| 10 s | 0.3204 | 0.1085 | −0.2119 | [−0.2437, −0.1659] |
| 60 s | 0.5531 | 0.1644 | **−0.3887** | [−0.4183, −0.3371] |

A passive order at AAPL's touch that never cancels fills **55%** of the time
within a minute. KM fitted on the real orders in the same book says **16%**.
On 2019-03-27: **59%** against **12%**, error −0.4747 [−0.4818, −0.4533].

**Mechanism.** By 60 s the risk set is almost entirely orders nobody bothered
to pull, and nobody pulls an order that was never going to fill. Much of the gap
is orders pulled just before the market moved through their price, which a
never-cancel order is filled by and its pulled twin is not.

Engine diagnostics: `unknown_order_assumed_ahead` 530, `fifo_violations` 686
(0.043% of events), `unknown_order_events` 380 — identical before and after the
trade-through fix. All expected on real data.

### H2 — across tick regimes, 60 s horizon

| symbol | tick (bps) | error 12-30 | 95% CI | error 03-27 | 95% CI |
|---|---|---|---|---|---|
| AAPL | 0.35 / 0.53 | −0.3887 | [−0.4169, −0.3378] | −0.4747 | [−0.4821, −0.4528] |
| MSFT | 0.63 / 0.86 | −0.4073 | [−0.4368, −0.3564] | −0.5253 | [−0.5373, −0.5013] |
| **SAP** | 0.75 / 0.88 | **+0.0544** | [+0.0424, +0.0702] | **−0.0918** | [−0.1314, −0.0259] |
| INTC | 1.68 / 1.88 | −0.2696 | [−0.3298, −0.1786] | −0.4649 | [−0.4909, −0.4249] |
| UN | 1.73 / 1.72 | −0.1327 | [−0.1520, −0.0944] | −0.1380 | [−0.1607, −0.1178] |
| CSCO | 2.10 / 1.89 | −0.2425 | [−0.2931, −0.1490] | −0.4231 | [−0.4420, −0.3891] |

SAP reverses on 12-30 and not on 03-27, both intervals clear of zero. Every
other name is negative on both days. The flip is not ordered by relative tick.
SAP and UN, the cross-listed names, have the lowest ground-truth fill rates on
both days. **That is a candidate mechanism for their magnitude, not a finding.**

### H3 — error in expected passive edge, basis points

| horizon | true edge | estimated edge | error | 95% CI |
|---|---|---|---|---|
| 100 ms | −0.002 | −0.000 | +0.002 | [+0.000, +0.004] |
| 1 s | −0.022 | −0.004 | +0.018 | [+0.006, +0.028] |
| 10 s | −0.137 | −0.020 | +0.117 | [+0.066, +0.161] |
| 60 s | **−0.230** | **−0.031** | **+0.199** | [+0.126, +0.260] |

Decomposed at 60 s: fill rate 0.5531 vs 0.1644 (3.4×); markout −0.415 vs
−0.190 bps (2.2×). **The markout is biased, and in the direction that adds to
the fill-rate error** — the fills the estimator never sees are the
picked-off ones. On 03-27 the edge error is +0.255 bps.

### H4 — the level-2 penalty

| horizon | L3 truth | front | proportional | back |
|---|---|---|---|---|
| 1 s | 0.0760 | +0.0081 [+0.0073, +0.0091] | +0.0063 [+0.0056, +0.0068] | −0.0034 [−0.0042, −0.0028] |
| 60 s | 0.5531 | +0.0053 [+0.0041, +0.0068] | +0.0041 [+0.0032, +0.0052] | −0.0038 [−0.0044, −0.0033] |

The heuristics bracket the truth at every horizon on both days; proportional
promises 8% more fills than it gets at 1 s (16% on 03-27). That is an order of
magnitude below the H1 bias (0.039 at 1 s, 0.389 at 60 s), so H4's "same order
of magnitude" prediction fails. Trade-through fills depend on price, not queue
position, so L2 gets them right too.

### H5 — decision relevance

| wait | F* | edge* (bps) | F̂ | edgê (bps) |
|---|---|---|---|---|
| 100 ms | 0.0207 | −0.5062 | 0.0140 | −0.5077 |
| 1 s | 0.0760 | −0.4976 | 0.0372 | −0.4996 |
| 5 s | 0.2256 | −0.4904 | 0.0830 | −0.4864 |
| 30 s | 0.4691 | −0.4691 | 0.1421 | −0.4683 |
| 60 s | 0.5531 | **−0.4598** | 0.1644 | **−0.4614** |

Crossing immediately costs 0.515 bps. **Inversions 0 / 10 pairs** on both days.
Both methods pick 60 s; they disagree on the best policy in 15% of bootstrap
replicates (6% on 03-27). Fill rate and markout errors nearly cancel in edge:
both methods value the choice at 0.046 bps.

### 5.1 Across the two sessions

| AAPL, 60 s | 12-30 | 03-27 |
|---|---|---|
| H1 truth F* / KM / error | 0.5531 / 0.1644 / −0.3887 | 0.5947 / 0.1200 / **−0.4747** |
| H3 error in expected edge | +0.199 bps | +0.255 bps |
| H4 proportional guess at 1 s, excess fills | +8% | +16% |
| H5 inversions | 0 / 10 | 0 / 10 |

- Every qualitative H1, H3, H4 and H5 conclusion repeats. Magnitudes are larger
  on 03-27 and the 60 s H1 intervals do not overlap. Two days cannot say whether
  that difference is noise or regime.
- **There is no interval over days.** The spec's session-level bootstrap is
  degenerate at two sessions; see amendment AG. Cross-day statements in this
  repository are side-by-side point estimates.

**Cross-day transfer** (`results/transfer-2019-12-30-2019-03-27`, twelve
symbol-day books). A per-(horizon, queue-ahead stratum) calibration learned on
one book and applied to another cuts the absolute error in 68% (358 / 528) of
pair cases and in 9 of 12 same-symbol cross-day pairs at 60 s. It is not a
fix: carried from 12-30 to 03-27 it under-corrects, carried the other way it
over-corrects and flips the sign (AAPL +0.21, INTC +0.29), and SAP gets worse
in both cross-day directions.

**Robustness and correction.** At 60 s the bias moves by under 1% across
0–20 ms of latency; at 100 ms it turns into a small overstatement from 1 ms up.
IPCW on queue position and order age removes 2% of it at 60 s.

---

## 6. Tests and checks

### Passing

```
268 tests collected
CI selection (-m "not needs_lobster and not needs_itch"):
  261 passed, 7 deselected   on 3.11 (locked), checked locally on 2026-10-04
ruff check   clean        ruff format --check   clean
mypy (strict, python/shadowfill)   clean, 24 source files
make test-cpp   all Catch2 cases pass
```

The strict `xfail` that pinned the trade-through gap is now an ordinary
passing test.

| check | status |
|---|---|
| **Placebo** (`make placebo`) | **passes** — −0.0001, −0.0007, −0.0037 at 100 ms / 1 s / 5 s against a 0.02 tolerance. This gates everything |
| **Known-bias injection** (`make injection`) | **passes** — an injected picked-off canceller is recovered with the right sign, monotone in strength. The opposite mechanism does *not* reverse sign; pinned as a negative result, amendment Y |
| C++ / Python engine equivalence, synthetic | passes, five independent seeds, all three L2 cancel models |
| C++ / Python engine equivalence, **real ITCH data** | passes — a 15-minute AAPL slice, 55,804 shadows, every outcome field and diagnostic identical |
| Trade-through credit, cross-checked | the measurement module credits **zero** further shadows on the fixed engine's output, on all four books it was run on |
| Prefix invariance (no look-ahead) | passes |
| ITCH parser layout tests (synthetic binary) | passing across parser + dataset + real-sample |
| Benchmark | passes. Gated on **speedup over the Python reference** (≥200×); 258–310× observed on 2026-10-04, down from 1,090–1,224× before the trade-through fix (amendment AI) |

### Skipped, not failing

Two tests in `test_refbook_vs_lobster_orderbook.py` are marked `needs_lobster`
and skip for want of a sample. **They do not pass and must never be reported as
passing.** `needs_itch` tests run when a sample is present.

### Failing

**None.**

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
12. **Opposite-side fills are not credited.** Trade-throughs are credited
    since 2026-10-04 (amendment AI). A new order on the opposite side arriving
    at or through a lone shadow's price would also have filled it; that route
    is neither credited nor measured, so the truth is still a lower bound and
    measured understatements are conservative on this account. Crediting it
    leans harder on the no-impact assumption (item 6).

### Open research questions

13. **Why does SAP reverse sign?** Cross-listing and fragmented liquidity is
    the obvious candidate. Untested. SAP is positive on 2019-12-30 (+0.054) and
    negative on 2019-03-27 (−0.092), both intervals clear of zero, so the
    reversal is one of two symbol-days. Still the most interesting open thread.
14. **Does the tick-regime prediction hold over a wider range?** 0.35–2.10 bps
    may be too narrow to contain the regimes H2 is about.
15. **Is the H1 bias stable across days and across regimes?** Partly answered:
    on two days its sign and the qualitative H3–H5 conclusions repeat, and its
    size does not (AAPL 60 s error −0.389 and −0.475). Whether the spread is
    noise, regime or tick is untested, and two days cannot say.
16. **Would a correct competing-risks or IPCW estimator close the gap?**
    Aalen–Johansen answers a different question (fills under *their*
    cancellation policies). IPCW on queue position and order age removes 2% of
    the bias at 60 s (amendments AC, AI); whether a richer censoring model would do
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
placebo then read −0.0000 / −0.0002 / −0.0015 (−0.0001 / −0.0007 / −0.0037
at 100 ms / 1 s / 5 s on the current engine). Amendment X.

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
−0.0067 → −0.0387 → −0.2119 → −0.3887, monotone and never flipping.

### Changed by crediting trade-throughs (2026-10-04, amendment AI)

Both engines missed fills where an aggressor traded through a lone shadow's
price. It was measured (+0.12 at 60 s), fixed and every result rerun. Retracted
or changed as a result:

- **Retracted (H3): "the markout gap is 5%; the whole edge error is the
  fill-rate error, priced."** The missing fills were the adversely selected
  ones. Markout is now −0.415 vs −0.190 bps, and edge error +0.199 bps, not
  +0.056.
- **Changed verdict (H4): supported → not supported.** The front-model error at
  1 s (0.0149) had looked the same size as the H1 bias (0.0134). Now it is 0.0081
  against 0.0387, and 0.005 against 0.389 at 60 s.
- **Retracted (H5): "the estimator understates what the choice is worth by
  2.8×".** With the fills restored both value it at 0.046 bps. Rankings still
  never invert.
- **Retracted (latency): "at 1 s the bias flips sign with any latency".** At 1 s
  it now stays negative at every latency; the flip appears at 100 ms instead.
- **Changed (H1, H2): the headline grows** from −0.269 to −0.389 at 60 s; SAP's
  12-30 reversal shrinks from +0.081 to +0.054, and SAP 03-27 moves from −0.003
  (interval including zero) to −0.092 (excluding it).

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
2. **Measure the opposite-side route** (§7 item 12) the way the trade-through
   gap was measured before it was fixed, then decide whether to credit it.
3. **Investigate SAP's sign reversal.** The most interesting open question.
   SAP is positive on one day and negative on the other, so it is not yet established as a
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

- `fetch_itch_parallel.sh` has no concurrency guard (`docs/IDEAS.md`).
- `plan-2a-recorder` remains parked and unmerged, by decision.
- The two `needs_lobster` definition-of-done items remain unchecked, by
  external blockage.
- A **Coinbase CDP API key was pasted into a chat transcript** during the Plan
  2a work and should be rotated. It was never used successfully and grants no
  L3 access, but it was disclosed.
