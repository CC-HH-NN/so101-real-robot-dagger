"""普通评测的官方RTC后端；复用原生控制器的串口/保护/收尾，仅替换旧gRPC通信与队列。"""
import base64
import io
import json
import math
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image

from native_feetech import aligned_state
from official_dagger_artifact import read_json, server_config, validate_contract


def validate_official_client(cfg):
    # 在打开硬件前核验模式、训练延迟、关节/统计合同及固定的本地端点。
    deploy, binding = read_json(cfg['official_config']), read_json(cfg['binding'])
    validate_contract(deploy, binding, read_json(Path(cfg['local_source']) / 'config.json'))
    if (cfg['model_path'] != binding['model'] or cfg['expected_files'] != binding['expected_files']
            or cfg['policy_type'] != binding['policy_type'] or cfg['n_obs_steps'] != binding['n_obs_steps']
            or cfg['output_action_dim'] != 6 or cfg['action_units'] != 'ros_radians'
            or cfg['features']['observation.state']['names'] != binding['joint_names']
            or cfg['fps'] != deploy['shared']['action_fps']
            or deploy['shared']['execution_mode'] != 'rtc_trained'
            or not cfg['remote_inference']['endpoint'].startswith('tcp/127.0.0.1:')):
        raise ValueError('官方训练时RTC客户端与部署合同不匹配')
    from official_rtc_preflight import preflight_settings
    preflight_settings(cfg)
    return deploy, binding


