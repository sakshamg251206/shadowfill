"""H1: how far the observational estimators sit from the never-cancel truth.

Three curves at the same horizons, over the same book:

``ground_truth``
    F*(h), computed by queue arithmetic from shadow orders that never cancel.
    Its only censoring is the recording window ending, which really is
    independent of the outcome, so a product-limit estimate of it is honest.

``naive_km``
    Kaplan-Meier on real resting orders with cancellation treated as
    censoring. This is what the survival literature and every L2 backtest
    effectively assume.

``competing_risks``
    Aalen-Johansen cumulative incidence on the same real orders, with
    cancellation as a competing event rather than censoring. Correct for the
    observational question, and still not the answer to the counterfactual
    one, because it describes fills under *their* cancellation policies.

**Comparability is the whole game.** Both sides use the same event definition
-- the order traded at all, at its ``first_fill_ts`` -- and by default the
observational population is restricted to orders arriving at the touch,
because shadow orders are placed at the prevailing best price. Comparing
top-of-book shadows against real orders resting five levels deep would show a
large gap that is mostly depth, not bias.

What remains uncontrolled is stated rather than hidden: real orders differ
from the shadow grid in size and in arrival timing, both of which correlate
with fill probability. Stratifying on ``ahead_at_arrival`` removes the largest
part of that, which is what ``ahead_bins`` is for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np

from .estimators import aalen_johansen, kaplan_meier, step_at
from .lifetimes import NO_FILL, ExitReason
from .replay import Status

#: The horizons H1 is pre-registered at, in nanoseconds.
DEFAULT_HORIZONS = np.array([100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000], "int64")

_CENSORED, _FILL, _CANCEL = 0, 1, 2


@dataclass(frozen=True)
class CurveComparison:
    """One stratum's worth of the H1 measurement."""

    horizons_ns: np.ndarray
    ground_truth: np.ndarray
    naive_km: np.ndarray
    competing_risks: np.ndarray
    n_shadows: int
    n_orders: int
    stratum: str = "all"
    #: (lo, hi) queue-ahead band this stratum covers, hi < 0 meaning open-ended.
    #: None for the unconditional comparison.
    ahead_range: tuple[int, int] | None = None

    @property
    def naive_error(self) -> np.ndarray:
        """F̂_KM - F*. Positive means the naive estimator promises too many fills."""
        return np.asarray(self.naive_km - self.ground_truth)

    @property
    def competing_risks_error(self) -> np.ndarray:
        return np.asarray(self.competing_risks - self.ground_truth)

    def as_rows(self) -> list[dict[str, Any]]:
        """Flatten to one record per horizon, for a manifest or a table."""
        return [
            {
                "stratum": self.stratum,
                "horizon_ns": int(h),
                "ground_truth": float(self.ground_truth[i]),
                "naive_km": float(self.naive_km[i]),
                "competing_risks": float(self.competing_risks[i]),
                "naive_error": float(self.naive_error[i]),
                "competing_risks_error": float(self.competing_risks_error[i]),
                "n_shadows": self.n_shadows,
                "n_orders": self.n_orders,
            }
            for i, h in enumerate(self.horizons_ns)
        ]


