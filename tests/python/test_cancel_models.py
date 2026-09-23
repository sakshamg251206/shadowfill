"""The three L2 queue models for an anonymous cancel, pinned on hand-built streams.

On an L2 feed a cancel carries no order id (``order_id == 0`` after
``ablate_to_l2``), so a simulator must guess how much of the removed size sat
ahead of the shadow. With the level holding ``L`` shares, of which ``a`` are
ahead of the shadow, a cancel of ``q`` removes from ahead:

    front         min(q, a)                    optimistic
    back          max(0, q - (L - a))          pessimistic
    proportional  floor(q * a / L)             expected value, uniform over shares

"Uniform over orders" needs an order count, which an L2 feed does not carry, so
on L2 it collapses into proportional and is not offered separately.
"""

from __future__ import annotations

import numpy as np
import pytest

from shadowfill.events import EVENT_DTYPE, EventType, Side
from shadowfill.replay import CancelModel, Placement, run_reference

SEC = 1_000_000_000

ANON = 0  # the order id an L2-ablated cancel carries


def make_events(rows):
    ev = np.zeros(len(rows), dtype=EVENT_DTYPE)
    for i, (ts, oid, price, size, etype, side) in enumerate(rows):
        ev[i] = (ts, i, oid, price, size, etype, side)
    return ev


def place(ts=0):
    return Placement(
        shadow_id=0,
        ts_ns=ts,
        latency_ns=0,
        side=int(Side.BID),
        price=100,
        size=10,
        horizon_ns=100 * SEC,
    )


#: 30 shares ahead of the shadow, then 70 behind it, so L = 100 and a = 30 when
#: the anonymous cancel of 40 arrives.
QUEUE = [
    (0, 1, 100, 30, EventType.ADD, Side.BID),
    (1 * SEC, 2, 100, 70, EventType.ADD, Side.BID),
]


def ahead_after_anonymous_cancel(model: CancelModel, qty: int) -> int:
    events = make_events([*QUEUE, (2 * SEC, ANON, 100, qty, EventType.DELETE, Side.BID)])
    return run_reference(events, [place(ts=1)], cancel_model=model)[0].ahead_at_end


def test_front_takes_the_whole_cancel_from_ahead():
    assert ahead_after_anonymous_cancel(CancelModel.FRONT, 40) == 0


def test_back_takes_the_cancel_from_behind_first():
    assert ahead_after_anonymous_cancel(CancelModel.BACK, 40) == 30


def test_back_reaches_ahead_only_once_behind_is_exhausted():
    # 80 > the 70 behind, so the last 10 come out of ahead: 30 - 10 = 20.
    assert ahead_after_anonymous_cancel(CancelModel.BACK, 80) == 20


def test_proportional_takes_the_share_of_the_level_that_is_ahead():
    # floor(40 * 30 / 100) = 12 removed from ahead: 30 - 12 = 18.
    assert ahead_after_anonymous_cancel(CancelModel.PROPORTIONAL, 40) == 18


def test_models_only_touch_anonymous_cancels():
    """A cancel whose id resolves is L3 information, and every model must use it."""
    events = make_events([*QUEUE, (2 * SEC, 2, 100, 70, EventType.DELETE, Side.BID)])
    for model in CancelModel:
        out = run_reference(events, [place(ts=1)], cancel_model=model)[0]
        assert out.ahead_at_end == 30, model


def test_default_is_front_so_existing_results_are_unchanged():
    events = make_events([*QUEUE, (2 * SEC, ANON, 100, 40, EventType.DELETE, Side.BID)])
    assert run_reference(events, [place(ts=1)])[0].ahead_at_end == 0


@pytest.mark.parametrize("model", list(CancelModel))
def test_ahead_never_goes_negative_under_any_model(model):
    events = make_events([*QUEUE, (2 * SEC, ANON, 100, 500, EventType.DELETE, Side.BID)])
    assert run_reference(events, [place(ts=1)], cancel_model=model)[0].ahead_at_end >= 0


def test_fill_rates_order_front_proportional_back_on_an_ablated_stream():
    """Provable by induction: per cancel, front removes the most from ahead and
    back the least, and every later operation is monotone in ahead. So per
    shadow, fills must order front >= proportional >= back."""
    from shadowfill.l2 import ablate_to_l2
    from shadowfill.placements import place_matched_to_orders
    from shadowfill.synthetic import generate_synthetic_messages

    events = ablate_to_l2(generate_synthetic_messages(n_events=20_000, seed=31))
    placements = place_matched_to_orders(events, horizon_ns=5 * SEC)
    filled = {
        model: np.array(
            [o.filled_qty for o in run_reference(events, placements, cancel_model=model)]
        )
        for model in CancelModel
    }
    assert np.all(filled[CancelModel.FRONT] >= filled[CancelModel.PROPORTIONAL])
    assert np.all(filled[CancelModel.PROPORTIONAL] >= filled[CancelModel.BACK])
    assert filled[CancelModel.FRONT].sum() > filled[CancelModel.BACK].sum()
