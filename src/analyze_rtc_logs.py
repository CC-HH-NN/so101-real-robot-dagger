"""从 SO-101 真机日志提取 RTC 延迟、队列和动作连续性指标。

输入是本地评测目录；输出只包含汇总数值，不复制原始视频、模型或硬件配置。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable


JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取事件日志并跳过损坏行。"""
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
    return events


def percentile(values: Iterable[float], q: float) -> float | None:
    """计算线性插值百分位数。"""
    values = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def summarize(values: Iterable[float], scale: float = 1.0) -> tuple[int, float | None, float | None, float | None]:
    """返回样本数、median、P95 和最大值。"""
    values = [float(v) * scale for v in values if math.isfinite(float(v))]
    return len(values), percentile(values, 0.5), percentile(values, 0.95), max(values) if values else None


def event_values(events: list[dict[str, Any]], event_name: str, key: str) -> list[float]:
    """提取事件中的数值字段。"""
    return [float(item[key]) for item in events if item.get("event") == event_name and isinstance(item.get(key), (int, float))]


def command_derivatives(commands: list[dict[str, Any]]) -> dict[str, Any]:
    """根据实际下发目标计算速度、加速度和 jerk 的整体统计。"""
    samples = [
        item for item in commands
        if isinstance(item.get("logged_monotonic"), (int, float))
        and isinstance(item.get("applied"), list)
        and len(item["applied"]) == len(JOINTS)
    ]
    samples.sort(key=lambda item: float(item["logged_monotonic"]))
    velocities: list[float] = []
    accelerations: list[float] = []
    jerks: list[float] = []
    gaps: list[float] = []
    previous_velocity: list[float] | None = None
    previous_acceleration: list[float] | None = None
    previous_positions: list[float] | None = None
    previous_time: float | None = None
    for item in samples:
        now = float(item["logged_monotonic"])
        position = [float(v) for v in item["applied"]]
        if previous_time is None or previous_positions is None:
            previous_time, previous_positions = now, position
            continue
        dt = now - previous_time
        if dt <= 0 or dt > 0.5:
            if dt > 0:
                gaps.append(dt)
            previous_velocity = None
            previous_acceleration = None
            previous_time, previous_positions = now, position
            continue
        velocity = [(a - b) / dt for a, b in zip(position, previous_positions)]
        velocities.extend(abs(v) for v in velocity)
        if previous_velocity is not None:
            acceleration = [(a - b) / dt for a, b in zip(velocity, previous_velocity)]
            accelerations.extend(abs(v) for v in acceleration)
            if previous_acceleration is not None:
                jerk = [(a - b) / dt for a, b in zip(acceleration, previous_acceleration)]
                jerks.extend(abs(v) for v in jerk)
            previous_acceleration = acceleration
        previous_velocity = velocity
        previous_time, previous_positions = now, position
    return {
        "velocity": summarize(velocities),
        "acceleration": summarize(accelerations),
        "jerk": summarize(jerks),
        "gaps": summarize(gaps),
    }


def boundary_jumps(events: list[dict[str, Any]], commands: list[dict[str, Any]]) -> list[float]:
    """测量被接受预测前后相邻实际下发目标的跳变。"""
    accepted_times = []
    for item in events:
        name = item.get("event")
        accepted = name == "official_result" and item.get("accepted") is True
        accepted = accepted or (name == "prediction" and item.get("accepted") is True)
        if accepted and isinstance(item.get("logged_monotonic"), (int, float)):
            accepted_times.append(float(item["logged_monotonic"]))
    commands = [
        item for item in commands
        if isinstance(item.get("logged_monotonic"), (int, float))
        and isinstance(item.get("applied"), list)
        and len(item["applied"]) == len(JOINTS)
    ]
    commands.sort(key=lambda item: float(item["logged_monotonic"]))
    jumps: list[float] = []
    for stamp in accepted_times:
        before = [x for x in commands if float(x["logged_monotonic"]) <= stamp]
        after = [x for x in commands if float(x["logged_monotonic"]) > stamp]
        if not before or not after:
            continue
        left = before[-1]["applied"]
        right = after[0]["applied"]
        jumps.append(math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(right, left))))
    return jumps


