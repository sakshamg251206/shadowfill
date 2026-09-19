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

from dataclasses import dataclass
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
    status = np.asarray(columns["status"])
    insert_ts = np.asarray(columns["insert_ts"])
    first_fill_ts = np.asarray(columns["first_fill_ts"])

    keep = status != int(Status.NOT_ACTIVATED)
    if not keep.any():
        raise ValueError("no activated placements")
    status, insert_ts, first_fill_ts = status[keep], insert_ts[keep], first_fill_ts[keep]

    traded = first_fill_ts >= 0
    censor_at = np.where(
        status == int(Status.TRUNCATED),
        np.maximum(end_ts - insert_ts, 0),
        horizon_ns,
    )
    durations = np.where(traded, first_fill_ts - insert_ts, censor_at).astype(float)
    times, surv = kaplan_meier(durations, traded.astype(float))
    return 1.0 - step_at(times, surv, horizons, initial=1.0)


def _observational_arrays(
    lives: np.ndarray, *, at_touch_only: bool
) -> tuple[np.ndarray, np.ndarray]:
    """(durations, cause) for real orders, optionally only those at the touch."""
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
    return durations, cause


def fill_curves_observational(
    lives: np.ndarray,
    *,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    at_touch_only: bool = True,
) -> tuple[np.ndarray, np.ndarray, int]:
    """(naive Kaplan-Meier, Aalen-Johansen incidence, n) for real orders."""
    durations, cause = _observational_arrays(lives, at_touch_only=at_touch_only)

    km_times, surv = kaplan_meier(durations, (cause == _FILL).astype(float))
    naive = 1.0 - step_at(km_times, surv, horizons, initial=1.0)

    aj_times, incidence = aalen_johansen(durations, cause, cause=_FILL)
    correct = step_at(aj_times, incidence, horizons, initial=0.0)
    return naive, correct, len(durations)


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
