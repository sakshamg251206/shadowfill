"""H4: what a level-2 feed costs you, measured against the level-3 truth.

Every open-source queue-aware backtester runs on level-2 data, which reports
aggregate size per price level and no order identities. When size leaves a
level, an L2 simulator cannot know *whose* order it was, so it cannot know
whether that size sat ahead of your order or behind it. It guesses, with one of
four standard heuristics -- cancel from front, from back, uniform, or
proportional -- and the guess is never validated, because validating it needs
exactly the L3 ground truth this repository computes.

The ablation is deliberately a *data* transform rather than a second engine.
Blanking the order id on every cancel and delete is precisely what an L2 feed
does to the information, and the identical engine then runs on the degraded
stream. A second simulator would risk measuring the difference between two
implementations instead of the difference between two data feeds.

**Scope.** Only the cancel-from-front heuristic is implemented, because it is
what the engine's unknown-order path already does: an id it cannot resolve is
assumed to have been ahead of the shadow, so the queue shrinks as fast as it
possibly could. That is the most *optimistic* of the four, which makes the
measured error a one-sided bound -- the true L2 penalty is at least this large
for a simulator that assumes the best. From-back and uniform need the tracker
to attribute a fraction of each cancel, which the current engine cannot express.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .bias import DEFAULT_HORIZONS, fill_cdf, ground_truth_arrays
from .events import EventType
from .experiment import DEFAULT_BLOCK_NS, REGULAR_HOURS_NS
from .ground_truth import compute_outcomes, load_messages
from .placements import place_matched_to_orders
from .provenance import environment, git_sha, sha256_file

#: Removals whose order identity an L2 feed does not carry. Executions keep
#: theirs: price-time priority means a trade takes the front of the queue, and
#: an L2 simulator knows that much without any identity.
_ANONYMISED = (int(EventType.CANCEL_PARTIAL), int(EventType.DELETE))


def ablate_to_l2(events: np.ndarray) -> np.ndarray:
    """Strip order identity from cancels, leaving everything else intact.

    This is the whole of the L2 handicap: the same events, at the same times,
    in the same sizes, with the one field an aggregated feed cannot report.
    """
    out = events.copy()
    out["order_id"][np.isin(out["type"], _ANONYMISED)] = 0
    return out


def run_l2_ablation(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    horizon_ns: int = 60_000_000_000,
    latency_ns: int = 0,
    engine: str = "cpp",
    horizons: np.ndarray = DEFAULT_HORIZONS,
    window_ns: tuple[int, int] | None = REGULAR_HOURS_NS,
    block_ns: int = DEFAULT_BLOCK_NS,
    n_replicates: int = 200,
    seed: int = 0,
) -> dict[str, Any]:
    """Run the identical placements against L3 truth and an L2-ablated stream."""
    message_path = Path(message_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events, input_format = load_messages(message_path)
    if window_ns is not None:
        events = events[(events["ts_ns"] >= window_ns[0]) & (events["ts_ns"] < window_ns[1])]
    if len(events) == 0:
        raise ValueError(f"{message_path} has no events in window {window_ns}")

    # The same hypothetical orders on both feeds. Anything else would confound
    # the feed's effect with a difference in what was being simulated.
    placements = place_matched_to_orders(events, horizon_ns=horizon_ns, latency_ns=latency_ns)
    end_ts = int(events["ts_ns"][-1])

    truth_cols, truth_diag = compute_outcomes(events, placements, engine)
    l2_cols, l2_diag = compute_outcomes(ablate_to_l2(events), placements, engine)

    horizons = np.asarray(horizons, dtype=np.int64)
    t_dur, t_ev, t_ts, _ = ground_truth_arrays(truth_cols, horizon_ns=horizon_ns, end_ts=end_ts)
    l_dur, l_ev, l_ts, _ = ground_truth_arrays(l2_cols, horizon_ns=horizon_ns, end_ts=end_ts)
    truth = fill_cdf(t_dur, t_ev, horizons)
    l2 = fill_cdf(l_dur, l_ev, horizons)

    # Same blocks for both feeds: the estimate is a paired difference over one
    # period, so drawing them independently would add variation that cancels.
    blocks = np.union1d(np.unique(t_ts // block_ns), np.unique(l_ts // block_ns))
    if len(blocks) < 2:
        raise ValueError(f"{len(blocks)} time block(s): too few to bootstrap")
    t_members = {b: np.flatnonzero(t_ts // block_ns == b) for b in blocks}
    l_members = {b: np.flatnonzero(l_ts // block_ns == b) for b in blocks}

    rng = np.random.default_rng(seed)
    errors = []
    for _ in range(n_replicates):
        drawn = rng.choice(blocks, size=len(blocks), replace=True)
        t_idx = np.concatenate([t_members[b] for b in drawn])
        l_idx = np.concatenate([l_members[b] for b in drawn])
        if len(t_idx) == 0 or len(l_idx) == 0:
            continue
        errors.append(
            fill_cdf(l_dur[l_idx], l_ev[l_idx], horizons)
            - fill_cdf(t_dur[t_idx], t_ev[t_idx], horizons)
        )
    err = np.array(errors)

    rows = [
        {
            "horizon_ns": int(h),
            "l3_truth": float(truth[i]),
            "l2_cancel_from_front": float(l2[i]),
            "l2_error": float(l2[i] - truth[i]),
            "l2_error_lo": float(np.percentile(err[:, i], 2.5)),
            "l2_error_hi": float(np.percentile(err[:, i], 97.5)),
            "n_shadows": len(t_dur),
        }
        for i, h in enumerate(horizons)
    ]

    manifest = {
        "git_sha": git_sha(),
        "experiment": "H4-l2-ablation",
        "heuristic": "cancel-from-front (the most optimistic of the four)",
        "input_path": str(message_path),
        "input_format": input_format,
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "n_placements": len(placements),
        "environment": environment(),
        "diagnostics": {"l3": truth_diag, "l2": l2_diag},
        "config": {
            "horizon_ns": horizon_ns,
            "latency_ns": latency_ns,
            "horizons_ns": [int(h) for h in horizons],
            "window_ns": list(window_ns) if window_ns else None,
            "block_ns": block_ns,
            "n_replicates": len(err),
            "n_blocks": len(blocks),
            "seed": seed,
            "engine": engine,
        },
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def format_table(rows: list[dict[str, Any]]) -> str:
    header = f"{'horizon':>10}  {'L3 truth':>9}  {'L2 guess':>9}  {'L2 error':>10}  {'95% CI':>22}"
    lines = [header, "-" * len(header)]
    for row in rows:
        h = row["horizon_ns"] / 1e9
        label = f"{h:g}s" if h >= 1 else f"{h * 1000:g}ms"
        ci = f"[{row['l2_error_lo']:+.4f}, {row['l2_error_hi']:+.4f}]"
        lines.append(
            f"{label:>10}  {row['l3_truth']:>9.4f}  {row['l2_cancel_from_front']:>9.4f}  "
            f"{row['l2_error']:>+10.4f}  {ci:>22}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure the level-2 queue-position penalty")
    parser.add_argument("--message-path", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--horizon-ns", type=int, default=60_000_000_000)
    parser.add_argument("--block-ns", type=int, default=DEFAULT_BLOCK_NS)
    parser.add_argument("--n-replicates", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()

    manifest = run_l2_ablation(
        message_path=args.message_path,
        out_dir=args.out_dir,
        horizon_ns=args.horizon_ns,
        block_ns=args.block_ns,
        n_replicates=args.n_replicates,
        seed=args.seed,
        window_ns=None if args.no_window else REGULAR_HOURS_NS,
    )
    print(format_table(manifest["rows"]))


if __name__ == "__main__":
    main()
