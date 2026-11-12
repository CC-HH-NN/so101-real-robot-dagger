"""依据审查清单及真实日志导出人工纠正数据；复用官方LeRobot写入器，保留原始轨迹。"""
import argparse
import bisect
import copy
import hashlib
import json
import math
from pathlib import Path
import random
import sys

import numpy as np
import pyarrow.parquet as pq


def read_json(path):
    # 统一按UTF-8读取清单。
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    # 输出仅允许创建新文件；重复执行不能覆盖已有清洗结果。
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def sha256(path):
    # 流式指纹核对原件与配方，不把视频整体加载进内存。
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_config(path):
    # 所有路径均相对于配置声明的项目目录解析。
    path = Path(path).resolve()
    cfg = read_json(path)
    root = (path.parent / cfg['project_root']).resolve()
    for key in ('source_dataset', 'audit_directory', 'output_directory', 'lerobot_source',
                'normalization_stats', 'original_split'):
        cfg[key] = (root / cfg[key]).resolve()
    if cfg['resampling'] != 'causal_previous_recorded_pair':
        raise ValueError('本入口只实现因果前值采样，不隐式插值动作或图像')
    return cfg, path


def causal_grid(times, fps):
    # 每个均匀时刻只使用已经记录的观测/动作对，不提前使用未来命令。
    times = np.asarray(times, dtype=np.float64)
    if len(times) == 0 or fps <= 0 or np.any(np.diff(times) <= 0):
        raise ValueError('采样时间必须非空、严格递增，频率必须为正')
    grid = times[0] + np.arange(math.floor((times[-1] - times[0]) * fps) + 1) / fps
    indices = np.searchsorted(times, grid, side='right') - 1
    if np.any(times[indices] > grid) or np.any(grid > times[-1]):
        raise ValueError('采样越过源数据或使用了未来命令')
    return grid, indices


def split_continuous(rows, transitions, gap_limit):
    # 按真实退出人工控制事件或源帧断点分段；未解释的大间隔必须停止而非猜测。
    parts, boundaries, current = [], [], []
    stamps = [event['monotonic'] for event in transitions]
    for row in rows:
        if current:
            before = current[-1]
            left = bisect.bisect_right(stamps, before['command_monotonic'])
            right = bisect.bisect_right(stamps, row['command_monotonic'])
            events = transitions[left:right]
            gap = row['command_monotonic'] - before['command_monotonic']
            discontinuous = row['frame_index'] != before['frame_index'] + 1
            if events or discontinuous:
                boundaries.append({'original_episode_index': row['episode_index'],
                    'before_frame_index': before['frame_index'], 'after_frame_index': row['frame_index'],
                    'gap_s': gap, 'events': events, 'source_frame_discontinuity': discontinuous})
                parts.append(current)
                current = []
            elif gap > gap_limit:
                raise ValueError(f"未解释的人工时间空档：episode={row['episode_index']} gap={gap}")
        current.append(row)
    if current:
        parts.append(current)
    return parts, boundaries


def make_plan(cfg, config_path):
    # 先冻结按任务分组的划分和逐帧来源，后续导出严格使用同一计划。
    audit = cfg['audit_directory']
    decisions = read_json(audit / 'cleaning-decisions.json')
    episodes = read_json(audit / 'episodes.json')
    time_rows = [json.loads(line) for line in (audit / 'human-frame-time-map.jsonl').open(encoding='utf-8')]
    selected = set(decisions['human_supervision_episode_indices'])
    by_episode = {ep: [row for row in time_rows if row['episode_index'] == ep] for ep in sorted(selected)}
    if len(time_rows) != decisions['human_supervision_frames'] or len(selected) != 35:
        raise ValueError('人工清单与审查数量不一致')
    validation = set()
    rng = random.Random(cfg['split_seed'])
    transitions_by_run = {}
    inputs = [config_path, audit / 'cleaning-decisions.json', audit / 'episodes.json',
              audit / 'human-frame-time-map.jsonl', cfg['normalization_stats'], cfg['original_split']]
    for run, count in cfg['validation_tasks_per_run'].items():
        candidates = [ep for ep in selected if episodes[ep]['source']['run'] == run]
        validation.update(rng.sample(sorted(candidates), count))
        leader = audit.parent.parent / run / 'leader.jsonl'
        inputs.append(leader)
        transitions_by_run[run] = [event for line in leader.open(encoding='utf-8')
            if (event := json.loads(line))['event'] == 'transition'
            and event['old_phase'] == 'correcting' and event['new_phase'] != 'correcting']
    segments, boundaries = [], []
    for ep, rows in by_episode.items():
        run = episodes[ep]['source']['run']
        parts, edges = split_continuous(rows, transitions_by_run[run], cfg['unexplained_gap_limit_s'])
        boundaries.extend(edges)
        for part in parts:
            grid, positions = causal_grid([r['command_monotonic'] for r in part], cfg['target_fps'])
            chosen = [part[int(i)] for i in positions]
            segments.append({'episode_index': len(segments), 'original_episode_index': ep,
                'run': run, 'split': 'validation' if ep in validation else 'train',
                'source_frames': len(part), 'output_frames': len(chosen),
                'valid_windows': max(0, len(chosen) - cfg['action_horizon'] + 1),
                'source_global_indices': [row['index'] for row in chosen],
                'sample_monotonic': grid.tolist(), 'source_time_rows': chosen})
    export_index = 0
    for segment in segments:
        segment['export_episode_index'] = export_index if segment['valid_windows'] else None
        if segment['valid_windows']:
            export_index += 1
    inputs.extend(sorted(p for p in cfg['source_dataset'].rglob('*') if p.is_file()))
    hashes = {str(p): sha256(p) for p in inputs}
    return {'config_sha256': sha256(config_path), 'inputs_sha256': hashes,
        'excluded_original_episodes': decisions['excluded_episode_indices'],
        'autonomous_archive_only': decisions['autonomous_success_indices'],
        'target_fps': cfg['target_fps'], 'action_horizon': cfg['action_horizon'],
        'train_original_episodes': sorted(selected - validation),
        'validation_original_episodes': sorted(validation),
        'source_human_frames': len(time_rows), 'boundaries': boundaries, 'segments': segments,
        'source_dataset': str(cfg['source_dataset'])}


