import numpy as np

from shadowfill.events import EVENT_DTYPE, EventType, Side, empty_events


def test_event_type_values_match_lobster_codes():
    assert EventType.ADD == 1
    assert EventType.CANCEL_PARTIAL == 2
    assert EventType.DELETE == 3
    assert EventType.EXECUTE == 4
    assert EventType.EXECUTE_HIDDEN == 5
    assert EventType.CROSS == 6
    assert EventType.HALT == 7


def test_side_encodes_resting_side():
    assert Side.BID == 1
    assert Side.ASK == -1


def test_event_dtype_field_order_and_widths():
    assert EVENT_DTYPE.names == (
        "ts_ns",
        "seq",
        "order_id",
        "price",
        "size",
        "type",
        "side",
    )
    assert EVENT_DTYPE["ts_ns"] == np.dtype("<i8")
    assert EVENT_DTYPE["price"] == np.dtype("<i8")
    assert EVENT_DTYPE["size"] == np.dtype("<i8")


def test_empty_events_allocates_correct_shape():
    ev = empty_events(3)
    assert ev.shape == (3,)
    assert ev.dtype == EVENT_DTYPE
