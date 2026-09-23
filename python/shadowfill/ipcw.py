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

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .bias import _FILL, DEFAULT_AHEAD_EDGES, fill_cdf, observational_arrays
from .ground_truth import compute_outcomes
from .lifetimes import extract_lifetimes
from .placements import place_matched_to_orders

#: The fill cause code shared with ``bias.observational_arrays``.
FILL = _FILL


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
    durations = np.asarray(durations, dtype=float)
    times = np.unique(durations)
    slot = np.searchsorted(times, durations)
    leaving = np.bincount(slot, minlength=times.size)
    at_risk = len(durations) - np.concatenate(([0], np.cumsum(leaving)[:-1]))
    fills = np.bincount(slot, weights=(cause == FILL).astype(float), minlength=times.size)
    censors = leaving - fills
    remaining = at_risk - fills
    # remaining == 0 only when every observation at s filled, so censors == 0.
    factor = np.where(remaining > 0, 1.0 - censors / np.where(remaining > 0, remaining, 1), 1.0)
    after = np.cumprod(factor)
    before = np.concatenate(([1.0], after[:-1]))
    return before[slot]


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
    n_orders: int

    @property
    def naive_error(self) -> np.ndarray:
        return np.asarray(self.naive_km - self.ground_truth)

    @property
    def ipcw_error(self) -> np.ndarray:
        return np.asarray(self.ipcw - self.ground_truth)

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

    from .bias import ground_truth_arrays

    t_dur, t_ev, _, _ = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    truth = fill_cdf(t_dur, t_ev, horizons)

    durations, cause, _, ahead = observational_arrays(
        extract_lifetimes(events), at_touch_only=False
    )
    naive = fill_cdf(durations, (cause == FILL).astype(float), horizons)
    corrected = ipcw_fill_curve(durations, cause, ahead_strata(ahead, edges), horizons)
    return IpcwComparison(
        horizons_ns=np.asarray(horizons, dtype=np.int64),
        ground_truth=truth,
        naive_km=naive,
        ipcw=corrected,
        n_orders=len(durations),
    )
