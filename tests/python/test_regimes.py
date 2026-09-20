"""H2: the tick-regime comparison."""

from __future__ import annotations

import numpy as np
import pytest

from shadowfill.dataset import write_events
from shadowfill.events import EventType, Side, empty_events
from shadowfill.regimes import MIN_EVENTS, TICK, format_table, relative_tick_bps, run_h2

SEC = 1_000_000_000
OPEN_NS = 34_200_000_000_000


def stream(price, n):
    rows = [(OPEN_NS, 999, price, 10_000, EventType.ADD, Side.BID)]
    for i in range(n):
        ts = OPEN_NS + i * 1_000_000
        rows.append((ts, 1000 + i, price, 50, EventType.ADD, Side.BID))
        rows.append((ts + 500_000, 1000 + i, price, 50, EventType.EXECUTE, Side.BID))
    events = empty_events(len(rows))
    for i, (ts, oid, p, size, etype, side) in enumerate(rows):
        events[i] = (ts, i, oid, p, size, int(etype), int(side))
    return events


def test_relative_tick_is_a_tick_as_a_fraction_of_price():
    """A $0.01 tick on a $100 stock is one basis point, by definition."""
    events = stream(1_000_000, 10)  # $100.00 in canonical units
    assert relative_tick_bps(events) == pytest.approx(1.0)

    cheap = stream(100_000, 10)  # $10.00 -> a tick is ten times bigger, relatively
    assert relative_tick_bps(cheap) == pytest.approx(10.0)
    assert TICK == 100


def test_the_regime_variable_resists_a_single_wide_quote():
    """It is a median, so one stray price cannot redefine the regime."""
    events = stream(1_000_000, 200)
    events["price"][1] = 50_000_000  # one absurd add
    assert relative_tick_bps(events) == pytest.approx(1.0)


def test_thin_symbols_are_skipped_rather_than_reported_noisily(tmp_path):
    """A regime point built on a few hundred orders is worse than no point.

    Measured: SAP has 8,131 in-window events on 2019-12-30 because its real
    liquidity is in European hours, and it would otherwise sit in the table
    looking like a tick-regime observation.
    """
    root = tmp_path / "parquet"
    for symbol, n in (("THIN", 10), ("ALSOTHIN", 20)):
        path = root / "date=2019-12-30" / f"symbol={symbol}" / "events.parquet"
        write_events(stream(1_000_000, n), path)

    result = run_h2(
        root, "2019-12-30", ["THIN", "ALSOTHIN", "MISSING"], tmp_path / "out", n_replicates=5
    )
    assert result["rows"] == []
    assert set(result["skipped"]) == {"THIN", "ALSOTHIN", "MISSING"}
    assert result["skipped"]["MISSING"] == "no partition"
    assert "in-window events" in result["skipped"]["THIN"]
    assert result["config"]["min_events"] == MIN_EVENTS


def test_rows_are_ordered_by_tick_regime():
    """The table is read down the regime axis, so it must be sorted on it."""
    rows = [
        {"symbol": "B", "relative_tick_bps": 2.0, "horizon_ns": SEC, "median_price": 50.0,
         "ground_truth": 0.4, "naive_km": 0.1, "naive_error": -0.3,
         "naive_error_lo": -0.35, "naive_error_hi": -0.25},
        {"symbol": "A", "relative_tick_bps": 0.5, "horizon_ns": SEC, "median_price": 200.0,
         "ground_truth": 0.4, "naive_km": 0.1, "naive_error": -0.3,
         "naive_error_lo": -0.35, "naive_error_hi": -0.25},
    ]  # fmt: skip
    rows.sort(key=lambda r: (r["relative_tick_bps"], r["horizon_ns"]))
    text = format_table(rows, SEC)
    assert text.index(" A ") < text.index(" B ")


def test_format_table_selects_one_horizon():
    rows = [
        {"symbol": "A", "relative_tick_bps": 0.5, "horizon_ns": h, "median_price": 200.0,
         "ground_truth": 0.4, "naive_km": 0.1, "naive_error": -0.3,
         "naive_error_lo": -0.35, "naive_error_hi": -0.25}
        for h in (SEC, 10 * SEC)
    ]  # fmt: skip
    assert len(format_table(rows, SEC).splitlines()) == 3  # header, rule, one row


def test_an_empty_stream_has_no_regime():
    with pytest.raises(ValueError, match="no adds"):
        relative_tick_bps(np.zeros(0, dtype=empty_events(0).dtype))