def ground_truth_arrays(
    columns: dict[str, np.ndarray], *, horizon_ns: int, end_ts: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(durations, traded, insert_ts, ahead_at_insert) for activated shadows.

    Separated from the curve so a bootstrap can resample the observations
    directly instead of rebuilding them from the outcome table B times.
    """
    status = np.asarray(columns["status"])
    insert_ts = np.asarray(columns["insert_ts"])
    first_fill_ts = np.asarray(columns["first_fill_ts"])

    keep = status != int(Status.NOT_ACTIVATED)
    if not keep.any():
        raise ValueError("no activated placements")
    ahead = np.asarray(columns["ahead_at_insert"])[keep]
    status, insert_ts, first_fill_ts = status[keep], insert_ts[keep], first_fill_ts[keep]

    traded = first_fill_ts >= 0
    censor_at = np.where(
        status == int(Status.TRUNCATED), np.maximum(end_ts - insert_ts, 0), horizon_ns
    )
    durations = np.where(traded, first_fill_ts - insert_ts, censor_at).astype(float)
    return durations, traded.astype(float), insert_ts, ahead


def fill_curve_ground_truth(
    columns: dict[str, np.ndarray],
    *,
    horizon_ns: int,
    end_ts: int,
    horizons: np.ndarray = DEFAULT_HORIZONS,
) -> np.ndarray:
    """F*(h) from shadow outcomes.

    ``columns`` is the outcome table the runner builds, from either engine.

    A shadow that traded at all is an event at ``first_fill_ts``, whatever its
    final status: a partial fill is still a fill, and requiring the full size
    would answer a different question from the observational side. One that
    never traded is censored -- at its horizon if it expired, at the end of the
    stream if the recording stopped first. NOT_ACTIVATED rows are dropped,
    because an order that never existed must not sit in the denominator.
    """
    durations, traded, _, _ = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    return fill_cdf(durations, traded, horizons)


def fill_cdf(durations: np.ndarray, events: np.ndarray, horizons: np.ndarray) -> np.ndarray:
    """1 - S(h): the fill probability by each horizon, from a product-limit fit."""
    times, surv = kaplan_meier(durations, events)
    return 1.0 - step_at(times, surv, horizons, initial=1.0)


def observational_arrays(
    lives: np.ndarray, *, at_touch_only: bool = True
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(durations, cause, arrival_ts, ahead_at_arrival), optionally only at the touch."""
    if at_touch_only:
        touch = np.where(
            lives["side"] > 0, lives["best_bid_at_arrival"], lives["best_ask_at_arrival"]
        )
        # A best price of 0 means that side was empty, so there was no touch to
        # arrive at and the order cannot be compared with a shadow.
        lives = lives[(touch != 0) & (lives["price"] == touch)]
    if len(lives) == 0:
        raise ValueError("no orders left after filtering")

    traded = lives["first_fill_ts"] != NO_FILL
    durations = np.where(
        traded,
        lives["first_fill_ts"] - lives["arrival_ts"],
        lives["exit_ts"] - lives["arrival_ts"],
    ).astype(float)
    cause = np.where(
        traded,
        _FILL,
        np.where(lives["exit_reason"] == int(ExitReason.CANCELLED), _CANCEL, _CENSORED),
    )
    return durations, cause, np.asarray(lives["arrival_ts"]), np.asarray(lives["ahead_at_arrival"])


def fill_curves_observational(
    lives: np.ndarray,
    *,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    at_touch_only: bool = True,
) -> tuple[np.ndarray, np.ndarray, int]:
    """(naive Kaplan-Meier, Aalen-Johansen incidence, n) for real orders."""
    durations, cause, _, _ = observational_arrays(lives, at_touch_only=at_touch_only)
    naive, correct = _observational_from_arrays(durations, cause, horizons)
    return naive, correct, len(durations)


def _observational_from_arrays(
    durations: np.ndarray, cause: np.ndarray, horizons: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    naive = fill_cdf(durations, (cause == _FILL).astype(float), horizons)
    aj_times, incidence = aalen_johansen(durations, cause, cause=_FILL)
    return naive, step_at(aj_times, incidence, horizons, initial=0.0)


def compare_fill_curves(
    columns: dict[str, np.ndarray],
    lives: np.ndarray,
    *,
    horizon_ns: int,
    end_ts: int,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    at_touch_only: bool = True,
    stratum: str = "all",
) -> CurveComparison:
    """Put the three curves side by side at the same horizons."""
    horizons = np.asarray(horizons, dtype=np.int64)
    truth = fill_curve_ground_truth(
        columns, horizon_ns=horizon_ns, end_ts=end_ts, horizons=horizons
    )
    naive, correct, n_orders = fill_curves_observational(
        lives, horizons=horizons, at_touch_only=at_touch_only
    )
    n_shadows = int((np.asarray(columns["status"]) != int(Status.NOT_ACTIVATED)).sum())
    return CurveComparison(
        horizons_ns=horizons,
        ground_truth=truth,
        naive_km=naive,
        competing_risks=correct,
        n_shadows=n_shadows,
        n_orders=n_orders,
        stratum=stratum,
    )


def _blocks(timestamps: np.ndarray, block_ns: int) -> np.ndarray:
    return np.asarray(timestamps, dtype=np.int64) // block_ns


def block_bootstrap_errors(
    columns: dict[str, np.ndarray],
    lives: np.ndarray,
    *,
    horizon_ns: int,
    end_ts: int,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    at_touch_only: bool = True,
    block_ns: int = 1_800_000_000_000,
    n_replicates: int = 200,
    seed: int = 0,
    alpha: float = 0.05,
    ahead_range: tuple[int, int] | None = None,
) -> dict[str, np.ndarray]:
    """Percentile confidence intervals for the estimator errors.

    Resampling unit is a **contiguous block of time**, never an individual
    order. Orders within a session are strongly dependent -- they queue behind
    each other, and a single trade settles many of them at once -- so an
    order-level bootstrap would treat that shared fate as independent evidence
    and report an interval several times too narrow.

    Shadows and real orders are resampled with the *same* drawn blocks, because
    the quantity of interest is a difference measured over one period. Drawing
    them independently would break the pairing and inflate the interval with
    variation that cancels in the estimate.

    **This is a within-session bootstrap.** The research spec asks for blocks
    over sessions, which needs more than one day of data; until then this
    quantifies within-day sampling variation only and says nothing about
    day-to-day variation. Reported as such rather than dressed up as the
    stronger interval.
    """
    gt_dur, gt_ev, gt_ts, gt_ahead = ground_truth_arrays(
        columns, horizon_ns=horizon_ns, end_ts=end_ts
    )
    obs_dur, obs_cause, obs_ts, obs_ahead = observational_arrays(lives, at_touch_only=at_touch_only)

    if ahead_range is not None:
        # Same band applied to both populations, so the interval belongs to the
        # same stratum as the point estimate it is reported beside.
        lo, hi = ahead_range
        gt_keep = gt_ahead >= lo if hi < 0 else (gt_ahead >= lo) & (gt_ahead < hi)
        obs_keep = obs_ahead >= lo if hi < 0 else (obs_ahead >= lo) & (obs_ahead < hi)
        gt_dur, gt_ev, gt_ts = gt_dur[gt_keep], gt_ev[gt_keep], gt_ts[gt_keep]
        obs_dur, obs_cause, obs_ts = obs_dur[obs_keep], obs_cause[obs_keep], obs_ts[obs_keep]
        if len(gt_dur) == 0 or len(obs_dur) == 0:
            raise ValueError(f"no observations in ahead range {ahead_range}")

    gt_block = _blocks(gt_ts, block_ns)
    obs_block = _blocks(obs_ts, block_ns)
    block_ids = np.union1d(np.unique(gt_block), np.unique(obs_block))
    if len(block_ids) < 2:
        raise ValueError(
            f"{len(block_ids)} time block(s) of {block_ns} ns: too few to bootstrap; "
            "use a shorter block_ns or a longer session"
        )

    gt_members = {b: np.flatnonzero(gt_block == b) for b in block_ids}
    obs_members = {b: np.flatnonzero(obs_block == b) for b in block_ids}

    rng = np.random.default_rng(seed)
    naive_errors, cr_errors = [], []
    for _ in range(n_replicates):
        drawn = rng.choice(block_ids, size=len(block_ids), replace=True)
        gt_idx = np.concatenate([gt_members[b] for b in drawn])
        obs_idx = np.concatenate([obs_members[b] for b in drawn])
        if len(gt_idx) == 0 or len(obs_idx) == 0:
            continue
        truth = fill_cdf(gt_dur[gt_idx], gt_ev[gt_idx], horizons)
        naive, correct = _observational_from_arrays(obs_dur[obs_idx], obs_cause[obs_idx], horizons)
        naive_errors.append(naive - truth)
        cr_errors.append(correct - truth)

    pct_lo, pct_hi = 100 * alpha / 2, 100 * (1 - alpha / 2)
    naive_arr, cr_arr = np.array(naive_errors), np.array(cr_errors)
    return {
        "n_replicates": np.array(len(naive_arr)),
        "n_blocks": np.array(len(block_ids)),
        "naive_error_lo": np.percentile(naive_arr, pct_lo, axis=0),
        "naive_error_hi": np.percentile(naive_arr, pct_hi, axis=0),
        "competing_risks_error_lo": np.percentile(cr_arr, pct_lo, axis=0),
        "competing_risks_error_hi": np.percentile(cr_arr, pct_hi, axis=0),
    }


#: Queue-ahead strata, in shares. A shadow at the front of an empty level and
#: one behind 5,000 shares are not the same experiment, and the marginal
#: populations differ sharply: shadows are placed on a clock, real orders
#: arrive when their sender chooses. Comparing them unconditionally mixes that
#: composition difference into the bias. Open-ended at the top.
DEFAULT_AHEAD_EDGES = (0, 1, 10, 100, 1_000)


def _stratum_label(lo: int, hi: int | None) -> str:
    if hi is None:
        return f"ahead>={lo}"
    if hi - lo == 1:
        return f"ahead={lo}"
    return f"ahead={lo}-{hi - 1}"


def compare_by_ahead(
    columns: dict[str, np.ndarray],
    lives: np.ndarray,
    *,
    horizon_ns: int,
    end_ts: int,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    at_touch_only: bool = True,
    edges: Sequence[int] = DEFAULT_AHEAD_EDGES,
    min_observations: int = 50,
) -> list[CurveComparison]:
    """The H1 comparison within bands of queue-ahead at placement.

    Queue position is the dominant determinant of whether a passive order
    fills, and it is exactly where the two populations differ most: shadows
    are placed on a fixed clock regardless of how deep the queue happens to
    be, while a real order is sent when someone wanted to send it. An
    unconditional comparison therefore blends that composition difference into
    the measured bias.

    ``ahead_at_insert`` and ``ahead_at_arrival`` are the same quantity by
    construction (see ``lifetimes``), which is what makes the strata
    comparable at all.

    Strata with fewer than ``min_observations`` on either side are omitted
    rather than reported noisily; the caller sees which survived.
    """
    gt_dur, gt_ev, _, gt_ahead = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    obs_dur, obs_cause, _, obs_ahead = observational_arrays(lives, at_touch_only=at_touch_only)

    bounds = [(int(lo), int(hi)) for lo, hi in pairwise(edges)]
    bounds.append((int(edges[-1]), -1))

    out: list[CurveComparison] = []
    for lo, hi in bounds:
        gt_mask = gt_ahead >= lo if hi < 0 else (gt_ahead >= lo) & (gt_ahead < hi)
        obs_mask = obs_ahead >= lo if hi < 0 else (obs_ahead >= lo) & (obs_ahead < hi)
        n_gt, n_obs = int(gt_mask.sum()), int(obs_mask.sum())
        if n_gt < min_observations or n_obs < min_observations:
            continue
        truth = fill_cdf(gt_dur[gt_mask], gt_ev[gt_mask], horizons)
        naive, correct = _observational_from_arrays(
            obs_dur[obs_mask], obs_cause[obs_mask], horizons
        )
        out.append(
            CurveComparison(
                horizons_ns=np.asarray(horizons, dtype=np.int64),
                ground_truth=truth,
                naive_km=naive,
                competing_risks=correct,
                n_shadows=n_gt,
                n_orders=n_obs,
                stratum=_stratum_label(lo, None if hi < 0 else hi),
                ahead_range=(lo, hi),
            )
        )
    return out