class VideoCursor:
    """依次解码原始视频，仅保留当前画面，避免一次缓存全部双摄图像。"""

    def __init__(self):
        # 解码器延迟到第一次取图时创建。
        self.container = self.path = self.frames = self.image = None
        self.position = -1

    def get(self, path, index):
        # 源帧必须单调读取，重复选中同一帧时复用该画面。
        import av
        av.logging.set_level(av.logging.ERROR)
        if self.path != path:
            self.close()
            self.container = av.open(str(path))
            self.frames = self.container.decode(video=0)
            self.path, self.position = path, -1
        if index < self.position:
            raise ValueError('源视频帧顺序倒退')
        while self.position < index:
            frame = next(self.frames)
            self.position += 1
            if self.position == index:
                self.image = frame.to_ndarray(format='rgb24')
        return self.image

    def close(self):
        # 结束当前视频文件句柄。
        if self.container is not None:
            self.container.close()
        self.container = None


def output_features(source):
    # 保留训练需要的原生字段，并把真实时间和源帧写成可追溯列。
    keys = ('action', 'observation.state', 'observation.images.top', 'observation.images.wrist', 'intervention')
    features = {k: {n: copy.deepcopy(source[k][n]) for n in ('dtype', 'shape', 'names')}
                for k in keys}
    for key in ('original_episode_index', 'original_frame_index', 'original_index'):
        features[key] = {'dtype': 'int64', 'shape': (1,), 'names': None}
    for key in ('sample_monotonic', 'source_command_monotonic', 'source_top_capture_wall_time',
                'source_wrist_capture_wall_time', 'source_state_capture_wall_time'):
        features[key] = {'dtype': 'float64', 'shape': (1,), 'names': None}
    return features


