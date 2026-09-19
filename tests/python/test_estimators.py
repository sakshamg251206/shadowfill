"""Survival estimators, checked against hand-computable cases.

These are the estimators H1 accuses of being biased, so they have to be right.
A bug here would produce a large, publishable, wrong gap.
"""

from __future__ import annotations

import numpy as np
import pytest

from shadowfill.estimators import aalen_johansen, kaplan_meier, step_at

CENSORED, FILL, CANCEL = 0, 1, 2


def test_with_no_censoring_kaplan_meier_is_the_empirical_cdf():
    durations = np.array([1, 2, 3, 4], dtype=float)
    events = np.ones(4, dtype=float)
    times, surv = kaplan_meier(durations, events)
    np.testing.assert_allclose(surv, [0.75, 0.5, 0.25, 0.0])
    np.testing.assert_allclose(times, [1, 2, 3, 4])


def test_kaplan_meier_matches_a_worked_example():
    """Textbook four-observation case, computed by hand.

    times 1(event), 2(censored), 3(event), 4(event):
      t=1: at risk 4, 1 event -> S = 3/4
      t=2: censored, no factor
      t=3: at risk 2, 1 event -> S = 3/4 * 1/2 = 3/8
      t=4: at risk 1, 1 event -> S = 0
    """
    durations = np.array([1, 2, 3, 4], dtype=float)
    events = np.array([1, 0, 1, 1], dtype=float)
    _, surv = kaplan_meier(durations, events)
    np.testing.assert_allclose(surv, [0.75, 0.75, 0.375, 0.0])


def test_ties_are_handled_at_the_same_risk_set():
    """Two events at one time must share the risk set, not be sequenced."""
    durations = np.array([1, 1, 2, 3], dtype=float)
    events = np.ones(4, dtype=float)
    times, surv = kaplan_meier(durations, events)
    np.testing.assert_allclose(times, [1, 2, 3])
    np.testing.assert_allclose(surv, [0.5, 0.25, 0.0])


def test_survival_is_non_increasing_and_bounded():
    rng = np.random.default_rng(0)
    durations = rng.exponential(5, size=500)
    events = (rng.random(500) < 0.7).astype(float)
    _, surv = kaplan_meier(durations, events)
    assert np.all(np.diff(surv) <= 1e-12)
    assert np.all((surv >= 0) & (surv <= 1))


def test_censoring_after_the_last_event_leaves_survival_above_zero():
    """The estimator must stop, not extrapolate to certainty."""
    durations = np.array([1, 2, 3], dtype=float)
    events = np.array([1, 0, 0], dtype=float)
    _, surv = kaplan_meier(durations, events)
    assert surv[-1] == pytest.approx(2 / 3)


def test_step_lookup_is_right_continuous():
    times = np.array([1.0, 3.0, 7.0])
    values = np.array([0.9, 0.5, 0.1])
    got = step_at(times, values, np.array([0.5, 1.0, 2.0, 3.0, 6.9, 7.0, 99.0]), initial=1.0)
    np.testing.assert_allclose(got, [1.0, 0.9, 0.9, 0.5, 0.5, 0.1, 0.1])


def test_with_one_cause_aalen_johansen_is_one_minus_kaplan_meier():
    durations = np.array([1, 2, 3, 4], dtype=float)
    causes = np.array([FILL, CENSORED, FILL, FILL], dtype=int)
    km_times, surv = kaplan_meier(durations, (causes == FILL).astype(float))
    aj_times, cif = aalen_johansen(durations, causes, cause=FILL)
    np.testing.assert_allclose(aj_times, km_times)
    np.testing.assert_allclose(cif, 1 - surv, atol=1e-12)


def test_cumulative_incidences_and_survival_partition_the_probability():
    """CIF_fill(t) + CIF_cancel(t) + S_any(t) = 1 at every observed time."""
    rng = np.random.default_rng(3)
    durations = rng.exponential(4, size=400)
    causes = rng.choice([CENSORED, FILL, CANCEL], size=400, p=[0.2, 0.4, 0.4])
    _times, cif_fill = aalen_johansen(durations, causes, cause=FILL)
    _, cif_cancel = aalen_johansen(durations, causes, cause=CANCEL)
    _, surv_any = kaplan_meier(durations, (causes != CENSORED).astype(float))
    np.testing.assert_allclose(cif_fill + cif_cancel + surv_any, 1.0, atol=1e-10)


def test_kaplan_meier_overstates_fills_when_cancellation_competes():
    """The mechanism H1 is about, reproduced in miniature.

    Treating a cancellation as censoring asserts that the order *would have*
    filled eventually at the rate of the orders still resting. It did not: it
    left. So the naive estimator reads high, and the gap grows with how much
    cancelling there is. This is the direction the real measurement has to
    beat, which is why it is pinned here.
    """
    rng = np.random.default_rng(11)
    n = 2_000
    durations = rng.exponential(3, size=n)
    causes = rng.choice([FILL, CANCEL], size=n, p=[0.3, 0.7])

    km_times, surv = kaplan_meier(durations, (causes == FILL).astype(float))
    naive = 1 - surv
    aj_times, correct = aalen_johansen(durations, causes, cause=FILL)

    np.testing.assert_allclose(km_times, aj_times)
    assert np.all(naive >= correct - 1e-12)
    # and the overstatement is material, not a rounding artefact
    assert naive[-1] - correct[-1] > 0.05


def test_empty_input_is_an_error_not_an_empty_answer():
    with pytest.raises(ValueError, match="no observations"):
        kaplan_meier(np.array([]), np.array([]))


def test_mismatched_lengths_are_rejected():
    with pytest.raises(ValueError, match="same length"):
        kaplan_meier(np.array([1.0, 2.0]), np.array([1.0]))
