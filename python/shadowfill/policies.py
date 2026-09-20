"""H5: does the correction change which execution policy you would choose?

A bias can be real, large, and irrelevant. It matters only if acting on the
uncorrected estimate would lead a desk to a different decision. The spec is
explicit about the null: if the rankings never invert, the correction is
academically true and practically irrelevant, and the paper says so.

**The policy class has to contain a genuine trade-off**, or the test is empty.
Ranking pure cancel-at-T policies by markout alone is degenerate: markouts on a
passive fill are negative here, so "never get filled" always wins. So the class
is the decision an execution desk actually faces --

    rest passively at the touch for T, and cross the spread if still unfilled

-- where waiting longer earns more passive fills but suffers more adverse
selection, and waiting less pays the spread more often::

    edge(T) = F(T) x E[m | filled by T] - (1 - F(T)) x cross_cost

Both terms move with T and in opposite directions, so the best T is an interior
choice and the ranking is informative.

**Why a policy is evaluable without re-simulating.** A cancel-at-T policy's
fill outcome follows from the never-cancel one: the order fills under the
policy exactly when its never-cancel fill time is at or before T. That is sound
only because the shadow does not change anyone else's behaviour -- the
project's one standing assumption, stated in the README and stress-tested in
Plan 4 rather than waved away. Your own cancellation cannot move the queue
ahead of you.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

from .bias import fill_cdf, ground_truth_arrays, observational_arrays
from .experiment import DEFAULT_BLOCK_NS, REGULAR_HOURS_NS
from .ground_truth import compute_outcomes, load_messages
from .lifetimes import NO_FILL, extract_lifetimes
from .markout import markout_bps, top_of_book_series
from .placements import place_matched_to_orders
from .provenance import environment, git_sha, sha256_file

SEC = 1_000_000_000

#: Rest for this long, then cross. Spans three orders of magnitude, because a
#: desk's real choice is between "cross almost immediately" and "be patient".
DEFAULT_WAITS_NS = (100_000_000, SEC, 5 * SEC, 30 * SEC, 60 * SEC)


@dataclass(frozen=True)
class PolicyEdge:
    """One policy's expected edge, and the parts it is made of."""

    wait_ns: int
    fill_rate: float
    markout_bps: float
    cross_cost_bps: float
    edge_bps: float


def median_half_spread_bps(tob: np.ndarray) -> float:
    """Typical cost of crossing, in basis points of the mid.

    The median over the session rather than the spread at each decision: a
    per-order spread would need the touch at every cancel time and would make
    the crossing cost depend on exactly the queue state the policies differ in,
    which mixes the cost model into the thing being measured.
    """
    bid = tob["best_bid"].astype(float)
    ask = tob["best_ask"].astype(float)
    ok = (bid > 0) & (ask > 0)
    if not ok.any():
        raise ValueError("book was never two-sided; no spread to measure")
    mid = (bid[ok] + ask[ok]) / 2.0
    return float(np.median((ask[ok] - bid[ok]) / 2.0 / mid * 10_000.0))


def evaluate_policies(
    durations: np.ndarray,
    events_flag: np.ndarray,
    filled: np.ndarray,
    marks: np.ndarray,
    *,
    cross_cost_bps: float,
    waits_ns: tuple[int, ...] = DEFAULT_WAITS_NS,
) -> list[PolicyEdge]:
    """Expected edge of each rest-then-cross policy.

    ``durations`` are times to fill or censoring, ``filled`` marks the observed
    fills, and ``marks`` their markouts. The fill rate uses the product-limit
    fit so orders still resting when the window closed are censored rather than
    counted as failures; the conditional markout is the mean over fills that
    landed inside the wait.
    """
    waits = np.asarray(waits_ns, dtype=np.int64)
    rates = fill_cdf(durations, events_flag, waits)
    out: list[PolicyEdge] = []
    for i, wait in enumerate(waits):
        sel = filled & (durations <= wait) & ~np.isnan(marks)
        mark = float(np.mean(marks[sel])) if sel.any() else 0.0
        rate = float(rates[i])
        out.append(
            PolicyEdge(
                wait_ns=int(wait),
                fill_rate=rate,
                markout_bps=mark,
                cross_cost_bps=cross_cost_bps,
                edge_bps=rate * mark - (1.0 - rate) * cross_cost_bps,
            )
        )
    return out


