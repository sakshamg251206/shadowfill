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


def test_run_writes_a_manifest_with_truth_naive_and_ipcw_intervals(tmp_path):
    pytest.importorskip("shadowfill._core")
    import json

    from shadowfill.ipcw import run_ipcw
    from shadowfill.synthetic import generate_synthetic_messages, write_synthetic_csv

    sec = 1_000_000_000
    events = generate_synthetic_messages(n_events=30_000, seed=5, informed_cancel=0.9)
    data_path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, data_path)

    manifest = run_ipcw(
        message_path=data_path,
        out_dir=tmp_path / "out",
        horizon_ns=5 * sec,
        horizons=np.array([sec, 5 * sec], dtype=np.int64),
        window_ns=None,
        block_ns=2 * sec,
        n_replicates=30,
    )
    on_disk = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert on_disk["experiment"] == "ipcw"
    assert len(on_disk["input_sha256"]) == 64
    for row in manifest["rows"]:
        for key in (
            "naive_error",
            "naive_error_lo",
            "naive_error_hi",
            "ipcw_error",
            "ipcw_error_lo",
            "ipcw_error_hi",
            "tv_ipcw_error",
            "tv_ipcw_error_lo",
            "tv_ipcw_error_hi",
        ):
            assert key in row
        assert row["naive_error_lo"] <= row["naive_error"] <= row["naive_error_hi"]
        assert row["ipcw_error_lo"] <= row["ipcw_error"] <= row["ipcw_error_hi"]
        assert row["tv_ipcw_error_lo"] <= row["tv_ipcw_error"] <= row["tv_ipcw_error_hi"]
    assert manifest["config"]["censoring_model"].startswith("stratified")
    assert "piecewise-exponential" in manifest["config"]["tv_censoring_model"]


# --- Time-varying IPCW: the censoring hazard depends on where an order is in
# its queue *now*, via the first-passage times the engine records.

MS = 1_000_000


def test_cell_exposure_splits_a_life_at_every_crossing_and_age_edge():
    """Worked by hand. Arrived with 500 ahead (bucket 3: 100-999), dropped
    below 100 at 2 s (bucket 2), cancelled at 5 s."""
    from shadowfill.ipcw import AGE_EDGES_NS, N_AGE, cell_exposure

    durations = np.array([5_000 * MS], dtype=float)
    inf = np.inf
    crossings = np.array([[0.0, 2_000 * MS, inf, inf]])  # <1000, <100, <10, <1
    exposure, exit_cell = cell_exposure(durations, crossings, AGE_EDGES_NS)

    def cell(q, g):
        return q * N_AGE + g

    expected = np.zeros(exposure.shape[1])
    expected[cell(3, 0)] = 1 * MS  # [0, 1 ms)
    expected[cell(3, 1)] = 9 * MS  # [1, 10 ms)
    expected[cell(3, 2)] = 90 * MS  # [10, 100 ms)
    expected[cell(3, 3)] = 900 * MS  # [100 ms, 1 s)
    expected[cell(3, 4)] = 1_000 * MS  # [1 s, 2 s), still >= 100 ahead
    expected[cell(2, 4)] = 3_000 * MS  # [2 s, 5 s)
    np.testing.assert_array_equal(exposure[0], expected)
    assert exit_cell[0] == cell(2, 4)
    assert exposure[0].sum() == durations[0], "exposure must add up to the life"


def test_exposure_always_sums_to_the_life_and_exit_cells_are_valid():
    from shadowfill.ipcw import AGE_EDGES_NS, N_CELLS, cell_exposure

    rng = np.random.default_rng(0)
    n = 500
    durations = rng.integers(0, 30_000 * MS, size=n).astype(float)
    # A valid path: crossing times non-decreasing (a lower threshold is crossed
    # at or after every higher one), with the first k present and the rest never.
    crossings = np.sort(rng.integers(0, 30_000 * MS, size=(n, 4)), axis=1).astype(float)
    k = rng.integers(0, 5, size=n)
    crossings[np.arange(4)[None, :] >= k[:, None]] = np.inf
    exposure, exit_cell = cell_exposure(durations, crossings, AGE_EDGES_NS)
    np.testing.assert_allclose(exposure.sum(axis=1), durations)
    assert np.all((exit_cell >= 0) & (exit_cell < N_CELLS))
    assert np.all(exposure >= 0)


def test_time_varying_ipcw_agrees_with_the_truth_on_the_placebo():
    pytest.importorskip("shadowfill._core")
    from shadowfill.ipcw import compare_ipcw
    from shadowfill.synthetic import generate_synthetic_messages

    sec = 1_000_000_000
    events = generate_synthetic_messages(n_events=60_000, seed=101)
    result = compare_ipcw(events, horizon_ns=5 * sec, horizons=np.array([sec, 5 * sec]))
    assert np.max(np.abs(result.tv_ipcw - result.ground_truth)) < 0.02


def test_multiplicity_weights_equal_replicating_the_rows():
    """The bootstrap passes draw counts as weights instead of copying a 791k x 30
    exposure matrix per replicate. That is only valid if the two are identical."""
    from shadowfill.ipcw import AGE_EDGES_NS, cell_exposure, tv_ipcw_fill_curve

    rng = np.random.default_rng(4)
    n = 400
    durations = rng.integers(1, 20_000 * MS, size=n).astype(float)
    cause = rng.choice([FILL, CANCEL, CENSORED], size=n, p=[0.4, 0.45, 0.15])
    crossings = np.sort(rng.integers(0, 20_000 * MS, size=(n, 4)), axis=1).astype(float)
    crossings[np.arange(4)[None, :] >= rng.integers(0, 5, size=n)[:, None]] = np.inf
    exposure, exit_cell = cell_exposure(durations, crossings, AGE_EDGES_NS)
    horizons = np.array([10 * MS, 1_000 * MS, 10_000 * MS])

    m = rng.integers(0, 4, size=n)
    rows = np.repeat(np.arange(n), m)
    replicated = tv_ipcw_fill_curve(
        durations[rows], cause[rows], exposure[rows], exit_cell[rows], horizons
    )
    weighted = tv_ipcw_fill_curve(
        durations, cause, exposure, exit_cell, horizons, weight=m.astype(float)
    )
    np.testing.assert_allclose(weighted, replicated, rtol=0, atol=1e-12)
