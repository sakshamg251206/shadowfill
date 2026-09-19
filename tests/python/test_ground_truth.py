import json

import pyarrow.parquet as pq
import pytest

from shadowfill.ground_truth import run_ground_truth
from shadowfill.placements import place_top_of_book_grid
from shadowfill.replay import Status
from shadowfill.synthetic import generate_synthetic_messages, write_synthetic_csv

SEC = 1_000_000_000


def test_grid_places_one_shadow_per_side_per_grid_point():
    events = generate_synthetic_messages(n_events=20_000, seed=6)
    placements = place_top_of_book_grid(
        events, grid_ns=50 * 1_000_000, size=5, horizon_ns=2 * SEC, latency_ns=0
    )
    assert placements
    assert len({p.shadow_id for p in placements}) == len(placements)
    assert {p.side for p in placements} == {1, -1}
    assert all(p.horizon_ns == 2 * SEC for p in placements)


def test_grid_ids_are_contiguous_from_zero():
    events = generate_synthetic_messages(n_events=20_000, seed=6)
    placements = place_top_of_book_grid(
        events, grid_ns=50 * 1_000_000, size=5, horizon_ns=2 * SEC, latency_ns=0
    )
    assert sorted(p.shadow_id for p in placements) == list(range(len(placements)))


def test_run_writes_parquet_and_manifest(tmp_path):
    events = generate_synthetic_messages(n_events=30_000, seed=8)
    data_path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, data_path)
    out_dir = tmp_path / "out"

    manifest = run_ground_truth(
        message_path=data_path,
        out_dir=out_dir,
        grid_ns=50 * 1_000_000,
        size=5,
        horizon_ns=2 * SEC,
        latency_ns=0,
        engine="python",
    )

    table = pq.read_table(out_dir / "outcomes.parquet")
    assert table.num_rows == manifest["n_placements"]
    assert set(table.column_names) >= {
        "shadow_id",
        "status",
        "insert_ts",
        "insert_seq",
        "ahead_at_insert",
        "ahead_at_end",
        "first_fill_ts",
        "full_fill_ts",
        "filled_qty",
    }

    on_disk = json.loads((out_dir / "manifest.json").read_text())
    assert on_disk["input_sha256"] == manifest["input_sha256"]
    assert len(on_disk["input_sha256"]) == 64
    assert on_disk["config"]["grid_ns"] == 50 * 1_000_000


def test_python_and_cpp_engines_write_identical_outcomes(tmp_path):
    pytest.importorskip("shadowfill._core")
    events = generate_synthetic_messages(n_events=30_000, seed=12)
    data_path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, data_path)

    kwargs = dict(
        message_path=data_path,
        grid_ns=50 * 1_000_000,
        size=5,
        horizon_ns=2 * SEC,
        latency_ns=0,
    )
    run_ground_truth(out_dir=tmp_path / "py", engine="python", **kwargs)
    run_ground_truth(out_dir=tmp_path / "cpp", engine="cpp", **kwargs)

    py_table = pq.read_table(tmp_path / "py" / "outcomes.parquet")
    cpp_table = pq.read_table(tmp_path / "cpp" / "outcomes.parquet")
    assert py_table.equals(cpp_table)


# --- amendment E: nulls, not sentinels, in Parquet -------------------------


def _run(tmp_path, seed, engine="python", **over):
    events = generate_synthetic_messages(n_events=30_000, seed=seed)
    data_path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, data_path)
    kwargs = dict(
        message_path=data_path,
        out_dir=tmp_path / engine,
        grid_ns=50 * 1_000_000,
        size=5,
        horizon_ns=2 * SEC,
        latency_ns=0,
        engine=engine,
    )
    kwargs.update(over)
    manifest = run_ground_truth(**kwargs)
    return manifest, pq.read_table(kwargs["out_dir"] / "outcomes.parquet")


def test_not_activated_rows_are_null_not_sentinel(tmp_path):
    """A placement that never existed is missing data, not a -1 observation.

    -1 silently regresses as a covariate downstream; a null propagates or raises.
    """
    # Latency pushes the last grid points' effective timestamps past the end of
    # the stream, so NOT_ACTIVATED rows are guaranteed rather than hoped for.
    _, table = _run(tmp_path, seed=8, latency_ns=5 * SEC)
    status = table.column("status").to_pylist()
    not_activated = [i for i, s in enumerate(status) if s == int(Status.NOT_ACTIVATED)]
    assert not_activated, "expected latency to strand some placements past the stream"
    for name in table.column_names:
        if name in ("shadow_id", "status"):
            continue  # identity and the status code itself stay populated
        col = table.column(name).to_pylist()
        for i in not_activated:
            assert col[i] is None, f"{name}[{i}] is {col[i]!r}, expected null"


def test_no_negative_values_survive_to_parquet(tmp_path):
    """Sentinels must not reach disk, except the two genuine 'did not fill' columns."""
    _, table = _run(tmp_path, seed=8)
    for name in (
        "insert_ts",
        "insert_seq",
        "ahead_at_insert",
        "ahead_at_end",
        "filled_qty",
        "assumed_ahead_events",
    ):
        values = [v for v in table.column(name).to_pylist() if v is not None]
        assert all(v >= 0 for v in values), f"negative value survived in {name}"


def test_fill_timestamps_stay_minus_one_on_activated_unfilled_rows(tmp_path):
    """Not filling is a real observation about an order that existed."""
    _, table = _run(tmp_path, seed=8)
    status = table.column("status").to_pylist()
    first = table.column("first_fill_ts").to_pylist()
    filled_qty = table.column("filled_qty").to_pylist()
    activated_unfilled = [
        i for i, s in enumerate(status) if s != int(Status.NOT_ACTIVATED) and filled_qty[i] == 0
    ]
    assert activated_unfilled, "expected some activated placements not to fill"
    assert all(first[i] == -1 for i in activated_unfilled)


# --- amendment F: the per-shadow assumption column is present --------------


def test_assumed_ahead_events_column_is_written(tmp_path):
    _, table = _run(tmp_path, seed=8)
    assert "assumed_ahead_events" in table.column_names


# --- amendment H: the run is traceable to an environment -------------------


def test_manifest_records_environment_provenance(tmp_path):
    manifest, _ = _run(tmp_path, seed=8)
    env = manifest["environment"]
    assert env["python"].startswith("3.")
    for pkg in ("numpy", "pandas", "pyarrow"):
        assert env[pkg]


def test_manifest_reports_diagnostics_for_both_engines(tmp_path):
    """The DoD requires fifo_violations reported, not silently dropped."""
    py_manifest, _ = _run(tmp_path, seed=8, engine="python")
    assert py_manifest["diagnostics"]["fifo_violations"] == 0
    assert "unknown_order_events" in py_manifest["diagnostics"]

    pytest.importorskip("shadowfill._core")
    cpp_manifest, _ = _run(tmp_path, seed=8, engine="cpp")
    assert cpp_manifest["diagnostics"] == py_manifest["diagnostics"]
