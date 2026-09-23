"""Plan 4's latency sweep: the H1 comparison repeated across order-entry latency.

RESEARCH-SPEC §7 asks for sensitivity to latency rather than a single number,
because every real participant is slower than the counterfactual one.

**Reading the output: 0 -> 1 ns is a change of question, not of speed.**
Matched placement timestamps each shadow one nanosecond before the real order
it twins, so at zero latency the shadow takes the twin's exact queue slot. That
is the counterfactual "would *this* order have filled had it not cancelled" --
the question survival estimators claim to answer, and the one H1 reports. At
any latency of at least 1 ns the twin reaches the book first, and the shadow
sits exactly one twin-order-size further back: "what if a *copy* of this order
had arrived Δ later". The step between the two is measured exactly, per
shadow, in ``tests/python/test_latency.py``. Everything beyond 1 ns is genuine
latency, and it can only cost queue position.

The observational side does not move with latency -- Kaplan-Meier is fitted on
real orders, which have no latency -- so every change in the error comes from
the ground truth.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .bias import DEFAULT_HORIZONS, CurveComparison, block_bootstrap_errors, compare_fill_curves
from .experiment import DEFAULT_BLOCK_NS, REGULAR_HOURS_NS, _rows_with_interval
from .ground_truth import compute_outcomes, load_messages
from .lifetimes import extract_lifetimes
from .placements import place_matched_to_orders
from .provenance import environment, git_sha, sha256_file
from .replay import Status

#: 0, then 1 ns (the change of question), then 100 us to 20 ms of real latency.
#: Colocated equity participants sit in the tens of microseconds; retail and
#: cloud-hosted flow in the milliseconds.
DEFAULT_LATENCIES_NS = (0, 1, 100_000, 1_000_000, 5_000_000, 20_000_000)


@dataclass(frozen=True)
class LatencyRow:
    """The H1 comparison at one latency."""

    latency_ns: int
    comparison: CurveComparison
    mean_ahead_at_insert: float
    columns: dict[str, np.ndarray]


def sweep_latency(
    events: np.ndarray,
    latencies_ns: Sequence[int],
    *,
    horizon_ns: int,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    engine: str = "cpp",
) -> list[LatencyRow]:
    """Run the matched H1 comparison once per latency, over the same events."""
    lives = extract_lifetimes(events)
    end_ts = int(events["ts_ns"][-1])
    rows: list[LatencyRow] = []
    for latency in latencies_ns:
        placements = place_matched_to_orders(events, horizon_ns=horizon_ns, latency_ns=latency)
        columns, _ = compute_outcomes(events, placements, engine)
        comparison = compare_fill_curves(
            columns,
            lives,
            horizon_ns=horizon_ns,
            end_ts=end_ts,
            horizons=horizons,
            at_touch_only=False,
        )
        activated = columns["status"] != int(Status.NOT_ACTIVATED)
        rows.append(
            LatencyRow(
                latency_ns=int(latency),
                comparison=comparison,
                mean_ahead_at_insert=float(columns["ahead_at_insert"][activated].mean()),
                columns=columns,
            )
        )
    return rows


def run_latency(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    latencies_ns: Sequence[int] = DEFAULT_LATENCIES_NS,
    horizon_ns: int = 60_000_000_000,
    horizons: np.ndarray = DEFAULT_HORIZONS,
    engine: str = "cpp",
    block_ns: int = DEFAULT_BLOCK_NS,
    n_replicates: int = 200,
    seed: int = 0,
    window_ns: tuple[int, int] | None = REGULAR_HOURS_NS,
) -> dict[str, Any]:
    """Sweep latency on one session; write latency_sweep.parquet + manifest.json."""
    message_path = Path(message_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events, input_format = load_messages(message_path)
    n_events_total = len(events)
    if window_ns is not None:
        events = events[(events["ts_ns"] >= window_ns[0]) & (events["ts_ns"] < window_ns[1])]
    if len(events) == 0:
        raise ValueError(f"{message_path} has no events in window {window_ns}")

    lives = extract_lifetimes(events)
    end_ts = int(events["ts_ns"][-1])
    sweep = sweep_latency(
        events, latencies_ns, horizon_ns=horizon_ns, horizons=horizons, engine=engine
    )

    rows: list[dict[str, Any]] = []
    n_blocks = 0
    for entry in sweep:
        interval = block_bootstrap_errors(
            entry.columns,
            lives,
            horizon_ns=horizon_ns,
            end_ts=end_ts,
            horizons=horizons,
            at_touch_only=False,
            block_ns=block_ns,
            n_replicates=n_replicates,
            seed=seed,
        )
        n_blocks = int(interval["n_blocks"])
        for row in _rows_with_interval(entry.comparison, interval):
            row["latency_ns"] = entry.latency_ns
            row["mean_ahead_at_insert"] = entry.mean_ahead_at_insert
            rows.append(row)
    pq.write_table(pa.Table.from_pylist(rows), out_dir / "latency_sweep.parquet")

    manifest: dict[str, Any] = {
        "git_sha": git_sha(),
        "experiment": "latency-sweep",
        "input_path": str(message_path),
        "input_format": input_format,
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "n_events_before_window": n_events_total,
        "window_ns": list(window_ns) if window_ns else None,
        "engine": engine,
        "environment": environment(),
        "config": {
            "latencies_ns": [int(x) for x in latencies_ns],
            "horizon_ns": horizon_ns,
            "horizons_ns": [int(h) for h in horizons],
            "placement": "matched",
            "block_ns": block_ns,
            "n_replicates": n_replicates,
            "n_blocks": n_blocks,
            "seed": seed,
        },
        "interpretation": (
            "0 ns: the shadow takes its real twin's queue slot (the H1 question). "
            ">= 1 ns: the twin arrives first and the shadow sits behind it."
        ),
        "bootstrap_scope": "within-session blocks only",
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def format_table(rows: list[dict[str, Any]], horizon_ns: int) -> str:
    """One line per latency at a single horizon."""
    lines = [f"{'latency':>10}  {'F*':>7}  {'KM':>7}  {'error':>8}  95% CI              ahead"]
    for r in rows:
        if r["horizon_ns"] != horizon_ns:
            continue
        lines.append(
            f"{r['latency_ns']:>10}  {r['ground_truth']:>7.4f}  {r['naive_km']:>7.4f}  "
            f"{r['naive_error']:>+8.4f}  [{r['naive_error_lo']:+.4f}, {r['naive_error_hi']:+.4f}]"
            f"  {r['mean_ahead_at_insert']:>8.1f}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan 4 latency sweep")
    parser.add_argument("--message-path", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--n-replicates", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--engine", choices=["cpp", "python"], default="cpp")
    args = parser.parse_args()

    manifest = run_latency(
        message_path=args.message_path,
        out_dir=args.out_dir,
        n_replicates=args.n_replicates,
        seed=args.seed,
        engine=args.engine,
    )
    for h in manifest["config"]["horizons_ns"]:
        print(f"\nhorizon {h / 1e9:g} s")
        print(format_table(manifest["rows"], h))


if __name__ == "__main__":
    main()
