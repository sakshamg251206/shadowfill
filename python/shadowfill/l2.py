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

**Heuristics.** Three are measured, each a ``CancelModel`` in the engine's
anonymous-cancel path (see ``replay.CancelModel`` for the formulas):

* ``front`` -- every anonymous cancel was ahead of you. The most optimistic:
  the queue shrinks as fast as it possibly could.
* ``proportional`` -- the cancelled shares were uniform over the level. The
  expected value, and what a careful simulator would assume.
* ``back`` -- every anonymous cancel was behind you until behind runs out. The
  most pessimistic; hftbacktest calls this the risk-averse model.

Per shadow, fills order front >= proportional >= back, proven by induction and
pinned in ``tests/python/test_cancel_models.py``, so the three together bracket
what any of these simulators would report. The fourth heuristic in the
literature, uniform over *orders*, needs an order count that an L2 feed does
not carry, so on L2 it collapses into proportional.
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
from .replay import CancelModel

#: Removals whose order identity an L2 feed does not carry. Executions keep
#: theirs: price-time priority means a trade takes the front of the queue, and
#: an L2 simulator knows that much without any identity.
_ANONYMISED = (int(EventType.CANCEL_PARTIAL), int(EventType.DELETE))

#: Ordered optimistic to pessimistic, which is also the order their fill curves
#: take. Names are the manifest's; values are the engine's.
HEURISTICS = {
    "front": CancelModel.FRONT,
    "proportional": CancelModel.PROPORTIONAL,
    "back": CancelModel.BACK,
}


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
    ablated = ablate_to_l2(events)

    horizons = np.asarray(horizons, dtype=np.int64)
    t_dur, t_ev, t_ts, _ = ground_truth_arrays(truth_cols, horizon_ns=horizon_ns, end_ts=end_ts)
    truth = fill_cdf(t_dur, t_ev, horizons)

    guesses: dict[str, dict[str, Any]] = {}
    for name, model in HEURISTICS.items():
        cols, diag = compute_outcomes(ablated, placements, engine, cancel_model=model)
        dur, ev, ts, _ = ground_truth_arrays(cols, horizon_ns=horizon_ns, end_ts=end_ts)
        guesses[name] = {
            "dur": dur,
            "ev": ev,
            "ts": ts,
            "diag": diag,
            "curve": fill_cdf(dur, ev, horizons),
        }

    # Same blocks for the truth and for every heuristic: each estimate is a
    # paired difference over one period, and pairing across heuristics too
    # means their intervals are directly comparable.
    all_ts = [t_ts, *(g["ts"] for g in guesses.values())]
    blocks = np.unique(np.concatenate([np.unique(x // block_ns) for x in all_ts]))
    if len(blocks) < 2:
        raise ValueError(f"{len(blocks)} time block(s): too few to bootstrap")
    t_members = {b: np.flatnonzero(t_ts // block_ns == b) for b in blocks}
    members = {
        name: {b: np.flatnonzero(g["ts"] // block_ns == b) for b in blocks}
        for name, g in guesses.items()
    }

    rng = np.random.default_rng(seed)
    errors: dict[str, list[np.ndarray]] = {name: [] for name in HEURISTICS}
    for _ in range(n_replicates):
        drawn = rng.choice(blocks, size=len(blocks), replace=True)
        t_idx = np.concatenate([t_members[b] for b in drawn])
        if len(t_idx) == 0:
            continue
        t_curve = fill_cdf(t_dur[t_idx], t_ev[t_idx], horizons)
        for name, g in guesses.items():
            idx = np.concatenate([members[name][b] for b in drawn])
            if len(idx) == 0:
                continue
            errors[name].append(fill_cdf(g["dur"][idx], g["ev"][idx], horizons) - t_curve)

    rows = []
    for name, g in guesses.items():
        err = np.array(errors[name])
        for i, h in enumerate(horizons):
            rows.append(
                {
                    "heuristic": name,
                    "horizon_ns": int(h),
                    "l3_truth": float(truth[i]),
                    "l2_estimate": float(g["curve"][i]),
                    "l2_error": float(g["curve"][i] - truth[i]),
                    "l2_error_lo": float(np.percentile(err[:, i], 2.5)),
                    "l2_error_hi": float(np.percentile(err[:, i], 97.5)),
                    "n_shadows": len(t_dur),
                }
            )

    manifest = {
        "git_sha": git_sha(),
        "experiment": "H4-l2-ablation",
        "input_path": str(message_path),
        "input_format": input_format,
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "n_placements": len(placements),
        "environment": environment(),
        "diagnostics": {"l3": truth_diag, **{f"l2_{n}": g["diag"] for n, g in guesses.items()}},
        "config": {
            "horizon_ns": horizon_ns,
            "latency_ns": latency_ns,
            "horizons_ns": [int(h) for h in horizons],
            "window_ns": list(window_ns) if window_ns else None,
            "block_ns": block_ns,
            "heuristics": list(HEURISTICS),
            "n_replicates": min(len(v) for v in errors.values()),
            "n_blocks": len(blocks),
            "seed": seed,
            "engine": engine,
        },
        "rows": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def format_table(rows: list[dict[str, Any]]) -> str:
    header = (
        f"{'heuristic':>13}  {'horizon':>8}  {'L3 truth':>9}  {'L2 guess':>9}  "
        f"{'L2 error':>9}  {'95% CI':>20}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        h = row["horizon_ns"] / 1e9
        label = f"{h:g}s" if h >= 1 else f"{h * 1000:g}ms"
        ci = f"[{row['l2_error_lo']:+.4f}, {row['l2_error_hi']:+.4f}]"
        lines.append(
            f"{row['heuristic']:>13}  {label:>8}  {row['l3_truth']:>9.4f}  "
            f"{row['l2_estimate']:>9.4f}  {row['l2_error']:>+9.4f}  {ci:>20}"
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
