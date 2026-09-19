"""Non-parametric survival estimators, in numpy.

These are written out rather than imported because the comparison between them
*is* the result. H1 claims that the naive estimator -- Kaplan-Meier with
cancellation treated as censoring -- overstates fill probability, and the claim
is only as trustworthy as the arithmetic behind both sides of it. A reader who
wants to check the number should be able to read the twenty lines that produced
it, and the tie-handling and risk-set conventions should be visible rather than
inherited from a library's defaults.

Two estimators:

**Kaplan-Meier** estimates S(t) = P(no event by t) by chaining conditional
survival across observed event times::

    S(t) = prod over t_i <= t of (1 - d_i / n_i)

with d_i events and n_i at risk at t_i. It assumes censoring is *independent*
of the outcome -- that an order withdrawn at time t was no more or less likely
to fill than the orders still resting. For real limit orders that assumption is
false in a specific, directional way, which is the whole subject of this
project.

**Aalen-Johansen** cumulative incidence handles competing risks properly::

    CIF_k(t) = sum over t_i <= t of S(t_(i-1)) * d_k(t_i) / n_i

The difference is the factor S(t_(i-1)), the probability of still being at risk
at all. 1 - S_KM answers "what fraction would fill if cancellation could be
undone"; CIF answers "what fraction actually fill", which is the quantity a
desk cares about. Under competing risks the first is always >= the second.
"""

from __future__ import annotations

import numpy as np


def _checked(durations: np.ndarray, marks: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    durations = np.asarray(durations, dtype=float)
    marks = np.asarray(marks)
    if durations.size == 0:
        raise ValueError("no observations")
    if durations.shape != marks.shape:
        raise ValueError("durations and event marks must be the same length")
    if np.any(durations < 0):
        raise ValueError("durations must be non-negative")
    order = np.argsort(durations, kind="stable")
    return durations[order], marks[order]


def _risk_sets(durations: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Unique observed times, the index of each observation, and the risk set.

    The risk set at t_i counts everything not yet removed *before* t_i, so
    observations tied at t_i are all still at risk -- ties share one risk set
    rather than being sequenced arbitrarily, which would bias the estimate.
    """
    times = np.unique(durations)
    slot = np.searchsorted(times, durations)
    leaving = np.bincount(slot, minlength=times.size)
    at_risk = durations.size - np.concatenate(([0], np.cumsum(leaving)[:-1]))
    return times, slot, at_risk


def kaplan_meier(durations: np.ndarray, events: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Product-limit estimate of S(t).

    ``events`` is 1 where the event of interest was observed and 0 where the
    observation was censored. Returns (observed times, S at each time).
    """
    durations, events = _checked(durations, events)
    times, slot, at_risk = _risk_sets(durations)
    n_events = np.bincount(slot, weights=events.astype(float), minlength=times.size)
    return times, np.cumprod(1.0 - n_events / at_risk)


def aalen_johansen(
    durations: np.ndarray, causes: np.ndarray, *, cause: int
) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative incidence of ``cause`` under competing risks.

    ``causes`` is 0 for censored and a positive integer per competing cause.
    Returns (observed times, CIF at each time).
    """
    durations, causes = _checked(durations, causes)
    times, slot, at_risk = _risk_sets(durations)

    n_cause = np.bincount(slot, weights=(causes == cause).astype(float), minlength=times.size)
    n_any = np.bincount(slot, weights=(causes != 0).astype(float), minlength=times.size)

    overall = np.cumprod(1.0 - n_any / at_risk)
    # S(t_(i-1)): survival just *before* each event time. Using S(t_i) here is
    # the classic off-by-one and silently deflates every incidence.
    prior = np.concatenate(([1.0], overall[:-1]))
    return times, np.cumsum(prior * n_cause / at_risk)


def step_at(
    times: np.ndarray, values: np.ndarray, query: np.ndarray, *, initial: float
) -> np.ndarray:
    """Evaluate a right-continuous step function at ``query``.

    Both estimators are step functions that jump at observed times and are
    constant in between, so the value at h is the value at the last observed
    time <= h. ``initial`` is what holds before the first one: 1.0 for a
    survival curve, 0.0 for a cumulative incidence.
    """
    query = np.asarray(query, dtype=float)
    idx = np.searchsorted(times, query, side="right") - 1
    return np.where(idx < 0, initial, values[np.clip(idx, 0, None)])
