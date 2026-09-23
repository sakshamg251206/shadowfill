"""Plan 4's latency sweep: how the measured bias moves with order-entry latency.

RESEARCH-SPEC §7 asks for sensitivity rather than a single number. Two regimes
have to be kept apart when reading the output, and the tests below pin both:

* **0 -> 1 ns is a change of question, not of speed.** Matched placement puts a
  zero-latency shadow in its real twin's exact queue slot -- "would *this*
  order have filled had it not cancelled", the question survival estimators
  claim to answer. At any latency >= 1 ns the twin arrives first, so the shadow
  sits exactly one twin-order-size further back. Measured: 100% of 65,949
  shadows across two seeds.
* **Beyond 1 ns the effect is genuine latency.** At every instant both are
  live, a later shadow has at least as much queue ahead as an earlier twin --
  everything ahead of the early one, plus whatever arrived in between -- and
  every operation is monotone in ``ahead``. So any execution that fills the
  late shadow fills the early one too.

What is *not* a theorem, and was wrongly asserted by the first version of this
file: that mean ``ahead_at_insert`` rises with latency, or that F* falls. Both
held on one synthetic seed. On AAPL mean ahead goes 776.7 -> 774.8 from 1 ms to
5 ms, because the two latencies read ``ahead`` at different moments and orders
ahead can cancel during the delay; and a later shadow's horizon also ends
later, so its duration-based F* is not bounded by the early one's. They were
replaced by the fill-time dominance they were approximating.
"""

from __future__ import annotations

import json
from itertools import pairwise

import numpy as np
import pytest

from shadowfill.events import EventType
from shadowfill.latency import run_latency, sweep_latency
from shadowfill.synthetic import generate_synthetic_messages, write_synthetic_csv

SEC = 1_000_000_000
MS = 1_000_000
HORIZONS = np.array([SEC // 10, SEC, 5 * SEC], dtype=np.int64)
LATENCIES = (0, 1, MS, 20 * MS)


@pytest.fixture(scope="module")
def events():
    return generate_synthetic_messages(n_events=60_000, seed=101)


@pytest.fixture(scope="module")
def sweep(events):
    pytest.importorskip("shadowfill._core")
    return sweep_latency(events, LATENCIES, horizon_ns=5 * SEC, horizons=HORIZONS)


def test_one_row_per_latency_in_order(sweep):
    assert [row.latency_ns for row in sweep] == list(LATENCIES)


def test_the_observational_side_does_not_depend_on_latency(sweep):
    """KM is fitted on real orders, which have no latency. If this moves, the
    sweep is leaking the shadow's configuration into the comparison baseline."""
    for row in sweep[1:]:
        np.testing.assert_array_equal(row.comparison.naive_km, sweep[0].comparison.naive_km)


def test_a_later_shadow_never_fills_before_its_earlier_twin(sweep):
    """The dominance theorem, exact and per shadow, for every adjacent latency pair.

    If the late shadow fills at tau inside the early shadow's own window, the
    early one had already filled, at or before tau.
    """
    horizon = 5 * SEC
    for early, late in pairwise(sweep):
        e, lt = early.columns, late.columns
        inside = (
            (lt["first_fill_ts"] != -1)
            & (e["insert_ts"] != -1)
            & (lt["first_fill_ts"] <= e["insert_ts"] + horizon)
        )
        assert inside.sum() > 0, "vacuous: no late fills inside the early windows"
        early_fill = e["first_fill_ts"][inside]
        assert np.all(early_fill != -1), (
            f"{late.latency_ns} ns filled where {early.latency_ns} ns did not"
        )
        assert np.all(early_fill <= lt["first_fill_ts"][inside])


def test_one_nanosecond_moves_every_shadow_behind_its_twin(events):
    """The 0 -> 1 ns step is exact, per shadow: behind by the twin's own size."""
    pytest.importorskip("shadowfill._core")
    from shadowfill.ground_truth import compute_outcomes
    from shadowfill.placements import place_matched_to_orders
    from shadowfill.replay import Status

    adds = events[events["type"] == int(EventType.ADD)]
    at0, _ = compute_outcomes(events, place_matched_to_orders(events, horizon_ns=5 * SEC), "cpp")
    at1, _ = compute_outcomes(
        events, place_matched_to_orders(events, horizon_ns=5 * SEC, latency_ns=1), "cpp"
    )
    live = (at0["status"] != int(Status.NOT_ACTIVATED)) & (
        at1["status"] != int(Status.NOT_ACTIVATED)
    )
    shift = (at1["ahead_at_insert"] - at0["ahead_at_insert"])[live]
    np.testing.assert_array_equal(shift, adds["size"][live].astype("int64"))


def test_run_writes_a_manifest_with_every_latency(tmp_path):
    pytest.importorskip("shadowfill._core")
    events = generate_synthetic_messages(n_events=20_000, seed=7)
    data_path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, data_path)

    manifest = run_latency(
        message_path=data_path,
        out_dir=tmp_path / "out",
        latencies_ns=(0, MS),
        horizon_ns=5 * SEC,
        horizons=HORIZONS,
        n_replicates=20,
        window_ns=None,
        # The synthetic stream spans ~20 s, so session-scale blocks give one.
        block_ns=2 * SEC,
    )

    on_disk = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert on_disk["experiment"] == "latency-sweep"
    assert on_disk["config"]["latencies_ns"] == [0, MS]
    assert len(on_disk["input_sha256"]) == 64
    assert {r["latency_ns"] for r in manifest["rows"]} == {0, MS}
    assert all("naive_error_lo" in r and "naive_error_hi" in r for r in manifest["rows"])
    assert (tmp_path / "out" / "latency_sweep.parquet").exists()
