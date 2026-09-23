"""Plan 4's known-bias injection: a cancellation mechanism with a known sign.

The placebo establishes that the pipeline measures *no* bias when cancellation
is independent of fill prospects. That is necessary but not sufficient: an
estimator that always returned zero would also pass it. This test is the other
half -- inject a cancellation mechanism whose direction is known by
construction, and require the measurement to recover it.

``informed_cancel > 0`` is picked-off avoidance: the cancel targets the front
of the best level, the order about to trade. Cancellation therefore removes
fill-prone orders from the risk set, the survivors are adversely selected
against filling, and a censoring estimator must **understate** fill
probability. The measured error ``KM - truth`` must be negative, and must grow
with the strength of the injection.

That is the mechanism RESEARCH-SPEC §3 names as dominant in deep-queue regimes,
and it is the one the AAPL result exhibits.

**What this test deliberately does not assert:** the opposite sign for
``informed_cancel < 0``. See `test_hopeless_queue_cancellation_does_not_reverse
_the_sign` and amendment Y -- the predicted reversal does not appear, and the
reason is not yet established.
"""

from __future__ import annotations

import numpy as np
import pytest

from shadowfill.bias import compare_fill_curves
from shadowfill.ground_truth import compute_outcomes
from shadowfill.lifetimes import extract_lifetimes
from shadowfill.placements import place_matched_to_orders
from shadowfill.synthetic import generate_synthetic_messages

SEC = 1_000_000_000

HORIZONS = np.array([SEC // 10, SEC, 5 * SEC], dtype=np.int64)

#: Same allowance the placebo uses, for the same reason: finite-sample noise in
#: a 60k-event stream. An injected bias has to clear it to count as recovered.
TOLERANCE = 0.02


def _naive_error(strength: float, seed: int = 101) -> np.ndarray:
    events = generate_synthetic_messages(n_events=60_000, seed=seed, informed_cancel=strength)
    placements = place_matched_to_orders(events, horizon_ns=5 * SEC)
    columns, _ = compute_outcomes(events, placements, "cpp")
    return compare_fill_curves(
        columns,
        extract_lifetimes(events),
        horizon_ns=5 * SEC,
        end_ts=int(events["ts_ns"][-1]),
        horizons=HORIZONS,
        at_touch_only=False,
    ).naive_error


@pytest.fixture(scope="module")
def sweep():
    pytest.importorskip("shadowfill._core")
    return {s: _naive_error(s) for s in (-0.9, 0.0, 0.3, 0.9)}


def test_zero_strength_reproduces_the_placebo(sweep):
    """The injection must be inert when switched off, or nothing below means anything."""
    assert np.max(np.abs(sweep[0.0])) < TOLERANCE


def test_injected_pick_off_bias_is_recovered_with_the_right_sign(sweep):
    """Cancelling the order about to trade must make the estimator understate."""
    error = sweep[0.9]
    assert np.all(error <= 0), f"expected a negative error at every horizon, got {error}"
    assert abs(error[-1]) > TOLERANCE, (
        f"injected bias {error[-1]:.4f} did not clear the noise floor {TOLERANCE}: "
        f"the measurement is not recovering a bias that was put there on purpose"
    )


def test_injected_bias_grows_with_the_strength_of_the_injection(sweep):
    """Magnitude is not known in closed form, so the orderable claim is pinned instead."""
    at_5s = [sweep[s][-1] for s in (0.0, 0.3, 0.9)]
    assert at_5s[0] > at_5s[1] > at_5s[2], f"not monotone in injection strength: {at_5s}"


def test_hopeless_queue_cancellation_does_not_reverse_the_sign(sweep):
    """A documented negative result, pinned so a future change has to confront it.

    RESEARCH-SPEC §3 predicts two opposing mechanisms: cancelling hopeless
    orders should make censoring estimators *overstate* fill probability. It
    does not here. Cancelling the back of the touch queue produces a negative
    error of much the same shape as cancelling the front.

    This is pinned rather than explained. A known confound is that the
    injection alters book depth as well as the censoring mechanism, so this is
    not evidence against the hypothesis on real data -- see amendment Y.
    """
    assert np.all(sweep[-0.9] <= 0), (
        f"hopeless-queue injection changed sign to positive: {sweep[-0.9]}. "
        f"If this now passes with a positive error, amendment Y is resolved and "
        f"RESEARCH-SPEC H2's two-mechanism story is supported on synthetic data."
    )
