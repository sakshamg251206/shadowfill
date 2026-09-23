"""Plan 3: inverse-probability-of-censoring weighting (IPCW).

Naive Kaplan-Meier treats a cancelled order as if it would have gone on to fill
at the same rate as the orders still resting. If cancellation depends only on
things you can observe, that failure is repairable: model the cancellation
hazard given those observables, and weight every fill by the inverse of the
probability that its order was still uncancelled when it filled,

    F_ipcw(h) = (1/n) * sum_i 1{fill_i, T_i <= h} / G(T_i- | x_i),

where G(t | x) is the probability of remaining uncancelled past t given x. The
reweighted curve then targets the never-cancel fill law -- exactly the ground
truth this repository computes, which is what makes the question testable.

**What it can and cannot fix.** IPCW removes the part of the bias that runs
through the covariates in the censoring model. Whatever remains is cancellation
driven by something the model does not see -- private information, a view on
the next trade -- and no reweighting on observables can recover that. So the
gap left after IPCW is itself a measurement: of how much of the bias is
unobservable from the data a backtester has.

**The censoring model here** is non-parametric and stratified on queue-ahead at
arrival, the strongest baseline predictor of a fill. Within a stratum, G is the
Kaplan-Meier survival of "not yet cancelled or window-censored", with fills
treated as censoring of that process. With this choice IPCW is algebraically
the size-weighted average of the per-stratum Kaplan-Meier fill curves (Satten &
Datta 2001), which ``tests/python/test_ipcw.py`` checks to 1e-12. Explicit
weights are kept anyway, so a richer censoring model -- time-varying queue
position, a parametric hazard -- slots in by changing ``G`` alone.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .bias import (
    _FILL,
    DEFAULT_AHEAD_EDGES,
    DEFAULT_HORIZONS,
    fill_cdf,
    ground_truth_arrays,
    observational_arrays,
)
from .experiment import DEFAULT_BLOCK_NS, REGULAR_HOURS_NS
from .ground_truth import compute_outcomes, load_messages
from .lifetimes import extract_lifetimes
from .placements import place_matched_to_orders
from .provenance import environment, git_sha, sha256_file
from .replay import AHEAD_THRESHOLDS, CROSSING_FIELDS

#: The fill cause code shared with ``bias.observational_arrays``.
FILL = _FILL
_CENSORED_ADMIN = 0  # window-end censoring in bias.observational_arrays


def left_survival(
    durations: np.ndarray,
    event: np.ndarray,
    leaves_first: np.ndarray,
    weight: np.ndarray | None = None,
) -> np.ndarray:
    """Kaplan-Meier survival of ``event``, evaluated at each T_i-, with ties ordered.

    Observations flagged ``leaves_first`` that are tied with an event at time s
    are removed from the risk set *before* the event at s is counted. See
    ``censoring_survival_before`` for why the ordering decides correctness.
    """
    durations = np.asarray(durations, dtype=float)
    w = np.ones(len(durations)) if weight is None else np.asarray(weight, dtype=float)
    times = np.unique(durations)
    slot = np.searchsorted(times, durations)
    leaving = np.bincount(slot, weights=w, minlength=times.size)
    at_risk = w.sum() - np.concatenate(([0.0], np.cumsum(leaving)[:-1]))
    n_event = np.bincount(slot, weights=w * np.asarray(event, dtype=float), minlength=times.size)
    n_first = np.bincount(
        slot, weights=w * np.asarray(leaves_first, dtype=float), minlength=times.size
    )
    remaining = at_risk - n_first
    # remaining <= 0 only when everything at s left first, so n_event == 0 there.
    # A tolerance, because with weights these are float sums, not counts.
    live = remaining > 1e-9
    factor = np.where(live, 1.0 - n_event / np.where(live, remaining, 1.0), 1.0)
    before = np.concatenate(([1.0], np.cumprod(factor)[:-1]))
    return before[slot]


def censoring_survival_before(durations: np.ndarray, cause: np.ndarray) -> np.ndarray:
    """G(T_i-): probability each observation was still uncensored just before T_i.

    The censoring process is "cancelled or window-censored"; a fill ends
    observation of it. Evaluated at the left limit because a fill at T must
    have survived censoring *up to* T, not through it.

    **Ties decide whether the estimator is right.** The fill Kaplan-Meier
    treats a cancel tied with a fill at time s as still at risk -- it leaves
    *after* the fill. G has to use the same ordering, so its risk set at s
    excludes the fills at s:

        G jumps by (1 - c_s / (Y_s - d_s)),  not  (1 - c_s / Y_s).

    Then the product telescopes, (1 - d/Y)(1 - c/(Y - d)) = (Y - d - c) / Y,
    and S(t-) G(t-) is exactly the fraction still at risk. Using a plain
    Kaplan-Meier for G, as the first version of this function did, leaves a
    d*c/Y^2 error at every tied time; with integer durations that disagreed
    with Kaplan-Meier by 0.045 at the longest horizon, and the identity test
    caught it.
    """
    filled = np.asarray(cause) == FILL
    return left_survival(durations, ~filled, filled)


def ipcw_fill_curve(
    durations: np.ndarray,
    cause: np.ndarray,
    strata: np.ndarray,
    horizons: np.ndarray,
) -> np.ndarray:
    """IPCW fill CDF at each horizon, with a censoring model stratified on ``strata``."""
    durations = np.asarray(durations, dtype=float)
    cause = np.asarray(cause)
    strata = np.asarray(strata)
    weights = np.zeros(len(durations))
    for s in np.unique(strata):
        member = strata == s
        g = censoring_survival_before(durations[member], cause[member])
        filled = cause[member] == FILL
        # G > 0 wherever a fill is observed: a fill at T means the order was in
        # the risk set just before T, so the censoring KM cannot have hit zero.
        weights[np.flatnonzero(member)[filled]] = 1.0 / g[filled]
    return np.array([weights[durations <= h].sum() for h in horizons]) / len(durations)


#: Order-age edges for the time-varying censoring model. Cancellation hazard
#: varies over orders of magnitude with age -- fleeting orders are pulled within
#: milliseconds -- so a model on queue position alone would confound the two.
AGE_EDGES_NS = (1_000_000, 10_000_000, 100_000_000, 1_000_000_000, 10_000_000_000)
N_AGE = len(AGE_EDGES_NS) + 1
#: Queue buckets: 4 = 1000+ ahead, 3 = 100-999, 2 = 10-99, 1 = 1-9, 0 = front.
#: Bucket = 4 minus the number of AHEAD_THRESHOLDS already crossed.
N_QUEUE = len(AHEAD_THRESHOLDS) + 1
N_CELLS = N_QUEUE * N_AGE
_CANCEL = 2  # bias.observational_arrays' cancel code


def crossing_times(
    columns: dict[str, np.ndarray], arrival_ts: np.ndarray, ahead_at_arrival: np.ndarray
) -> np.ndarray:
    """(n, 4) time after arrival at which each real order's queue-ahead fell
    below 1000 / 100 / 10 / 1; +inf if it never did.

    Transitions come from the order's matched shadow, which at latency 0 sits
    in the order's own slot. The starting point comes from the order itself, so
    a shadow that never activated -- placed after the last event -- still gets
    the right initial bucket. Roughly 10% of shadows activate on a neighbouring
    same-timestamp event (limitation #10 in CURRENT-STATUS.md), so for those the
    trajectory is a close approximation, not the order's exact one.
    """
    rel = np.full((len(arrival_ts), len(CROSSING_FIELDS)), np.inf)
    for k, (threshold, field) in enumerate(zip(AHEAD_THRESHOLDS, CROSSING_FIELDS, strict=True)):
        stamp = np.asarray(columns[field])
        seen = stamp != -1
        rel[seen, k] = np.maximum(stamp[seen] - arrival_ts[seen], 0)
        rel[np.asarray(ahead_at_arrival) < threshold, k] = 0.0
    # A lower threshold cannot be crossed before a higher one.
    return np.maximum.accumulate(rel, axis=1)


def cell_exposure(
    durations: np.ndarray, crossings: np.ndarray, age_edges: Sequence[int]
) -> tuple[np.ndarray, np.ndarray]:
    """Time each order spent in each (queue bucket, age bucket) cell, and the
    cell it exited from.

    Every order's life [0, D) is cut at its four crossing times and at the age
    edges; each piece is attributed to the cell holding at its start. Fully
    vectorised: the cut points are stacked into one (n, 11) matrix, clipped to
    D and sorted along each row, so a 791k-order session needs no Python loop.
    """
    durations = np.asarray(durations, dtype=float)
    n = len(durations)
    edges = np.asarray(age_edges, dtype=float)
    cuts = np.concatenate(
        [np.zeros((n, 1)), crossings, np.broadcast_to(edges, (n, len(edges))), durations[:, None]],
        axis=1,
    )
    cuts = np.sort(np.minimum(cuts, durations[:, None]), axis=1)
    starts, lengths = cuts[:, :-1], np.diff(cuts, axis=1)

    queue = N_QUEUE - 1 - (crossings[:, None, :] <= starts[:, :, None]).sum(axis=2)
    age = (edges[None, None, :] <= starts[:, :, None]).sum(axis=2)
    cell = queue * N_AGE + age

    flat = (np.arange(n)[:, None] * N_CELLS + cell).ravel()
    exposure = np.bincount(flat, weights=lengths.ravel(), minlength=n * N_CELLS)

    # The exit happens in the cell holding just before D (left-continuous);
    # durations are whole nanoseconds, so D - 1 is the last instant of the life.
    at = np.maximum(durations - 1.0, 0.0)
    exit_queue = N_QUEUE - 1 - (crossings <= at[:, None]).sum(axis=1)
    exit_age = (edges[None, :] <= at[:, None]).sum(axis=1)
    return exposure.reshape(n, N_CELLS), exit_queue * N_AGE + exit_age


def tv_ipcw_fill_curve(
    durations: np.ndarray,
    cause: np.ndarray,
    exposure: np.ndarray,
    exit_cell: np.ndarray,
    horizons: np.ndarray,
    weight: np.ndarray | None = None,
) -> np.ndarray:
    """IPCW fill CDF with a piecewise-exponential, time-varying cancel hazard.

    lambda_c = cancels exiting from cell c / total order-time in c, the
    maximum-likelihood estimate for a hazard constant within each cell. A fill
    at T is weighted by 1 / (exp(-sum_c lambda_c * exposure_c) * G_admin(T-)).
    Window-end censoring gets its own Kaplan-Meier factor: it is deterministic
    given arrival time, hence independent of everything, and folding it into
    the cancel hazard would misattribute it to queue position.

    ``weight`` is a per-row multiplicity: a bootstrap resample passes its draw
    counts here rather than copying the exposure matrix, and the result is
    identical to replicating the rows (tested to 1e-12).
    """
    durations = np.asarray(durations, dtype=float)
    cause = np.asarray(cause)
    w = np.ones(len(durations)) if weight is None else np.asarray(weight, dtype=float)
    cancelled = cause == _CANCEL
    exposure_total = w @ exposure
    events = np.bincount(exit_cell[cancelled], weights=w[cancelled], minlength=N_CELLS)
    hazard = np.divide(events, exposure_total, out=np.zeros(N_CELLS), where=exposure_total > 0)

    filled = cause == FILL
    g_cancel = np.exp(-(exposure[filled] @ hazard))
    admin = cause == _CENSORED_ADMIN
    g_admin = left_survival(durations, admin, ~admin, w)[filled]

    contribution = np.zeros(len(durations))
    contribution[filled] = w[filled] / (g_cancel * g_admin)
    return np.array([contribution[durations <= h].sum() for h in horizons]) / float(w.sum())


def ahead_strata(
    ahead_at_arrival: np.ndarray, edges: Sequence[int] = DEFAULT_AHEAD_EDGES
) -> np.ndarray:
    """Bin index of each order's queue-ahead at arrival."""
    return np.searchsorted(np.asarray(edges), np.asarray(ahead_at_arrival), side="right")


@dataclass(frozen=True)
class IpcwComparison:
    """Truth, the naive estimator, and the IPCW correction, on one population."""

    horizons_ns: np.ndarray
    ground_truth: np.ndarray
    naive_km: np.ndarray
    ipcw: np.ndarray
    tv_ipcw: np.ndarray
    n_orders: int

    @property
    def naive_error(self) -> np.ndarray:
        return np.asarray(self.naive_km - self.ground_truth)

    @property
    def ipcw_error(self) -> np.ndarray:
        return np.asarray(self.ipcw - self.ground_truth)

    @property
    def tv_ipcw_error(self) -> np.ndarray:
        return np.asarray(self.tv_ipcw - self.ground_truth)

    @property
    def share_of_bias_removed(self) -> np.ndarray:
        """1 - |IPCW error| / |naive error|: 1 closes the gap, 0 changes nothing."""
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.asarray(1.0 - np.abs(self.ipcw_error) / np.abs(self.naive_error))


def compare_ipcw(
    events: np.ndarray,
    *,
    horizon_ns: int,
    horizons: np.ndarray,
    engine: str = "cpp",
    edges: Sequence[int] = DEFAULT_AHEAD_EDGES,
) -> IpcwComparison:
    """Matched never-cancel truth vs naive KM vs stratified IPCW, on one stream.

    Uses the same matched placement as H1: one shadow per real order, so the
    truth and both estimators describe one population.
    """
    placements = place_matched_to_orders(events, horizon_ns=horizon_ns)
    columns, _ = compute_outcomes(events, placements, engine)
    end_ts = int(events["ts_ns"][-1])

    t_dur, t_ev, _, _ = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    truth = fill_cdf(t_dur, t_ev, horizons)

    lives = extract_lifetimes(events)
    durations, cause, arrival, ahead = observational_arrays(lives, at_touch_only=False)
    naive = fill_cdf(durations, (cause == FILL).astype(float), horizons)
    corrected = ipcw_fill_curve(durations, cause, ahead_strata(ahead, edges), horizons)
    exposure, exit_cell = cell_exposure(
        durations, crossing_times(columns, arrival, ahead), AGE_EDGES_NS
    )
    return IpcwComparison(
        horizons_ns=np.asarray(horizons, dtype=np.int64),
        ground_truth=truth,
        naive_km=naive,
        ipcw=corrected,
        tv_ipcw=tv_ipcw_fill_curve(durations, cause, exposure, exit_cell, horizons),
        n_orders=len(durations),
    )


def run_ipcw(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    horizon_ns: int = 60_000_000_000,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    engine: str = "cpp",
    edges: Sequence[int] = DEFAULT_AHEAD_EDGES,
    window_ns: tuple[int, int] | None = REGULAR_HOURS_NS,
    block_ns: int = DEFAULT_BLOCK_NS,
    n_replicates: int = 200,
    seed: int = 0,
) -> dict[str, Any]:
    """Truth vs naive KM vs IPCW on one session, with paired block-bootstrap CIs.

    The censoring model is refitted inside every replicate. Holding it fixed
    at the full-sample fit would treat the weights as known and understate
    the interval.
    """
    message_path = Path(message_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events, input_format = load_messages(message_path)
    if window_ns is not None:
        events = events[(events["ts_ns"] >= window_ns[0]) & (events["ts_ns"] < window_ns[1])]
    if len(events) == 0:
        raise ValueError(f"{message_path} has no events in window {window_ns}")
    horizons = np.asarray(horizons, dtype=np.int64)

    placements = place_matched_to_orders(events, horizon_ns=horizon_ns)
    columns, diagnostics = compute_outcomes(events, placements, engine)
    end_ts = int(events["ts_ns"][-1])
    t_dur, t_ev, t_ts, _ = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    o_dur, cause, o_ts, ahead = observational_arrays(extract_lifetimes(events), at_touch_only=False)
    strata = ahead_strata(ahead, edges)
    fills = (cause == FILL).astype(float)
    exposure, exit_cell = cell_exposure(o_dur, crossing_times(columns, o_ts, ahead), AGE_EDGES_NS)

    def curves(
        ti: np.ndarray, oi: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        truth = fill_cdf(t_dur[ti], t_ev[ti], horizons)
        naive = fill_cdf(o_dur[oi], fills[oi], horizons)
        corrected = ipcw_fill_curve(o_dur[oi], cause[oi], strata[oi], horizons)
        # Draw counts as weights: identical to indexing, without copying the
        # (orders x cells) exposure matrix on every replicate.
        drawn = np.bincount(oi, minlength=len(o_dur)).astype(float)
        tv = tv_ipcw_fill_curve(o_dur, cause, exposure, exit_cell, horizons, weight=drawn)
        return truth, naive, corrected, tv

    truth, naive, corrected, tv = curves(np.arange(len(t_dur)), np.arange(len(o_dur)))

    # Paired blocks: shadows by insert time, real orders by arrival time, drawn
    # together so truth and estimate describe the same resampled periods.
    t_block, o_block = t_ts // block_ns, o_ts // block_ns
    blocks = np.union1d(np.unique(t_block), np.unique(o_block))
    if len(blocks) < 2:
        raise ValueError(f"{len(blocks)} time block(s): too few to bootstrap")
    t_members = {b: np.flatnonzero(t_block == b) for b in blocks}
    o_members = {b: np.flatnonzero(o_block == b) for b in blocks}

    rng = np.random.default_rng(seed)
    naive_err, ipcw_err, tv_err = [], [], []
    for _ in range(n_replicates):
        drawn = rng.choice(blocks, size=len(blocks), replace=True)
        ti = np.concatenate([t_members[b] for b in drawn])
        oi = np.concatenate([o_members[b] for b in drawn])
        if len(ti) == 0 or len(oi) == 0:
            continue
        rt, rn, rc, rv = curves(ti, oi)
        naive_err.append(rn - rt)
        ipcw_err.append(rc - rt)
        tv_err.append(rv - rt)
    ne, ie, ve = np.array(naive_err), np.array(ipcw_err), np.array(tv_err)

    rows = [
        {
            "horizon_ns": int(h),
            "ground_truth": float(truth[i]),
            "naive_km": float(naive[i]),
            "ipcw": float(corrected[i]),
            "naive_error": float(naive[i] - truth[i]),
            "naive_error_lo": float(np.percentile(ne[:, i], 2.5)),
            "naive_error_hi": float(np.percentile(ne[:, i], 97.5)),
            "ipcw_error": float(corrected[i] - truth[i]),
            "ipcw_error_lo": float(np.percentile(ie[:, i], 2.5)),
            "ipcw_error_hi": float(np.percentile(ie[:, i], 97.5)),
            "tv_ipcw": float(tv[i]),
            "tv_ipcw_error": float(tv[i] - truth[i]),
            "tv_ipcw_error_lo": float(np.percentile(ve[:, i], 2.5)),
            "tv_ipcw_error_hi": float(np.percentile(ve[:, i], 97.5)),
            "n_shadows": len(t_dur),
            "n_orders": len(o_dur),
        }
        for i, h in enumerate(horizons)
    ]

    manifest: dict[str, Any] = {
        "git_sha": git_sha(),
        "experiment": "ipcw",
        "input_path": str(message_path),
        "input_format": input_format,
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "engine": engine,
        "environment": environment(),
        "diagnostics": diagnostics,
        "config": {
            "censoring_model": "stratified Kaplan-Meier on queue-ahead at arrival",
            "tv_censoring_model": (
                "piecewise-exponential cancel hazard over current queue-ahead bucket "
                "x order age; window-end censoring by Kaplan-Meier"
            ),
            "age_edges_ns": list(AGE_EDGES_NS),
            "ahead_edges": [int(e) for e in edges],
            "horizon_ns": horizon_ns,
            "horizons_ns": [int(h) for h in horizons],
            "window_ns": list(window_ns) if window_ns else None,
            "placement": "matched",
            "block_ns": block_ns,
            "n_replicates": len(ne),
            "n_blocks": len(blocks),
            "seed": seed,
        },
        "bootstrap_scope": "within-session blocks only; censoring model refitted per replicate",
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def format_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        f"{'horizon':>8}  {'truth':>7}  {'KM err':>8}  {'IPCW err':>9}  "
        f"{'TV-IPCW err':>11}  {'TV 95% CI':>20}"
    ]
    for r in rows:
        h = r["horizon_ns"] / 1e9
        label = f"{h:g}s" if h >= 1 else f"{h * 1000:g}ms"
        lines.append(
            f"{label:>8}  {r['ground_truth']:>7.4f}  {r['naive_error']:>+8.4f}  "
            f"{r['ipcw_error']:>+9.4f}  {r['tv_ipcw_error']:>+11.4f}  "
            f"[{r['tv_ipcw_error_lo']:+.4f}, {r['tv_ipcw_error_hi']:+.4f}]"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="IPCW correction measured against ground truth")
    parser.add_argument("--message-path", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--n-replicates", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    manifest = run_ipcw(
        message_path=args.message_path,
        out_dir=args.out_dir,
        n_replicates=args.n_replicates,
        seed=args.seed,
    )
    print(format_table(manifest["rows"]))


if __name__ == "__main__":
    main()
