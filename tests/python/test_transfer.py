"""Plan 4 cross-regime generalisation: does a correction learned on one book
transfer to another?

The correction is a calibration factor per (horizon, queue-ahead stratum),
R = truth / Kaplan-Meier, learned on a source and applied to a target's own
stratified Kaplan-Meier. The rule was fixed before any real data was looked at.
"""

from __future__ import annotations

import numpy as np
import pytest

from shadowfill.transfer import StratifiedCurves, apply_calibration, stratified_curves

H = np.array([1.0, 5.0, 20.0])


def curves(truth, km, shares) -> StratifiedCurves:
    return StratifiedCurves(
        horizons=H,
        truth=np.array(truth, float),
        km=np.array(km, float),
        shares=np.array(shares, float),
    )


def test_self_transfer_reproduces_the_target_truth_exactly():
    c = curves([[0.1, 0.3, 0.5], [0.0, 0.1, 0.2]], [[0.05, 0.2, 0.3], [0.0, 0.05, 0.1]], [0.6, 0.4])
    np.testing.assert_allclose(apply_calibration(c, c), c.stratified_truth(), atol=1e-15)


def test_a_source_with_no_bias_leaves_the_target_estimate_unchanged():
    unbiased = curves([[0.1, 0.3, 0.5]], [[0.1, 0.3, 0.5]], [1.0])
    target = curves([[0.4, 0.6, 0.8]], [[0.2, 0.3, 0.4]], [1.0])
    np.testing.assert_allclose(apply_calibration(unbiased, target), target.stratified_km())


def test_a_stratum_the_source_cannot_calibrate_is_left_uncorrected():
    """KM of zero in the source gives no usable ratio; the target's own KM is kept."""
    source = curves(
        [[0.1, 0.2, 0.3], [0.0, 0.0, 0.0]], [[0.05, 0.1, 0.15], [0.0, 0.0, 0.0]], [0.5, 0.5]
    )
    target = curves(
        [[0.3, 0.4, 0.5], [0.2, 0.3, 0.4]], [[0.1, 0.2, 0.25], [0.1, 0.15, 0.2]], [0.5, 0.5]
    )
    out = apply_calibration(source, target)
    expected = 0.5 * np.minimum(1.0, 2.0 * target.km[0]) + 0.5 * target.km[1]
    np.testing.assert_allclose(out, expected)


def test_calibrated_probabilities_never_exceed_one():
    source = curves([[0.9, 0.9, 0.9]], [[0.1, 0.1, 0.1]], [1.0])
    target = curves([[0.5, 0.5, 0.5]], [[0.5, 0.5, 0.5]], [1.0])
    assert np.all(apply_calibration(source, target) <= 1.0)


def test_stratified_curves_on_the_placebo_find_no_bias():
    pytest.importorskip("shadowfill._core")
    from shadowfill.synthetic import generate_synthetic_messages
    from shadowfill.transfer import order_arrays

    sec = 1_000_000_000
    events = generate_synthetic_messages(n_events=60_000, seed=101)
    arrays = order_arrays(events, horizon_ns=5 * sec)
    c = stratified_curves(arrays, np.array([sec, 5 * sec]))
    assert np.max(np.abs(c.stratified_km() - c.stratified_truth())) < 0.02
    assert abs(c.shares.sum() - 1.0) < 1e-12


def test_run_writes_pairs_and_leave_one_out_with_intervals(tmp_path):
    pytest.importorskip("shadowfill._core")
    import json

    from shadowfill.synthetic import generate_synthetic_messages, write_synthetic_csv
    from shadowfill.transfer import run_transfer

    sec = 1_000_000_000
    books = {}
    for name, seed, inject in (("calm", 1, 0.0), ("picked", 2, 0.9), ("mixed", 3, 0.5)):
        path = tmp_path / f"{name}_message_10.csv"
        write_synthetic_csv(
            generate_synthetic_messages(n_events=20_000, seed=seed, informed_cancel=inject), path
        )
        books[name] = path

    manifest = run_transfer(
        books=books,
        out_dir=tmp_path / "out",
        horizon_ns=5 * sec,
        horizons=np.array([sec, 5 * sec], dtype=np.int64),
        window_ns=None,
        block_ns=2 * sec,
        n_replicates=20,
    )
    on_disk = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert on_disk["experiment"] == "transfer"
    assert set(on_disk["input_sha256"]) == set(books)

    pairs = [r for r in manifest["rows"] if r["mode"] == "pair"]
    assert len(pairs) == 3 * 2 * 2  # ordered pairs x horizons
    assert all(r["source"] != r["target"] for r in pairs)

    loo = [r for r in manifest["rows"] if r["mode"] == "leave_one_out"]
    assert {r["target"] for r in loo} == set(books)
    for r in loo:
        assert r["error_transferred_lo"] <= r["error_transferred_hi"]
        assert r["error_uncorrected_lo"] <= r["error_uncorrected_hi"]
    assert 0.0 <= manifest["summary"]["share_of_pairs_improved"] <= 1.0