def rank_inversions(
    true_edges: list[PolicyEdge], est_edges: list[PolicyEdge]
) -> tuple[int, int, float]:
    """(pairs compared, pairs ordered differently, inversion rate).

    Pairwise rather than a rank correlation, because the decision a desk makes
    is a comparison between two candidate policies, and a single pair flipping
    is the thing that costs money.
    """
    pairs = inverted = 0
    for i, j in combinations(range(len(true_edges)), 2):
        truth = np.sign(true_edges[i].edge_bps - true_edges[j].edge_bps)
        est = np.sign(est_edges[i].edge_bps - est_edges[j].edge_bps)
        pairs += 1
        if truth != est:
            inverted += 1
    return pairs, inverted, (inverted / pairs if pairs else 0.0)


def run_h5(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    horizon_ns: int = 60 * SEC,
    markout_ns: int = SEC,
    waits_ns: tuple[int, ...] = DEFAULT_WAITS_NS,
    engine: str = "cpp",
    window_ns: tuple[int, int] | None = REGULAR_HOURS_NS,
    block_ns: int = DEFAULT_BLOCK_NS,
    n_replicates: int = 200,
    seed: int = 0,
) -> dict[str, Any]:
    """Rank execution policies under the truth and under the estimator."""
    message_path = Path(message_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events, input_format = load_messages(message_path)
    if window_ns is not None:
        events = events[(events["ts_ns"] >= window_ns[0]) & (events["ts_ns"] < window_ns[1])]
    if len(events) == 0:
        raise ValueError(f"{message_path} has no events in window {window_ns}")

    placements = place_matched_to_orders(events, horizon_ns=horizon_ns)
    columns, diagnostics = compute_outcomes(events, placements, engine)
    lives = extract_lifetimes(events)
    tob = top_of_book_series(events)
    cross_cost = median_half_spread_bps(tob)
    end_ts = int(events["ts_ns"][-1])

    keep = np.asarray(columns["status"]) != 4  # NOT_ACTIVATED
    gt_dur, gt_ev, gt_ts, _ = ground_truth_arrays(columns, horizon_ns=horizon_ns, end_ts=end_ts)
    gt_fill_ts = np.asarray(columns["first_fill_ts"])[keep]
    gt_marks = markout_bps(
        gt_fill_ts,
        np.array([p.price for p in placements], dtype=np.int64)[keep],
        np.array([p.side for p in placements], dtype=np.int8)[keep],
        tob,
        markout_ns,
    )
    gt_filled = gt_fill_ts >= 0

    obs_dur, obs_cause, obs_ts, _ = observational_arrays(lives, at_touch_only=False)
    obs_marks = markout_bps(lives["first_fill_ts"], lives["price"], lives["side"], tob, markout_ns)
    obs_filled = np.asarray(lives["first_fill_ts"]) != NO_FILL
    obs_ev = (obs_cause == 1).astype(float)

    def both(gi: np.ndarray, oi: np.ndarray) -> tuple[list[PolicyEdge], list[PolicyEdge]]:
        return (
            evaluate_policies(
                gt_dur[gi], gt_ev[gi], gt_filled[gi], gt_marks[gi],
                cross_cost_bps=cross_cost, waits_ns=waits_ns,
            ),
            evaluate_policies(
                obs_dur[oi], obs_ev[oi], obs_filled[oi], obs_marks[oi],
                cross_cost_bps=cross_cost, waits_ns=waits_ns,
            ),
        )  # fmt: skip

    truth, est = both(np.arange(len(gt_dur)), np.arange(len(obs_dur)))
    pairs, inverted, rate = rank_inversions(truth, est)

    best_true = max(range(len(truth)), key=lambda i: truth[i].edge_bps)
    best_est = max(range(len(est)), key=lambda i: est[i].edge_bps)

    blocks = np.union1d(np.unique(gt_ts // block_ns), np.unique(obs_ts // block_ns))
    if len(blocks) < 2:
        raise ValueError(f"{len(blocks)} time block(s): too few to bootstrap")
    gt_members = {b: np.flatnonzero(gt_ts // block_ns == b) for b in blocks}
    obs_members = {b: np.flatnonzero(obs_ts // block_ns == b) for b in blocks}

    rng = np.random.default_rng(seed)
    rates, disagreements = [], 0
    for _ in range(n_replicates):
        drawn = rng.choice(blocks, size=len(blocks), replace=True)
        gi = np.concatenate([gt_members[b] for b in drawn])
        oi = np.concatenate([obs_members[b] for b in drawn])
        if len(gi) == 0 or len(oi) == 0:
            continue
        t, e = both(gi, oi)
        rates.append(rank_inversions(t, e)[2])
        if max(range(len(t)), key=lambda i: t[i].edge_bps) != max(
            range(len(e)), key=lambda i: e[i].edge_bps
        ):
            disagreements += 1

    manifest = {
        "git_sha": git_sha(),
        "experiment": "H5-decision-relevance",
        "input_path": str(message_path),
        "input_format": input_format,
        "input_sha256": sha256_file(message_path),
        "n_events": len(events),
        "n_placements": len(placements),
        "environment": environment(),
        "diagnostics": diagnostics,
        "cross_cost_bps": cross_cost,
        "inversion_rate": rate,
        "inverted_pairs": inverted,
        "pairs": pairs,
        "inversion_rate_lo": float(np.percentile(rates, 2.5)) if rates else None,
        "inversion_rate_hi": float(np.percentile(rates, 97.5)) if rates else None,
        "best_policy_true_ns": truth[best_true].wait_ns,
        "best_policy_estimated_ns": est[best_est].wait_ns,
        "best_policy_disagreement_rate": disagreements / len(rates) if rates else None,
        "config": {
            "horizon_ns": horizon_ns,
            "markout_ns": markout_ns,
            "waits_ns": [int(w) for w in waits_ns],
            "window_ns": list(window_ns) if window_ns else None,
            "block_ns": block_ns,
            "n_replicates": len(rates),
            "n_blocks": len(blocks),
            "seed": seed,
            "engine": engine,
        },
        "rows": [
            {
                "wait_ns": t.wait_ns,
                "true_fill_rate": t.fill_rate,
                "true_markout_bps": t.markout_bps,
                "true_edge_bps": t.edge_bps,
                "est_fill_rate": e.fill_rate,
                "est_markout_bps": e.markout_bps,
                "est_edge_bps": e.edge_bps,
            }
            for t, e in zip(truth, est, strict=True)
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def format_table(manifest: dict[str, Any]) -> str:
    header = (
        f"{'wait':>8} {'F*':>7} {'m* bps':>8} {'edge* bps':>10} "
        f"{'F^':>7} {'m^ bps':>8} {'edge^ bps':>10}"
    )
    lines = [header, "-" * len(header)]
    for r in manifest["rows"]:
        w = r["wait_ns"] / 1e9
        label = f"{w:g}s" if w >= 1 else f"{w * 1000:g}ms"
        lines.append(
            f"{label:>8} {r['true_fill_rate']:>7.4f} {r['true_markout_bps']:>8.3f} "
            f"{r['true_edge_bps']:>10.4f} {r['est_fill_rate']:>7.4f} "
            f"{r['est_markout_bps']:>8.3f} {r['est_edge_bps']:>10.4f}"
        )
    lines.append("")
    lines.append(
        f"cross cost {manifest['cross_cost_bps']:.3f} bps  |  "
        f"inversions {manifest['inverted_pairs']}/{manifest['pairs']} "
        f"= {manifest['inversion_rate']:.1%}"
    )
    lines.append(
        f"best policy: truth {manifest['best_policy_true_ns'] / 1e9:g}s, "
        f"estimated {manifest['best_policy_estimated_ns'] / 1e9:g}s  |  "
        f"they disagree in {manifest['best_policy_disagreement_rate']:.1%} of replicates"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Does the correction change the decision?")
    parser.add_argument("--message-path", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--markout-ns", type=int, default=SEC)
    parser.add_argument("--block-ns", type=int, default=DEFAULT_BLOCK_NS)
    parser.add_argument("--n-replicates", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    print(
        format_table(
            run_h5(
                message_path=args.message_path,
                out_dir=args.out_dir,
                markout_ns=args.markout_ns,
                block_ns=args.block_ns,
                n_replicates=args.n_replicates,
                seed=args.seed,
            )
        )
    )


if __name__ == "__main__":
    main()