def episode_rows(root: Path, label: str) -> list[dict[str, Any]]:
    """读取一个评测批次下的所有 episode。"""
    result_file = root / "results.csv"
    if not result_file.exists():
        raise FileNotFoundError(result_file)
    output: list[dict[str, Any]] = []
    with result_file.open(encoding="utf-8", newline="") as handle:
        records = list(csv.DictReader(handle))
    for record in records:
        run = Path(record["run"])
        events = read_jsonl(run / "motion/shadow/events.jsonl")
        commands = [item for item in events if item.get("event") == "command_sent"]
        official_predictions = [item for item in events if item.get("event") == "official_result"]
        local_predictions = [item for item in events if item.get("event") == "prediction"]
        # Training-Time RTC 会同时记录 official_result 和 prediction；同一个结果只保留 official_result。
        predictions = official_predictions if official_predictions else local_predictions
        if not events:
            continue
        timing = command_derivatives(commands)
        prediction_age = [
            float(item["age_s"])
            for item in events
            if item.get("event") == "prediction" and isinstance(item.get("age_s"), (int, float))
        ]
        prediction_rpc = [
            float(item["get_actions_rpc_s"])
            for item in events
            if item.get("event") == "prediction" and isinstance(item.get("get_actions_rpc_s"), (int, float))
        ]
        turnaround = [
            float(item["turnaround_s"])
            for item in events
            if item.get("event") == "official_result" and isinstance(item.get("turnaround_s"), (int, float))
        ]
        source_age = [
            float(item["source_age_s"])
            for item in events
            if item.get("event") == "official_result" and isinstance(item.get("source_age_s"), (int, float))
        ]
        worker = [
            float(item["server_durations"]["worker"])
            for item in events
            if item.get("event") == "official_result"
            and isinstance(item.get("server_durations"), dict)
            and isinstance(item["server_durations"].get("worker"), (int, float))
        ]
        queue = [
            float(item["queue_playback_s"])
            for item in events
            if item.get("event") == "official_result" and isinstance(item.get("queue_playback_s"), (int, float))
        ]
        accepted = [item.get("accepted") is True for item in predictions]
        start = next((item for item in events if item.get("event") == "motion_window_started"), {})
        record_error = record.get("error", "")
        if "KeyboardInterrupt" in record_error:
            record_error = ""
        output.append({
            "condition": label,
            "batch": root.name,
            "attempt": record.get("attempt", ""),
            "trial": record.get("trial", ""),
            "phase": record.get("phase", ""),
            "score": record.get("score", ""),
            "episode_id": f"{root.name}/attempt-{record.get('attempt', '')}",
            "prediction_n": len(predictions),
            "accepted_n": sum(accepted),
            "rejected_n": len(accepted) - sum(accepted),
            "prediction_age_median_ms": percentile(prediction_age, 0.5) * 1000 if prediction_age else None,
            "prediction_age_p95_ms": percentile(prediction_age, 0.95) * 1000 if prediction_age else None,
            "prediction_rpc_median_ms": percentile(prediction_rpc, 0.5) * 1000 if prediction_rpc else None,
            "prediction_rpc_p95_ms": percentile(prediction_rpc, 0.95) * 1000 if prediction_rpc else None,
            "turnaround_median_ms": percentile(turnaround, 0.5) * 1000 if turnaround else None,
            "turnaround_p95_ms": percentile(turnaround, 0.95) * 1000 if turnaround else None,
            "source_age_median_ms": percentile(source_age, 0.5) * 1000 if source_age else None,
            "source_age_p95_ms": percentile(source_age, 0.95) * 1000 if source_age else None,
            "worker_median_ms": percentile(worker, 0.5) * 1000 if worker else None,
            "worker_p95_ms": percentile(worker, 0.95) * 1000 if worker else None,
            "queue_min_s": min(queue) if queue else None,
            "queue_empty_n": sum(value <= 0 for value in queue),
            "startup_s": start.get("startup_seconds"),
            "boundary_jump_median_rad": percentile(boundary_jumps(events, commands), 0.5),
            "boundary_jump_p95_rad": percentile(boundary_jumps(events, commands), 0.95),
            "velocity_p95_rad_s": timing["velocity"][2],
            "acceleration_p95_rad_s2": timing["acceleration"][2],
            "jerk_p95_rad_s3": timing["jerk"][2],
            "command_gap_p95_s": timing["gaps"][2],
            "technical_error": record_error,
        })
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """写出 UTF-8 汇总表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def aggregate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按运行条件汇总逐回合指标。"""
    output: list[dict[str, Any]] = []
    numeric = (
        "prediction_age_median_ms", "prediction_age_p95_ms", "prediction_rpc_median_ms",
        "prediction_rpc_p95_ms", "turnaround_median_ms", "turnaround_p95_ms",
        "source_age_median_ms", "source_age_p95_ms", "worker_median_ms", "worker_p95_ms",
        "queue_min_s", "boundary_jump_p95_rad", "velocity_p95_rad_s",
        "acceleration_p95_rad_s2", "jerk_p95_rad_s3", "startup_s",
    )
    for condition in sorted({row["condition"] for row in rows}):
        group = [row for row in rows if row["condition"] == condition]
        item: dict[str, Any] = {
            "condition": condition,
            "episodes": len(group),
            "scored_episodes": sum(row["score"] in {"0", "1"} for row in group),
            "successes": sum(row["score"] == "1" for row in group),
            "success_rate": sum(row["score"] == "1" for row in group) / len(group) if group else None,
            "prediction_n": sum(int(row["prediction_n"]) for row in group),
            "accepted_n": sum(int(row["accepted_n"]) for row in group),
            "rejected_n": sum(int(row["rejected_n"]) for row in group),
        }
        item["accepted_rate"] = item["accepted_n"] / item["prediction_n"] if item["prediction_n"] else None
        for key in numeric:
            values = [float(row[key]) for row in group if row[key] not in (None, "")]
            item[f"{key}_median"] = statistics.median(values) if values else None
        output.append(item)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--standard-root", type=Path, required=True, help="普通 π0.5 批次目录")
    parser.add_argument("--trained-root", type=Path, required=True, help="Training-Time RTC 批次目录")
    parser.add_argument("--output-dir", type=Path, required=True, help="公开结果输出目录")
    args = parser.parse_args()
    rows = episode_rows(args.standard_root, "π0.5 Inference-Time RTC")
    rows += episode_rows(args.trained_root, "π0.5 Training-Time RTC")
    write_csv(args.output_dir / "rtc_episode_metrics.csv", rows)
    write_csv(args.output_dir / "rtc_comparison_summary.csv", aggregate_rows(rows))
    print(f"wrote {len(rows)} episode rows to {args.output_dir / 'rtc_episode_metrics.csv'}")


if __name__ == "__main__":
    main()