def export_dataset(cfg, plan):
    # 使用官方写入器按配置频率导出独立数据，绝不修改录制目录。
    sys.path.insert(0, str(cfg['lerobot_source']))
    from lerobot.configs.video import RGBEncoderConfig
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    root, out = cfg['source_dataset'], cfg['output_directory']
    if (out / 'dataset').exists():
        raise FileExistsError('目标数据集已存在，不覆盖或自动重试')
    raw = []
    for path in sorted((root / 'data').rglob('*.parquet')):
        raw.extend(pq.read_table(path).to_pylist())
    rows = {row['index']: row for row in raw}
    source_info = read_json(root / 'meta/info.json')
    episodes = read_json(cfg['audit_directory'] / 'episodes.json')
    tasks = {row['task_index']: row['task'] for row in pq.read_table(root / 'meta/tasks.parquet').to_pylist()}
    ds = LeRobotDataset.create(repo_id=cfg['repo_id'], root=out / 'dataset', fps=cfg['target_fps'],
        features=output_features(source_info['features']), robot_type=source_info['robot_type'],
        video_backend='pyav', streaming_encoding=True, rgb_encoder=RGBEncoderConfig(**cfg['encoder']),
        encoder_threads=cfg['encoder_threads'])
    cursors = {key: VideoCursor() for key in ('top', 'wrist')}
    try:
        for segment in plan['segments']:
            if not segment['valid_windows']:
                continue
            original = episodes[segment['original_episode_index']]
            for grid, index, timing in zip(segment['sample_monotonic'], segment['source_global_indices'],
                                            segment['source_time_rows']):
                row = rows[index]
                if not bool(row['intervention']):
                    raise ValueError('人工导出混入自主帧')
                frame = {'action': np.array(row['action'], dtype=np.float32),
                    'observation.state': np.array(row['observation.state'], dtype=np.float32),
                    'intervention': np.array([True]), 'task': tasks[row['task_index']]}
                for camera, cursor in cursors.items():
                    ref = original['video'][camera]
                    video_index = round(ref['start_s'] * source_info['fps']) + row['frame_index']
                    frame['observation.images.' + camera] = cursor.get(root / ref['file'], video_index)
                for name, value in [('original_episode_index', row['episode_index']),
                                    ('original_frame_index', row['frame_index']), ('original_index', index)]:
                    frame[name] = np.array([value], dtype=np.int64)
                values = {'sample_monotonic': grid, 'source_command_monotonic': timing['command_monotonic'],
                    'source_top_capture_wall_time': timing['capture_stamps']['top'],
                    'source_wrist_capture_wall_time': timing['capture_stamps']['wrist'],
                    'source_state_capture_wall_time': timing['capture_stamps']['state']}
                frame.update({key: np.array([value], dtype=np.float64) for key, value in values.items()})
                ds.add_frame(frame)
            ds.save_episode(parallel_encoding=False)
            print(json.dumps({'segment': segment['episode_index'], 'original_task': original['episode_index'],
                'frames': segment['output_frames'], 'valid_windows': segment['valid_windows']},
                ensure_ascii=False), flush=True)
    finally:
        ds.finalize()
        for cursor in cursors.values():
            cursor.close()
    write_json(out / 'dataset/meta/model_coordinates.json', read_json(root / 'meta/model_coordinates.json'))
    write_json(out / 'normalization-train75.json', read_json(cfg['normalization_stats']))
    write_json(out / 'original-train75-val8-split.json', read_json(cfg['original_split']))
    write_json(out / 'short-segments-archive.json', {'source_dataset': str(root),
        'note': '仅归档来源映射，原始双摄和数据完整保留；不进入50步动作监督',
        'segments': [s for s in plan['segments'] if not s['valid_windows']]})
    split = {key + '_episode_indices': [s['export_episode_index'] for s in plan['segments']
        if s['split'] == key and s['valid_windows'] > 0] for key in ('train', 'validation')}
    split['short_source_segment_indices_archive_only'] = [s['episode_index'] for s in plan['segments'] if not s['valid_windows']]
    split.update({'train_original_episodes': plan['train_original_episodes'],
        'validation_original_episodes': plan['validation_original_episodes'],
        'sampler_drop_n_last_frames': cfg['action_horizon'] - 1})
    write_json(out / 'training-selection.json', split)
    write_json(out / 'training-contract.json', {'status': 'human_pool_only_not_mixed_training_job',
        'repo_id': cfg['repo_id'], 'fps': cfg['target_fps'], 'chunk_size': cfg['action_horizon'],
        'rtc_training_max_delay': cfg['rtc_training_max_delay'], 'drop_n_last_frames': cfg['action_horizon'] - 1,
        'selection_file': 'training-selection.json',
        'train_tasks': len(plan['train_original_episodes']),
        'validation_tasks': len(plan['validation_original_episodes']),
        'normalization': '使用normalization-train75.json及现有checkpoint处理器；不得默认使用全体新数据统计',
        'original_demo_pool': '原75训练/8验证示范仍在服务器，混合前须准备相同真实时间尺度的数据',
        'loss_supervision': '仅人工连续段内完整未来动作窗；不得关闭末尾49帧的起点排除',
        'split_seed': cfg['split_seed']})


def main():
    # 默认只生成确定性计划；显式export才创建独立数据集。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--export', action='store_true')
    args = parser.parse_args()
    cfg, config_path = load_config(args.config)
    out = cfg['output_directory']
    plan_path = out / 'plan.json'
    if plan_path.exists():
        plan = read_json(plan_path)
        if any(sha256(path) != expected for path, expected in plan['inputs_sha256'].items()):
            raise ValueError('已冻结的源文件或配置发生变化，不能复用旧计划')
    else:
        plan = make_plan(cfg, config_path)
        write_json(plan_path, plan)
    summary = {'tasks': len(plan['train_original_episodes']) + len(plan['validation_original_episodes']),
        'segments': len(plan['segments']), 'frames': sum(s['output_frames'] for s in plan['segments']),
        'windows': sum(s['valid_windows'] for s in plan['segments']), 'boundaries': len(plan['boundaries'])}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if args.export:
        export_dataset(cfg, plan)


if __name__ == '__main__':
    main()