class NativeOfficialRPC:
    """官方引擎拥有模型空间前缀与动作队列；本类不打开串口，也不发送机械臂动作。"""

    def __init__(self, cfg, read_state, record):
        # 每次运行新建会话，异常锁存，不重连、不切回旧RTC实现。
        self.cfg, self.read_state, self.record = cfg, read_state, record
        self.deploy, self.binding = validate_official_client(cfg)
        self.fault, self.predictions, self.engine, self.client = None, 0, None, None
        self.stop = threading.Event()
        self.thread = None
        self.camera_stamps = None
        self.last_sequence = None
        self.has_action = False
        self.result_received_at = {}
        self.last_result_request = None
        self.last_rejection = None
        self.waiting_hold_acknowledged = False
        # 相机采集线程短暂落后时，允许控制线程有界等待新鲜帧。
        self.camera_stale_since = None
        self.camera_stale_hold_acknowledged = False
        self.camera_stale_recovered_pending = False
        self.camera_stale_grace_s = float(cfg.get('camera_stale_grace_s', 0.5))
        self.camera_fetch_failed_since = None
        if not math.isfinite(self.camera_stale_grace_s) or self.camera_stale_grace_s <= 0:
            raise ValueError('camera_stale_grace_s必须为正数')

    def receive_result(self, request, result, received_at):
        # 与普通π0.5一致：2.1秒是结果入场年龄，不是整块动作播放的总年龄。
        age = received_at - request.observation.capture_time
        if not 0 <= age <= self.cfg['max_prediction_age_s']:
            self.record('official_result_expired', request_id=request.request_id, source_age_s=age)
            raise RuntimeError('官方RTC预测返回时来源观测已过期')
        self.result_received_at[request.request_id] = received_at
        self.last_result_request = request
        # 仅保留仍可能被播放的块；旧ID缺失时下方派发检查会拒绝动作。
        self.result_received_at = {key: stamp for key, stamp in self.result_received_at.items()
                                   if received_at - stamp <= self.cfg['chunk_plan_lifetime_s']}
        return result

    def record_result(self, values):
        # 只补充官方拒绝原因诊断，不改变官方延迟估计或接纳规则。
        self.predictions += 1
        if values['accepted']:
            self.last_rejection = None
        else:
            request = self.last_result_request
            if request is not None and request.request_id == values['request_id']:
                measured = math.ceil(values['turnaround_s'] * self.cfg['fps'])
                maximum = self.binding['rtc_training_max_delay']
                previous = request.continuation.model_actions
                has_previous = previous is not None and len(previous) > 0
                detail = dict(request_id=request.request_id,
                    turnaround_s=values['turnaround_s'], estimated_required_delay_steps=measured,
                    conditioned_delay_steps=request.delay, training_max_delay_steps=maximum,
                    server_worker_s=values.get('server_durations', {}).get('worker'))
                self.last_rejection = 'RTC结果被官方接纳规则拒绝'
                if has_previous and measured > min(request.delay, maximum):
                    self.last_rejection += (
                        f"：往返{values['turnaround_s']:.3f}秒估计需要{measured}步，"
                        f'本次条件前缀{request.delay}步，训练上限{maximum}步')
                self.record('official_trained_result_rejected', **detail)
        self.record('prediction', age_s=values['source_age_s'],
                    accepted=values['accepted'], mode='rtc_trained')

    def dispatch_error(self, fallback):
        # 保留真正的官方故障；正常耗尽时带出最近一次具体接续拒绝原因。
        if self.engine.failure_traceback:
            return self.engine.failure_traceback
        return fallback if self.last_rejection is None else fallback + '；' + self.last_rejection

    def check_playback_age(self, now):
        # 每条动作按其自身来源检查接收后的寿命，不能靠其他新预测刷新旧块。
        provenance = self.engine.runtime.current
        received_at = None if provenance is None else self.result_received_at.get(provenance.request_id)
        if received_at is None or not 0 <= now - received_at <= self.cfg['chunk_plan_lifetime_s']:
            raise RuntimeError('官方RTC动作块接收后的播放期限已过期')

    def start(self):
        # 绑定严格加载报告的模型身份，按官方能力合同建立独占远程会话。
        from lerobot.inference import FeatureSpec, RemoteInferenceConfig
        from lerobot.remote_inference import RemoteClient
        from lerobot.remote_inference.engine import RemoteInferenceEngine
        from dagger_pi05_capture import artifact_identity
        cfg = RemoteInferenceConfig(**self.cfg['remote_inference'],
            deployment=self.deploy['server']['deployment'],
            expected_artifact=artifact_identity({'artifact_report': self.cfg['artifact_report']}, self.binding),
            semantics=self.deploy['shared']['semantics'], mode='rtc_trained')
        self.record('official_age_budgets', result_admission_s=self.cfg['max_prediction_age_s'],
                    received_chunk_playback_s=self.cfg['chunk_plan_lifetime_s'],
                    fresh_prediction_heartbeat_s=self.cfg['max_action_lease_s'],
                    total_source_age_s=cfg.max_observation_age_s)
        spec = server_config(self.deploy, self.binding, Path(self.deploy['server']['root']) / 'serving_model')
        owner = self

        class FreshResultClient(RemoteClient):
            # 沿用官方收发和合同校验，只在官方接纳动作前执行独立入场检查。
            def infer(self, request, *, cancelled=lambda: False):
                result = super().infer(request, cancelled=cancelled)
                return owner.receive_result(request, result, time.monotonic())

        self.client = FreshResultClient.connect(cfg)
        try:
            self.client.admit(features=tuple(FeatureSpec(**f) for f in spec['features']),
                action_feature=FeatureSpec(**spec['action_feature']), semantics=cfg.semantics,
                action_interval=1 / self.cfg['fps'], mode=cfg.mode)
            owner = self

            class RecordedEngine(RemoteInferenceEngine):
                # 同时归档官方请求、接纳和实测网络耗时，不影响官方调度逻辑。
                def _event(self, name, **values):
                    super()._event(name, **values)
                    owner.record('official_' + name, **values)
                    if name == 'result':
                        owner.record_result(values)

            self.engine = RecordedEngine(self.client, cfg, task=self.cfg['task'])
            self.engine.start()
            self.engine.resume()
            self.thread = threading.Thread(target=self.capture, daemon=True, name='OfficialRTCCamera')
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def capture(self):
        # 使用双摄真实采集时刻匹配整笔关节反馈；丢弃旧快照，不给旧图像续期。
        try:
            while not self.stop.is_set():
                began = time.monotonic()
                try:
                    with urllib.request.urlopen(self.cfg['camera_url'], timeout=self.cfg['camera_http_timeout_s']) as response:
                        body = response.read(self.cfg['max_camera_bytes'] + 1)
                except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
                    now = time.monotonic()
                    if self.camera_fetch_failed_since is None:
                        self.camera_fetch_failed_since = now
                    outage = now - self.camera_fetch_failed_since
                    self.record('official_camera_fetch_retry', error=repr(exc),
                                outage_s=outage, grace_s=self.camera_stale_grace_s)
                    if outage > self.camera_stale_grace_s:
                        raise RuntimeError(f'相机取帧连续失败{outage:.3f}秒：{exc}') from exc
                    self.stop.wait(1 / self.cfg['fps'])
                    continue
                if self.camera_fetch_failed_since is not None:
                    self.record('official_camera_fetch_recovered',
                                outage_s=time.monotonic() - self.camera_fetch_failed_since)
                    self.camera_fetch_failed_since = None
                if len(body) > self.cfg['max_camera_bytes']:
                    raise ValueError('相机响应超出有界大小')
                camera = json.loads(body)
                if not camera['ready'] or camera['faults'] or set(camera['cameras']) != {'top', 'wrist'}:
                    raise RuntimeError('双摄未就绪或故障')
                frames = camera['cameras']
                stamps = [frames[k]['capture_wall_time'] for k in ('top', 'wrist')]
                self.camera_stamps = stamps
                sequence = tuple(frames[k]['seq'] for k in ('top', 'wrist'))
                if self.last_sequence is not None and any(a < b for a, b in zip(sequence, self.last_sequence)):
                    raise RuntimeError('相机序号倒退')
                try:
                    state = aligned_state(self.read_state(), stamps, time.time(), self.cfg['max_age_s'],
                                          self.cfg['max_future_s'], self.cfg['max_skew_s'])
                except RuntimeError:
                    state = None
                if state is not None and (self.last_sequence is None or all(
                        a > b for a, b in zip(sequence, self.last_sequence))):
                    features = {'observation.state': np.asarray(state['positions'], dtype=np.float32)}
                    self.record('official_model_observation', positions=features['observation.state'].tolist(),
                                state_stamp=state['stamp'], capture_stamps=stamps,
                                coordinate_space='training_model')
                    for key in ('top', 'wrist'):
                        with Image.open(io.BytesIO(base64.b64decode(frames[key]['jpeg_b64'], validate=True))) as image:
                            if image.format != 'JPEG' or image.size != (640, 480):
                                raise ValueError('图像格式或大小不符')
                            features['observation.images.' + key] = np.asarray(image.convert('RGB')).copy()
                    sampled = state['monotonic'] + min(*stamps, state['stamp']) - state['stamp']
                    self.engine.submit_observation(features, sampled)
                    self.last_sequence = sequence
                self.stop.wait(max(0, 1 / self.cfg['fps'] - (time.monotonic() - began)))
        except Exception as exc:
            self.fault = repr(exc)
            self.record('official_capture_fault', error=self.fault)

    def target(self, now):
        # 逐周期检查相机时效；短暂取帧空窗先保持当前位置，持续过期才停止。
        if self.fault:
            raise RuntimeError(self.fault)
        if self.camera_stamps is None:
            return None
        ages = [time.time() - stamp for stamp in self.camera_stamps]
        if min(ages) < -self.cfg['max_future_s'] or max(ages) > self.cfg['max_age_s']:
            if self.camera_stale_since is None:
                self.camera_stale_since = now
                self.camera_stale_hold_acknowledged = False
                self.record('official_camera_stale_started', ages_s=ages,
                            max_age_s=self.cfg['max_age_s'],
                            grace_s=self.camera_stale_grace_s)
            stale_for = now - self.camera_stale_since
            if stale_for <= self.camera_stale_grace_s:
                return None
            raise RuntimeError(f'控制时相机已经过期，持续{stale_for:.3f}秒')
        if self.camera_stale_since is not None:
            self.record('official_camera_stale_recovered',
                        duration_s=now - self.camera_stale_since)
            self.camera_stale_since = None
            self.camera_stale_recovered_pending = self.camera_stale_hold_acknowledged
            self.camera_stale_hold_acknowledged = False
        if self.camera_stale_recovered_pending:
            return None
        if not self.engine.dispatch_allowed():
            if self.engine.failed:
                raise RuntimeError(self.dispatch_error('官方RTC已撤销动作派发权限，停止本轮'))
            # 有界等待交给控制线程实际保持并确认，再由官方引擎请求新观测。
            # 不能把“暂时没有动作”误报为终止故障。
            return None
        self.waiting_hold_acknowledged = False
        waiting = getattr(self.engine.runtime, 'waiting_for_fresh_actions', lambda: False)()
        accepted_at = self.engine.runtime.last_accepted_at
        if (not waiting and accepted_at is not None
                and now - accepted_at > self.cfg['max_action_lease_s']):
            raise RuntimeError('模型新鲜预测心跳已过期')
        action = self.engine.get_action(None)
        if not self.engine.dispatch_allowed():
            if self.engine.failed:
                raise RuntimeError(self.dispatch_error('官方RTC已撤销动作派发权限，停止本轮'))
            return None
        if action is None:
            return None
        self.check_playback_age(time.monotonic())
        self.has_action = True
        return dict(action=action.float().cpu().numpy(), execution_mode='rtc_trained')

    def maintain_waiting_hold(self, robot, execute):
        # 相机短暂过期或官方接续等待时，由串口线程保持当前姿态。
        if self.camera_stale_recovered_pending:
            self.camera_stale_recovered_pending = False
            return True
        if self.camera_stale_since is not None:
            if (time.monotonic() - self.camera_stale_since > self.camera_stale_grace_s
                    or self.fault):
                return False
            if not self.has_action:
                return False
            if not self.camera_stale_hold_acknowledged:
                if not execute:
                    raise RuntimeError('相机恢复要求实际保持确认，影子模式不能确认')
                held = robot.hold_current()
                self.camera_stale_hold_acknowledged = True
                self.record('official_camera_stale_hold_acknowledged', **held)
            return True
        if self.engine.failed:
            raise RuntimeError(self.dispatch_error('官方RTC故障，不能恢复'))
        runtime = self.engine.runtime
        if runtime.starvation_deadline is None:
            return False
        runtime.check_deadlines()
        if runtime.failure:
            raise RuntimeError(self.dispatch_error(runtime.failure))
        if runtime.held and not self.waiting_hold_acknowledged:
            if not execute:
                raise RuntimeError('RTC恢复要求实际保持确认，影子模式不能确认')
            held = robot.hold_current()
            self.engine.acknowledge_hold()
            self.waiting_hold_acknowledged = True
            self.record('official_wait_hold_acknowledged', **held)
        return True

    def close(self):
        # 先阻止新观测，再有界关闭官方会话；实际保持和串口关闭仍由原执行器处理。
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        if self.engine is not None:
            self.engine.stop()
        elif self.client is not None:
            self.client.close()
