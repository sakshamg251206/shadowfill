"""Run the ground-truth engine end to end and persist reproducible output."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .dataset import load_events
from .lobster import load_lobster_messages
from .placements import place_top_of_book_grid
from .provenance import environment, git_sha, sha256_file
from .replay import CancelModel, Placement, Status, replay_reference

OUTCOME_FIELDS = (
    "shadow_id",
    "status",
    "insert_ts",
    "insert_seq",
    "ahead_at_insert",
    "ahead_at_end",
    "first_fill_ts",
    "full_fill_ts",
    "filled_qty",
    "assumed_ahead_events",
    # First-passage times of queue-ahead; -1 on an activated row means "never
    # got that close to the front", a real observation, so it stays -1 on disk
    # like an unfilled first_fill_ts.
    "ahead_lt_1000_ts",
    "ahead_lt_100_ts",
    "ahead_lt_10_ts",
    "ahead_lt_1_ts",
)

# Identity and the status code stay populated on every row: without them a
# NOT_ACTIVATED row could not be recognised as one.
ALWAYS_POPULATED = ("shadow_id", "status")

DEFAULTS: dict[str, Any] = {
    "grid_ns": 100_000_000,
    "size": 100,
    "horizon_ns": 60_000_000_000,
    "latency_ns": 0,
    "engine": "cpp",
}

_CONFIG_SCHEMA: dict[str, Callable[[str], Any]] = {
    "message_path": str,
    "out_dir": str,
    "grid_ns": int,
    "size": int,
    "horizon_ns": int,
    "latency_ns": int,
    "engine": str,
}


def load_config(path: str | Path) -> dict[str, Any]:
    """Read the flat ``key: value`` run config.

    Deliberately not a YAML parser. The config is a flat map of seven scalars,
    which is not worth a dependency, and anything this does not understand --
    an unknown key, a missing colon, a value that will not coerce -- raises
    rather than falling back to a default. A narrow reader that refuses input
    it cannot represent costs a clear error; a lenient one costs a wrong run
    that still writes a manifest saying it was right.
    """
    config: dict[str, Any] = {}
    for lineno, raw in enumerate(Path(path).read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"{path}:{lineno}: expected 'key: value', got {raw!r}")
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip().strip("\"'")
        if key not in _CONFIG_SCHEMA:
            raise ValueError(f"{path}:{lineno}: unknown config key {key!r}")
        try:
            config[key] = _CONFIG_SCHEMA[key](value)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: bad value for {key!r}: {value!r}") from exc
    missing = set(_CONFIG_SCHEMA) - set(config)
    if missing:
        raise ValueError(f"{path}: missing required keys: {sorted(missing)}")
    return config


def load_messages(path: str | Path) -> tuple[np.ndarray, str]:
    """Load canonical events from either input format, dispatching on suffix.

    ``.parquet`` is a materialised dataset (see ``dataset.py``); ``.csv`` is a
    LOBSTER message file. An unrecognised suffix raises rather than guessing,
    because guessing wrong produces a run that still writes a manifest saying
    it was right. The format used is recorded in that manifest.
    """
    path = Path(path)
    if path.suffix == ".parquet":
        return load_events(path), "parquet"
    if path.suffix == ".csv":
        return load_lobster_messages(path), "lobster-csv"
    raise ValueError(f"unknown input format for {path.name!r}: expected .parquet or .csv")


def _run_cpp(
    events: np.ndarray, placements: list[Placement], cancel_model: int = CancelModel.FRONT
) -> dict[str, Any]:
    from shadowfill import _core

    return _core.replay(
        events["ts_ns"],
        events["seq"],
        events["order_id"],
        events["price"],
        events["size"],
        events["type"],
        events["side"],
        np.array([p.shadow_id for p in placements], dtype=np.uint64),
        np.array([p.ts_ns for p in placements], dtype=np.int64),
        np.array([p.latency_ns for p in placements], dtype=np.int64),
        np.array([p.side for p in placements], dtype=np.int8),
        np.array([p.price for p in placements], dtype=np.int64),
        np.array([p.size for p in placements], dtype=np.int64),
        np.array([p.horizon_ns for p in placements], dtype=np.int64),
        int(cancel_model),
    )


def compute_outcomes(
    events: np.ndarray,
    placements: list[Placement],
    engine: str,
    cancel_model: int = CancelModel.FRONT,
) -> tuple[dict[str, np.ndarray], dict[str, int]]:
    """Run one engine and return (outcome columns, diagnostics).

    Shared with the experiment runner so a headline number and a reproduction
    run cannot diverge in how they invoked the engine.
    """
    if engine == "cpp":
        result = _run_cpp(events, placements, cancel_model)
        columns = {f: np.asarray(result[f], dtype="int64") for f in OUTCOME_FIELDS}
        return columns, {
            "unknown_order_assumed_ahead": int(result["unknown_order_assumed_ahead"]),
            "fifo_violations": int(result["fifo_violations"]),
            "unknown_order_events": int(result["unknown_order_events"]),
        }
    if engine == "python":
        outcomes, raw = replay_reference(events, placements, cancel_model)
        columns = {
            f: np.array([getattr(o, f) for o in outcomes], dtype="int64") for f in OUTCOME_FIELDS
        }
        # Reported for both engines, not just the fast one: the definition of
        # done asks for fifo_violations to be reported, never silently dropped.
        return columns, {k: int(v) for k, v in raw.items()}
    raise ValueError(f"unknown engine: {engine!r}")


def _to_table(columns: dict[str, np.ndarray]) -> pa.Table:
    """Build the outcome table, nulling rows for placements that never existed.

    Amendment E. ``-1`` stays in the in-memory Outcome so the equivalence test
    keeps comparing raw integers, but it must not reach disk: a sentinel
    silently regresses as a covariate downstream, where a null propagates or
    raises. The two fill-timestamp columns keep ``-1`` on activated rows,
    because "this order existed and did not fill" is a real observation.
    """
    never_existed = columns["status"] == int(Status.NOT_ACTIVATED)
    arrays = {
        name: pa.array(values) if name in ALWAYS_POPULATED else pa.array(values, mask=never_existed)
        for name, values in columns.items()
    }
    return pa.table(arrays)


def run_ground_truth(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    grid_ns: int,
    size: int,
    horizon_ns: int,
    latency_ns: int,
    engine: str = "cpp",
) -> dict[str, Any]:
    """Compute never-cancel ground truth and write outcomes.parquet + manifest.json."""
    message_path = Path(message_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events, input_format = load_messages(message_path)
    placements = place_top_of_book_grid(
        events,
        grid_ns=grid_ns,
        size=size,
        horizon_ns=horizon_ns,
        latency_ns=latency_ns,
    )

    columns, diagnostics = compute_outcomes(events, placements, engine)

    pq.write_table(_to_table(columns), out_dir / "outcomes.parquet")

    manifest = {
        "git_sha": git_sha(),
        "input_path": str(message_path),
        "input_format": input_format,
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "n_placements": len(placements),
        "engine": engine,
        "environment": environment(),
        "diagnostics": diagnostics,
        "config": {
            "grid_ns": grid_ns,
            "size": size,
            "horizon_ns": horizon_ns,
            "latency_ns": latency_ns,
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute shadow-order ground truth")
    parser.add_argument("--config", help="flat key: value run config")
    parser.add_argument("--message-path")
    parser.add_argument("--out-dir")
    parser.add_argument("--grid-ns", type=int)
    parser.add_argument("--size", type=int)
    parser.add_argument("--horizon-ns", type=int)
    parser.add_argument("--latency-ns", type=int)
    parser.add_argument("--engine", choices=["cpp", "python"])
    args = parser.parse_args()

    # Precedence: explicit flag beats config file beats built-in default, so a
    # config can be checked in and still overridden for a one-off run.
    settings: dict[str, Any] = dict(DEFAULTS)
    if args.config:
        settings.update(load_config(args.config))
    settings.update({k: v for k, v in vars(args).items() if k != "config" and v is not None})

    for required in ("message_path", "out_dir"):
        if not settings.get(required):
            parser.error(f"--{required.replace('_', '-')} is required (or set it in --config)")

    manifest = run_ground_truth(**{k: settings[k] for k in _CONFIG_SCHEMA})
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
