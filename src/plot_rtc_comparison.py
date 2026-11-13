"""生成普通 π0.5 与 Training-Time RTC 的真机对比图。"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt


COLORS = {"π0.5 Inference-Time RTC": "#0072B2", "π0.5 Training-Time RTC": "#D55E00"}


def read_rows(path: Path) -> list[dict[str, str]]:
    """读取汇总 CSV。"""
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def val(row: dict[str, str], key: str) -> float:
    """读取必需的数值字段。"""
    return float(row[key])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--output", type=Path, default=Path("assets/figures/rtc-controlled-comparison.png"))
    args = parser.parse_args()
    rows = read_rows(args.results / "rtc_comparison_summary.csv")
    labels = [row["condition"] for row in rows]
    x = list(range(len(labels)))
    fig, axes = plt.subplots(2, 2, figsize=(9.2, 6.4), layout="constrained")

    def bars(ax, key: str, title: str, ylabel: str, scale: float = 1.0) -> None:
        values = [val(row, key) * scale for row in rows]
        ax.bar(x, values, color=[COLORS[label] for label in labels], width=0.62)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xticks(x, ["Inference-Time\nRTC", "Training-Time\nRTC"])
        ax.grid(axis="y", alpha=0.25)
        for index, value in enumerate(values):
            ax.text(index, value, f"{value:.1f}", ha="center", va="bottom", fontsize=8)

    bars(axes[0, 0], "success_rate", "Task success", "Rate", 100)
    axes[0, 0].set_ylim(0, 105)
    bars(axes[0, 1], "prediction_age_median_ms_median", "Prediction/source age", "Median (ms)")
    bars(axes[1, 0], "boundary_jump_p95_rad_median", "Action boundary jump", "P95 (rad)")
    bars(axes[1, 1], "jerk_p95_rad_s3_median", "Action jerk", "P95 (rad/s³)")
    fig.suptitle("π0.5 real-robot RTC comparison", fontsize=13)
    fig.text(
        0.01,
        -0.02,
        "Historical fixed-point runs: 10 episodes per condition. Descriptive comparison; checkpoint and run conditions were not a strict paired ablation.",
        fontsize=8.5,
        color="#666666",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180, facecolor="white")
    plt.close(fig)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
