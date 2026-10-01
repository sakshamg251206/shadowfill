"""Render the H1 headline chart from a committed manifest.

Reads every plotted number from the manifest, never from a hand-typed table, so
the image is traceable to the run that produced it exactly as the README tables
are. Needs matplotlib: pip install -e ".[plot]"

    python scripts/plot_h1.py \
        --manifest results/h1-aapl-2019-12-30/manifest.json \
        --out docs/img/h1-aapl-2019-12-30.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HORIZON_LABELS = {
    100_000_000: "100 ms",
    1_000_000_000: "1 s",
    10_000_000_000: "10 s",
    60_000_000_000: "60 s",
}

SURFACE, INK, INK_2, MUTED, GRID, BASELINE = (
    "#fcfcfb",
    "#0b0b0b",
    "#52514e",
    "#898781",
    "#e1e0d9",
    "#c3c2b7",
)
TRUTH_COLOR, ESTIMATE_COLOR = "#2a78d6", "#eb6834"


def plot(manifest_path: Path, out_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text())
    rows = [r for r in manifest["rows"] if r["stratum"] == "all"]
    rows.sort(key=lambda r: r["horizon_ns"])
    horizons = [HORIZON_LABELS[r["horizon_ns"]] for r in rows]
    truth = [100 * r["ground_truth"] for r in rows]
    estimate = [100 * r["naive_km"] for r in rows]
    last = rows[-1]

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 13})
    fig = plt.figure(figsize=(12, 6.27), dpi=150, facecolor=SURFACE)
    ax = fig.add_axes((0.07, 0.17, 0.9, 0.56), facecolor=SURFACE)

    width, gap = 0.36, 0.02
    for i in range(len(rows)):
        x_truth, x_est = i - width - gap / 2, i + gap / 2
        ax.add_patch(plt.Rectangle((x_truth, 0), width, truth[i], fc=TRUTH_COLOR, zorder=3))
        ax.add_patch(plt.Rectangle((x_est, 0), width, estimate[i], fc=ESTIMATE_COLOR, zorder=3))
        ax.text(
            x_truth + width / 2,
            truth[i] + 0.9,
            f"{truth[i]:.1f}%",
            ha="center",
            va="bottom",
            color=INK,
            fontsize=12,
            fontweight="bold",
        )
        ax.text(
            x_est + width / 2,
            estimate[i] + 0.9,
            f"{estimate[i]:.1f}%",
            ha="center",
            va="bottom",
            color=INK_2,
            fontsize=12,
        )

    top = 10 * (int(max(truth) // 10) + 1)
    ticks = list(range(0, top + 1, 10))
    ax.set_xlim(-0.6, len(rows) - 0.4)
    ax.set_ylim(0, top)
    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels(horizons, color=INK_2, fontsize=13)
    ax.set_yticks(ticks)
    ax.set_yticklabels([f"{t}%" for t in ticks], color=MUTED, fontsize=11)
    ax.yaxis.grid(True, color=GRID, lw=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(length=0, pad=8)
    ax.set_xlabel("time the order rests in the queue", color=MUTED, fontsize=11, labelpad=8)

    fig.text(
        0.07,
        0.93,
        f"A backtest says {estimate[-1]:.0f}% of passive orders fill within a minute. "
        f"The truth is {truth[-1]:.0f}%.",
        color=INK,
        fontsize=17,
        fontweight="bold",
    )
    fig.text(
        0.07,
        0.875,
        "Share of passive orders filled within each horizon · AAPL, Nasdaq TotalView-ITCH, "
        f"2019-12-30, {manifest['n_orders']:,} orders",
        color=INK_2,
        fontsize=12.5,
    )
    legend = [
        (TRUTH_COLOR, "Computed ground truth (never-cancel shadow order)"),
        (ESTIMATE_COLOR, "Kaplan\u2013Meier, the standard backtest fill estimate"),
    ]
    for i, (color, text) in enumerate(legend):
        x = 0.07 + i * 0.43
        fig.patches.append(
            plt.Rectangle((x, 0.80), 0.012, 0.024, transform=fig.transFigure, fc=color)
        )
        fig.text(x + 0.018, 0.803, text, color=INK, fontsize=12)
    fig.text(
        0.07,
        0.025,
        f"95% block-bootstrap interval on the 60 s error: {100 * last['naive_error_lo']:.1f} to "
        f"{100 * last['naive_error_hi']:.1f} points.  "
        f"Source: {manifest_path.as_posix()} (commit {manifest['git_sha'][:7]})",
        color=MUTED,
        fontsize=10,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=SURFACE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot the H1 headline from a manifest")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    plot(args.manifest, args.out)


if __name__ == "__main__":
    main()
