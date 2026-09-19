"""The falsification test: on independent censoring, the measured bias must vanish.

The synthetic generator cancels orders uniformly at random over the live set,
at any depth, without looking at the queue front. Cancellation is therefore
independent of fill prospects by construction -- exactly the assumption
Kaplan-Meier needs. So on this stream the naive estimator is *correct*, and
ShadowFill must measure no bias.

If this test fails, no number produced by this pipeline means anything. It has
already caught one real defect: the original time-grid comparison reported
+0.26 here, because grid shadows and real orders are different populations.
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

# Horizons must stay short relative to the stream, or the ground truth curve
# goes flat for want of anything left to observe and the comparison measures
# the window rather than the estimator. See the 60s row in the README.
HORIZONS = np.array([SEC // 10, SEC, 5 * SEC], dtype=np.int64)

#: Independent censoring should leave no bias at all. The allowance is for
#: finite-sample noise in a 60k-event stream, not for a tolerated defect: the
#: committed 200k-event fixture measures -0.0000, -0.0002 and -0.0015.
TOLERANCE = 0.02


@pytest.fixture(scope="module")
def placebo():
    pytest.importorskip("shadowfill._core")
    events = generate_synthetic_messages(n_events=60_000, seed=101)
    placements = place_matched_to_orders(events, horizon_ns=5 * SEC)
    columns, _ = compute_outcomes(events, placements, "cpp")
    return compare_fill_curves(
        columns,
        extract_lifetimes(events),
        horizon_ns=5 * SEC,
        end_ts=int(events["ts_ns"][-1]),
        horizons=HORIZONS,
        at_touch_only=False,
    )


def test_no_bias_is_measured_when_censoring_is_independent(placebo):
    worst = np.max(np.abs(placebo.naive_error))
    assert worst < TOLERANCE, (
        f"placebo bias {worst:.4f} exceeds {TOLERANCE}: the pipeline is measuring "
        f"something other than the cancellation bias\n{placebo.naive_error}"
    )


def test_the_two_populations_are_the_same_size(placebo):
    """Matched placement means one shadow per order. Any drift breaks the claim."""
    assert placebo.n_shadows == placebo.n_orders


def test_the_competing_risks_estimator_also_agrees_here(placebo):
    """With independent censoring and no informative cancellation, all three agree."""
    assert np.max(np.abs(placebo.competing_risks_error)) < TOLERANCE
