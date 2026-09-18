import numpy as np
import pytest

from shadowfill.events import EVENT_DTYPE, EventType, Side
from shadowfill.lobster import load_lobster_messages, parse_seconds_to_ns

RAW = """34200.123456789,1,11885113,21,2238100,-1
34200.123456790,3,11885113,21,2238100,-1
34200.200000000,4,11885114,100,2238000,1
34201.000000000,5,0,50,2238050,-1
"""


@pytest.fixture
def message_file(tmp_path):
    p = tmp_path / "TEST_2012-06-21_34200000_57600000_message_10.csv"
    p.write_text(RAW)
    return p


def test_parse_seconds_to_ns_is_exact_at_nanosecond_resolution():
    import pandas as pd

    out = parse_seconds_to_ns(pd.Series(["34200.123456789", "1", "0.000000001"]))
    assert out.tolist() == [34200123456789, 1_000_000_000, 1]


def test_load_returns_canonical_dtype_and_monotone_seq(message_file):
    ev = load_lobster_messages(message_file)
    assert ev.dtype == EVENT_DTYPE
    assert ev.shape == (4,)
    assert np.array_equal(ev["seq"], np.arange(4, dtype=np.uint64))


def test_direction_maps_to_resting_side(message_file):
    ev = load_lobster_messages(message_file)
    assert ev["side"][0] == Side.ASK
    assert ev["side"][2] == Side.BID


def test_types_and_prices_preserved_as_integers(message_file):
    ev = load_lobster_messages(message_file)
    assert ev["type"][0] == EventType.ADD
    assert ev["type"][3] == EventType.EXECUTE_HIDDEN
    assert ev["price"][0] == 2238100
    assert ev["size"][2] == 100


def test_timestamps_are_nondecreasing(message_file):
    ev = load_lobster_messages(message_file)
    assert np.all(np.diff(ev["ts_ns"]) >= 0)


def test_rejects_out_of_order_timestamps(tmp_path):
    p = tmp_path / "bad_message_10.csv"
    p.write_text("34200.000000002,1,1,10,100,1\n34200.000000001,1,2,10,100,1\n")
    with pytest.raises(ValueError, match="not sorted by timestamp"):
        load_lobster_messages(p)
