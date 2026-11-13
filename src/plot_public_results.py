"""从公开 CSV 生成项目展示图；不读取私有权重、数据集或真机日志。"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


PALETTE = {
    "blue": "#0072B2",
    "orange": "#D55E00",
    "green": "#009E73",
    "purple": "#6A3D9A",
    "gray": "#666666",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def save(fig: plt.Figure, path: Path) -> None:
    fig.savefig(path, dpi=180, facecolor="white")
    plt.close(fig)


def plot_runtime_comparison(results: Path, output: Path) -> None:
    rows = read_csv(results / "rtc_runtime_comparison.csv")
    labels = [row["policy"] for row in rows]
    median = [float(row["median"]) for row in rows]
    p95 = [float(row["p95"]) for row in rows]
    x = range(len(labels))
    fig, ax = plt.subplots(figsize=(8.2, 4.6), layout="constrained")
    ax.bar([i - 0.18 for i in x], median, width=0.36, color=PALETTE["blue"], label="median")
    ax.bar([i + 0.18 for i in x], p95, width=0.36, color=PALETTE["orange"], label="p95")
    ax.set_xticks(list(x), labels, rotation=18, ha="right")
    ax.set_ylabel("Prediction age (ms)")
    ax.set_title("Live timing probe: prediction age")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False, ncol=2)
    ax.text(0.01, -0.25, "Same probe family, separate short runs; descriptive timing evidence, not a causal RTC ablation.", transform=ax.transAxes, fontsize=8.5, color=PALETTE["gray"])
    save(fig, output / "rtc-runtime-comparison.png")


def plot_action_trajectory(results: Path, output: Path) -> None:
    rows = read_csv(results / "rtc_action_trajectory.csv")
    grouped: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    for row in rows:
        grouped[(row["checkpoint"], row["joint"])].append((int(row["step"]), float(row["position_deg"])))
    fig, axes = plt.subplots(2, 1, figsize=(8.2, 6.0), sharex=True, layout="constrained")
    colors = {"π0.5 RTC 20k": PALETTE["blue"], "π0.5 RTC + DAgger 1k": PALETTE["orange"]}
    for ax, joint in zip(axes, ["shoulder_lift", "gripper"]):
        for checkpoint, color in colors.items():
            points = sorted(grouped[(checkpoint, joint)])
            ax.plot([p[0] for p in points], [p[1] for p in points], color=color, linewidth=1.6, label=checkpoint)
        ax.set_ylabel(f"{joint} (deg)")
        ax.grid(alpha=0.25)
    axes[-1].set_xlabel("Replay action index")
    axes[0].set_title("Action trajectory under the same recorded observation sequence")
    axes[0].legend(frameon=False, ncol=2)
    axes[-1].text(0.01, -0.28, "Offline replay only: no motor command was sent; trajectories describe checkpoint behavior.", transform=axes[-1].transAxes, fontsize=8.5, color=PALETTE["gray"])
    save(fig, output / "rtc-action-trajectory.png")


def plot_latency_distribution(results: Path, output: Path) -> None:
    rows = read_csv(results / "rtc_latency_distribution.csv")
    turnaround = [float(row["turnaround_ms"]) for row in rows]
    source_age = [float(row["source_age_ms"]) for row in rows]
    fig, axes = plt.subplots(1, 2, figsize=(8.2, 3.9), layout="constrained")
    for ax, values, title, color in [
        (axes[0], turnaround, "RTC turnaround", PALETTE["blue"]),
        (axes[1], source_age, "Source-frame age", PALETTE["orange"]),
    ]:
        ax.hist(values, bins=24, color=color, alpha=0.82, edgecolor="white")
        ax.axvline(sorted(values)[len(values) // 2], color="#222222", linewidth=1.3, linestyle="--", label="median")
        ax.set_title(title)
        ax.set_xlabel("Milliseconds")
        ax.set_ylabel("Results")
        ax.grid(axis="y", alpha=0.25)
        ax.legend(frameon=False)
    fig.suptitle("π0.5 Training-Time RTC live latency distribution")
    fig.text(0.01, -0.03, "626 official results from three consecutive step-1000 runs; queue playback is reported separately in the CSV.", fontsize=8.5, color=PALETTE["gray"])
    save(fig, output / "rtc-latency-distribution.png")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--output", type=Path, default=Path("assets/figures"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    plot_runtime_comparison(args.results, args.output)
    plot_action_trajectory(args.results, args.output)
    plot_latency_distribution(args.results, args.output)


if __name__ == "__main__":
    main()
