"""H5: policy ranking under the truth versus under the estimator."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from shadowfill.markout import TOB_DTYPE
from shadowfill.policies import (
    PolicyEdge,
    evaluate_policies,
    median_half_spread_bps,
    rank_inversions,
)

SEC = 1_000_000_000


def tob(rows):
    out = np.zeros(len(rows), dtype=TOB_DTYPE)
    for i, (ts, bid, ask) in enumerate(rows):
        out[i] = (ts, bid, ask)
    return out


def test_half_spread_is_measured_against_the_mid():
    """A 2-tick spread on a $100 book is 1 bp wide, so half of it is 0.5 bp."""
    book = tob([(1, 999_900, 1_000_100), (2, 999_900, 1_000_100)])
    assert median_half_spread_bps(book) == pytest.approx(1.0)


def test_one_sided_rows_are_ignored_not_treated_as_zero_spread():
    """A zero on one side means an empty book, not a price of zero."""
    book = tob([(1, 0, 1_000_100), (2, 999_900, 1_000_100), (3, 999_900, 0)])
    assert median_half_spread_bps(book) == pytest.approx(1.0)


def test_a_book_that_is_never_two_sided_has_no_spread_to_measure():
    with pytest.raises(ValueError, match="never two-sided"):
        median_half_spread_bps(tob([(1, 999_900, 0)]))


def test_edge_is_the_fill_weighted_markout_less_the_cost_of_crossing():
    """Half the orders fill at -1 bp; the rest cross at 2 bp.

    edge = 0.5 * (-1) - 0.5 * 2 = -1.5
    """
    durations = np.array([1.0, 1.0, 10.0, 10.0])
    filled = np.array([True, True, False, False])
    events_flag = filled.astype(float)
    marks = np.array([-1.0, -1.0, np.nan, np.nan])

    got = evaluate_policies(
        durations, events_flag, filled, marks, cross_cost_bps=2.0, waits_ns=(5,)
    )
    assert got[0].fill_rate == pytest.approx(0.5)
    assert got[0].markout_bps == pytest.approx(-1.0)
    assert got[0].edge_bps == pytest.approx(-1.5)


def test_a_longer_wait_can_only_gain_fills():
    """Fill probability is a CDF, so it is non-decreasing in the wait."""
    rng = np.random.default_rng(0)
    durations = rng.exponential(2.0, size=500)
    filled = rng.random(500) < 0.6
    got = evaluate_policies(
        durations,
        filled.astype(float),
        filled,
        rng.normal(-0.3, 0.1, size=500),
        cross_cost_bps=1.0,
        waits_ns=(1, 2, 4, 8),
    )
    rates = [p.fill_rate for p in got]
    assert all(b >= a - 1e-12 for a, b in pairwise(rates))


def test_a_policy_nobody_ever_filled_under_still_has_a_cost():
    """Crossing is what happens when the passive order does not fill."""
    durations = np.array([100.0, 100.0])
    filled = np.array([False, False])
    got = evaluate_policies(
        durations, filled.astype(float), filled, np.array([np.nan, np.nan]),
        cross_cost_bps=3.0, waits_ns=(1,),
    )  # fmt: skip
    assert got[0].fill_rate == pytest.approx(0.0)
    assert got[0].edge_bps == pytest.approx(-3.0)


def _edges(values):
    return [PolicyEdge(i, 0.0, 0.0, 0.0, v) for i, v in enumerate(values)]


def test_identical_orderings_invert_nothing():
    pairs, inverted, rate = rank_inversions(_edges([1.0, 2.0, 3.0]), _edges([10.0, 20.0, 30.0]))
    assert (pairs, inverted, rate) == (3, 0, 0.0)


def test_a_reversed_ordering_inverts_every_pair():
    pairs, inverted, rate = rank_inversions(_edges([1.0, 2.0, 3.0]), _edges([3.0, 2.0, 1.0]))
    assert (pairs, inverted) == (3, 3)
    assert rate == 1.0


def test_one_swapped_neighbour_inverts_exactly_one_pair():
    """The decision a desk makes is a pairwise comparison, so pairs are counted."""
    pairs, inverted, rate = rank_inversions(_edges([1.0, 2.0, 3.0]), _edges([1.0, 3.0, 2.0]))
    assert (pairs, inverted) == (3, 1)
    assert rate == pytest.approx(1 / 3)
