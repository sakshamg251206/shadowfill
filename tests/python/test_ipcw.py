"""IPCW: reweighting the observational fill curve for informative cancellation.

The first three tests are mathematics, not measurements. With a stratified
Kaplan-Meier censoring model, IPCW is *algebraically* equal to the size-weighted
average of the per-stratum Kaplan-Meier fill curves (Satten & Datta 2001). So an
implementation that disagrees with that identity is wrong, whatever it measures.
Integer durations are used on purpose, to force ties between fills and cancels.
"""

from __future__ import annotations

import numpy as np
import pytest

from shadowfill.bias import fill_cdf
from shadowfill.ipcw import FILL, ipcw_fill_curve

CANCEL, CENSORED = 2, 0
HORIZONS = np.array([1.0, 3.0, 7.0, 15.0, 40.0])


def random_population(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    durations = rng.integers(1, 30, size=n).astype(float)
    cause = rng.choice([FILL, CANCEL, CENSORED], size=n, p=[0.4, 0.45, 0.15])
    return durations, cause


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_one_stratum_ipcw_is_exactly_kaplan_meier(seed):
    durations, cause = random_population(2_000, seed)
    strata = np.zeros(len(durations), dtype=int)
    expected = fill_cdf(durations, (cause == FILL).astype(float), HORIZONS)
    np.testing.assert_allclose(
        ipcw_fill_curve(durations, cause, strata, HORIZONS), expected, rtol=0, atol=1e-12
    )


@pytest.mark.parametrize("seed", [3, 4])
def test_stratified_ipcw_is_the_size_weighted_average_of_stratum_km(seed):
    durations, cause = random_population(3_000, seed)
    strata = np.random.default_rng(seed + 100).integers(0, 4, size=len(durations))
    expected = sum(
        np.mean(strata == s)
        * fill_cdf(durations[strata == s], (cause[strata == s] == FILL).astype(float), HORIZONS)
        for s in np.unique(strata)
    )
    np.testing.assert_allclose(
        ipcw_fill_curve(durations, cause, strata, HORIZONS), expected, rtol=0, atol=1e-12
    )


def test_without_any_censoring_ipcw_is_the_empirical_cdf():
    durations = np.array([1.0, 2.0, 2.0, 5.0, 9.0, 20.0])
    cause = np.full(6, FILL)
    strata = np.zeros(6, dtype=int)
    empirical = np.array([np.mean(durations <= h) for h in HORIZONS])
    np.testing.assert_allclose(ipcw_fill_curve(durations, cause, strata, HORIZONS), empirical)


def test_a_curve_is_monotone_and_inside_the_unit_interval():
    durations, cause = random_population(2_000, 9)
    strata = np.random.default_rng(9).integers(0, 5, size=len(durations))
    curve = ipcw_fill_curve(durations, cause, strata, HORIZONS)
    assert np.all(np.diff(curve) >= 0)
    assert np.all((curve >= 0) & (curve <= 1))


def test_placebo_ipcw_agrees_with_the_never_cancel_truth():
    """On the placebo stream cancellation is independent by construction, so the
    corrected estimator, like the naive one, must sit on the truth."""
    pytest.importorskip("shadowfill._core")
    from shadowfill.ipcw import compare_ipcw
    from shadowfill.synthetic import generate_synthetic_messages

    sec = 1_000_000_000
    events = generate_synthetic_messages(n_events=60_000, seed=101)
    result = compare_ipcw(events, horizon_ns=5 * sec, horizons=np.array([sec, 5 * sec]))
    assert np.max(np.abs(result.ipcw - result.ground_truth)) < 0.02
