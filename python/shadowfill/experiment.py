"""H1 end to end: one command, one input, one reproducible table.

Loads an event stream, places never-cancel shadow orders on a time grid,
computes their fill times by queue arithmetic, extracts the real orders that
rested in the same book, and reports how far the observational estimators sit
from the truth -- with a block-bootstrap interval, because a bias reported
without one cannot be distinguished from sampling noise.

Everything needed to regenerate the table is written to the manifest beside
it: git commit, input hash, engine, config, seed and diagnostics.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .bias import (
    DEFAULT_AHEAD_EDGES,
    DEFAULT_HORIZONS,
    CurveComparison,
    block_bootstrap_errors,
    compare_by_ahead,
    compare_fill_curves,
)
from .ground_truth import compute_outcomes, load_messages
from .lifetimes import extract_lifetimes
from .placements import place_top_of_book_grid
from .provenance import environment, git_sha, sha256_file

#: Half an hour. Long enough that queue dynamics decorrelate across blocks,
#: short enough that a single session still yields a usable number of them.
DEFAULT_BLOCK_NS = 1_800_000_000_000


def run_h1(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    grid_ns: int = 100_000_000,
    size: int = 100,
    horizon_ns: int = 60_000_000_000,
    latency_ns: int = 0,
    engine: str = "cpp",
    horizons: np.ndarray = DEFAULT_HORIZONS,
    at_touch_only: bool = True,
    block_ns: int = DEFAULT_BLOCK_NS,
    n_replicates: int = 200,
    seed: int = 0,
    ahead_edges: Sequence[int] = DEFAULT_AHEAD_EDGES,
) -> dict[str, Any]:
    """Measure H1 on one session and write h1_curves.parquet + manifest.json."""
    message_path = Path(message_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events, input_format = load_messages(message_path)
    if len(events) == 0:
        raise ValueError(f"{message_path} contains no events")

    placements = place_top_of_book_grid(
        events, grid_ns=grid_ns, size=size, horizon_ns=horizon_ns, latency_ns=latency_ns
    )
    columns, diagnostics = compute_outcomes(events, placements, engine)
    lives = extract_lifetimes(events)
    end_ts = int(events["ts_ns"][-1])

    comparison = compare_fill_curves(
        columns,
        lives,
        horizon_ns=horizon_ns,
        end_ts=end_ts,
        horizons=horizons,
        at_touch_only=at_touch_only,
    )
    interval = block_bootstrap_errors(
        columns,
        lives,
        horizon_ns=horizon_ns,
        end_ts=end_ts,
        horizons=horizons,
        at_touch_only=at_touch_only,
        block_ns=block_ns,
        n_replicates=n_replicates,
        seed=seed,
    )

    rows = _rows_with_interval(comparison, interval)
    # Stratified by queue-ahead. The unconditional row above blends a real
    # composition difference into the bias -- shadows are placed on a clock,
    # real orders arrive when someone chose to send them -- so the strata are
    # the defensible comparison and the "all" row is context for them.
    strata = compare_by_ahead(
        columns,
        lives,
        horizon_ns=horizon_ns,
        end_ts=end_ts,
        horizons=horizons,
        at_touch_only=at_touch_only,
        edges=ahead_edges,
    )
    for stratum in strata:
        stratum_interval = block_bootstrap_errors(
            columns,
            lives,
            horizon_ns=horizon_ns,
            end_ts=end_ts,
            horizons=horizons,
            at_touch_only=at_touch_only,
            block_ns=block_ns,
            n_replicates=n_replicates,
            seed=seed,
            ahead_range=stratum.ahead_range,
        )
        rows.extend(_rows_with_interval(stratum, stratum_interval))
    pq.write_table(pa.Table.from_pylist(rows), out_dir / "h1_curves.parquet")

    manifest = {
        "git_sha": git_sha(),
        "experiment": "H1",
        "input_path": str(message_path),
        "input_format": input_format,
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "n_placements": len(placements),
        "n_shadows_activated": comparison.n_shadows,
        "n_orders": comparison.n_orders,
        "n_orders_total": len(lives),
        "engine": engine,
        "environment": environment(),
        "diagnostics": diagnostics,
        "config": {
            "grid_ns": grid_ns,
            "size": size,
            "horizon_ns": horizon_ns,
            "latency_ns": latency_ns,
            "horizons_ns": [int(h) for h in horizons],
            "at_touch_only": at_touch_only,
            "block_ns": block_ns,
            "n_replicates": int(interval["n_replicates"]),
            "n_blocks": int(interval["n_blocks"]),
            "seed": seed,
            "ahead_edges": [int(e) for e in ahead_edges],
        },
        "bootstrap_scope": (
            "within-session blocks only; day-to-day variation is not covered "
            "and needs more than one session"
        ),
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _rows_with_interval(
    comparison: CurveComparison, interval: dict[str, np.ndarray]
) -> list[dict[str, Any]]:
    """Attach the bootstrap bounds to each horizon's row."""
    rows = comparison.as_rows()
    for i, row in enumerate(rows):
        for key in (
            "naive_error_lo",
            "naive_error_hi",
            "competing_risks_error_lo",
            "competing_risks_error_hi",
        ):
            row[key] = float(interval[key][i])
    return rows


def format_table(rows: list[dict[str, Any]]) -> str:
    """Human-readable summary of the rows, for a terminal or a README."""
    header = (
        f"{'stratum':>14}  {'horizon':>8}  {'truth F*':>9}  {'naive KM':>9}  {'AJ CIF':>9}  "
        f"{'naive err':>10}  {'n_shadow':>9}  {'n_order':>8}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        horizon = row["horizon_ns"] / 1e9
        label = f"{horizon:g}s" if horizon >= 1 else f"{horizon * 1000:g}ms"
        lines.append(
            f"{row['stratum']:>14}  {label:>8}  {row['ground_truth']:>9.4f}  "
            f"{row['naive_km']:>9.4f}  {row['competing_risks']:>9.4f}  "
            f"{row['naive_error']:>+10.4f}  {row['n_shadows']:>9,}  {row['n_orders']:>8,}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure H1 on one session")
    parser.add_argument("--message-path", required=True, help=".parquet events or LOBSTER .csv")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--grid-ns", type=int, default=100_000_000)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--horizon-ns", type=int, default=60_000_000_000)
    parser.add_argument("--latency-ns", type=int, default=0)
    parser.add_argument("--engine", choices=["cpp", "python"], default="cpp")
    parser.add_argument("--block-ns", type=int, default=DEFAULT_BLOCK_NS)
    parser.add_argument("--n-replicates", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--all-depths",
        action="store_true",
        help="do not restrict real orders to the touch (not comparable with shadows)",
    )
    args = parser.parse_args()

    manifest = run_h1(
        message_path=args.message_path,
        out_dir=args.out_dir,
        grid_ns=args.grid_ns,
        size=args.size,
        horizon_ns=args.horizon_ns,
        latency_ns=args.latency_ns,
        engine=args.engine,
        at_touch_only=not args.all_depths,
        block_ns=args.block_ns,
        n_replicates=args.n_replicates,
        seed=args.seed,
    )
    print(format_table(manifest["rows"]))
    print(
        f"\n{manifest['n_shadows_activated']:,} shadows vs {manifest['n_orders']:,} real orders"
        f"  |  {manifest['config']['n_blocks']} blocks, "
        f"{manifest['config']['n_replicates']} replicates"
    )


if __name__ == "__main__":
    main()
