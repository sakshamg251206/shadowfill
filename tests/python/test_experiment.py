"""The H1 runner: one command, one reproducible table."""

from __future__ import annotations

import json

import numpy as np
import pyarrow.parquet as pq
import pytest

from shadowfill.experiment import format_table, run_h1
from shadowfill.synthetic import generate_synthetic_messages, write_synthetic_csv

SEC = 1_000_000_000


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    path = tmp_path_factory.mktemp("h1") / "messages.csv"
    write_synthetic_csv(generate_synthetic_messages(n_events=40_000, seed=31), path)
    return path


@pytest.fixture(scope="module")
def result(session, tmp_path_factory):
    out = tmp_path_factory.mktemp("h1out")
    manifest = run_h1(
        message_path=session,
        out_dir=out,
        grid_ns=20_000_000,
        size=5,
        horizon_ns=10 * SEC,
        engine="python",
        horizons=np.array([SEC // 10, SEC, 10 * SEC], dtype=np.int64),
        at_touch_only=False,
        block_ns=2 * SEC,
        n_replicates=40,
        seed=5,
    )
    return manifest, out


def test_writes_a_table_and_a_manifest(result):
    manifest, out = result
    table = pq.read_table(out / "h1_curves.parquet").to_pylist()
    assert len(table) == len(manifest["rows"])
    assert {r["stratum"] for r in table} >= {"all"}
    # Strata are reported alongside the unconditional row, not instead of it.
    assert len({r["stratum"] for r in table}) > 1
    assert json.loads((out / "manifest.json").read_text()) == manifest


def test_every_reported_number_is_traceable(result):
    """Invariant 7: a figure nobody can regenerate is not evidence."""
    manifest, _ = result
    assert len(manifest["input_sha256"]) == 64
    assert manifest["git_sha"]
    assert manifest["environment"]["python"]
    assert manifest["config"]["seed"] == 5
    assert manifest["config"]["n_blocks"] >= 2
    assert "within-session" in manifest["bootstrap_scope"]


def test_each_row_carries_an_interval_around_its_point_estimate(result):
    manifest, _ = result
    for row in manifest["rows"]:
        assert row["naive_error_lo"] <= row["naive_error"] <= row["naive_error_hi"]
        assert row["naive_error"] == pytest.approx(row["naive_km"] - row["ground_truth"])
        assert 0 <= row["ground_truth"] <= 1
        assert row["n_shadows"] > 0 and row["n_orders"] > 0


def test_the_run_is_reproducible_from_its_manifest(session, tmp_path):
    """Same input, same config, same seed -- same numbers, or nothing here is evidence."""
    kwargs = dict(
        message_path=session,
        grid_ns=20_000_000,
        size=5,
        horizon_ns=10 * SEC,
        engine="python",
        horizons=np.array([SEC], dtype=np.int64),
        at_touch_only=False,
        block_ns=2 * SEC,
        n_replicates=20,
        seed=5,
    )
    first = run_h1(out_dir=tmp_path / "a", **kwargs)
    second = run_h1(out_dir=tmp_path / "b", **kwargs)
    assert first["rows"] == second["rows"]


def test_an_empty_input_is_refused(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    with pytest.raises(Exception):  # noqa: B017 - pandas or our own check, both fine
        run_h1(message_path=path, out_dir=tmp_path / "out")


def test_the_printed_table_shows_every_horizon(result):
    manifest, _ = result
    text = format_table(manifest["rows"])
    assert "100ms" in text and "1s" in text and "10s" in text
    assert "naive err" in text
