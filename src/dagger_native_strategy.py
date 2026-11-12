"""复用官方DAgger录制与状态机；为原生弧度设备补充实测保持和对齐验收。"""
from contextlib import contextmanager
import math
import time

from lerobot.common.control_utils import teleop_smooth_move_to
from lerobot.rollout.strategies.dagger import DAggerPhase, DAggerStrategy
from lerobot.utils.feature_utils import build_dataset_frame
from dagger_session_controls import CaptureSessionControl


class NativeDAggerStrategy(CaptureSessionControl, DAggerStrategy):
    """官方纠正录制不改；主臂按实测从臂位置对齐，不追逐未执行的预测。"""

    def _background_push(self, dataset, cfg):
        # 官方Enter上传分支不检查push_to_hub；本入口禁止意外上传本地采集数据。
        if cfg.dataset is not None and cfg.dataset.push_to_hub:
            super()._background_push(dataset, cfg)

    @contextmanager
    def _pause_for_recording(self, ctx, **kwargs):
        # 保存完成才提示成功，不把尚未落盘的片段算入进度。
        before = ctx.data.dataset.num_episodes
        with super()._pause_for_recording(ctx, **kwargs):
            yield
        if ctx.data.dataset.num_episodes > before:
            if self._human_completed_correction:
                print(f'人工已完成剩余任务；完整纠正 episode 已保存，总计'
                      f'{ctx.data.dataset.num_episodes}段。当前暂停保持；p开始下一轮，q停止。', flush=True)
            else:
                print(f'已保存当前人工纠正片段，总计{ctx.data.dataset.num_episodes}段。'
                      '当前暂停保持；p继续模型，b再次纠正，q停止。', flush=True)

    def hold_control_state(self, hw):
        # 取一次暂停锚点并持续保持；不能随每次反馈下沉而改写保持目标。
        wrapper = hw.robot_wrapper
        if wrapper.hardware_failure is None and wrapper.inner.native.fault is None:
            held = wrapper.inner.begin_fixed_hold()
            wrapper.send_action(held)
        else:
            raise RuntimeError('从臂已有硬件故障，不能确认暂停保持')
        self._require_interpolator().reset()
        self._cached_obs_processed = None
        self._require_engine().acknowledge_hold()

    def _wait_alignment_confirmation(self, teleop, ctx, deadline):
        # 复用官方按键队列；b只确认当前步骤，不能提前进入纠正或恢复模型。
        while time.monotonic() < deadline:
            if teleop.stop_requested():
                raise RuntimeError('用户停止，取消主臂粗对齐')
            transition = self._events.consume_transition()
            if transition is not None:
                self._events.phase = DAggerPhase.PAUSED
                if transition[1] == DAggerPhase.CORRECTING:
                    return
                print('粗对齐尚未完成；按b确认当前步骤，q停止。', flush=True)
            self.hold_control_state(ctx.hardware)
            time.sleep(0.05)
        raise RuntimeError('人工粗对齐等待超时，停止本轮')

    def _coarse_alignment(self, teleop, ctx, target):
        # 先确认支撑再卸力；只有一次明确确认且差距在小范围内才允许精对齐。
        settings = teleop.settings['leader_handover']
        deadline = time.monotonic() + settings['coarse_alignment_timeout_s']
        teleop.aligned = False
        teleop.keep_torque_on_disconnect = (
            teleop.native.fault is None and teleop.torque.enabled is True)
        print('暂停：从臂保持。先扶住主臂，按b或Tab确认卸力；q停止。', flush=True)
        self._wait_alignment_confirmation(teleop, ctx, deadline)
        teleop.disable_torque()
        teleop.keep_torque_on_disconnect = False
        print('主臂已卸力。手动摆到接近从臂（含夹爪），摆好后按b或Tab检查；'
              '通过后主臂将小幅运动，请先让开运动路径。', flush=True)
        while True:
            self._wait_alignment_confirmation(teleop, ctx, deadline)
            physical_target = teleop.inverse_target(target)
            state = teleop.native.read()
            errors = {name: desired - actual for name, desired, actual in
                      zip(teleop.names, physical_target, state['positions'])}
            outside = {name: error for name, error in errors.items()
                       if not math.isfinite(error) or abs(error) > settings['fine_alignment_max_delta_rad'][name]}
            teleop.journal('coarse_alignment_checked', errors=errors, accepted=not outside)
            if not outside:
                return physical_target, state
            details = '；'.join(f'{name}还需调整{math.degrees(error):+.1f}°'
                               for name, error in outside.items())
            print('差距仍大，保持卸力，请继续手动摆位：' + details +
                  '。摆好后再按b检查；q停止。', flush=True)

    def _apply_transition(self, old_phase, new_phase, engine, interpolator, teleop,
                          ctx, prev_action, timer=None):
        # 暂停后撤销旧推理，复用官方主臂插值；时长由真实关节距离和主臂限速决定。
        teleop.journal('transition', old_phase=old_phase.value, new_phase=new_phase.value)
        if old_phase == DAggerPhase.AUTONOMOUS and new_phase == DAggerPhase.PAUSED:
            engine.pause()
            self.hold_control_state(ctx.hardware)
            target = ctx.hardware.robot_wrapper.inner.begin_fixed_hold()
            settings = teleop.settings['leader_handover']
            physical_target, state = self._coarse_alignment(teleop, ctx, target)
            speed_overrides = settings.get('fine_alignment_speed_overrides_rad_s', {})
            distance_time = max(
                abs(a - b) / min(limit['max_command_velocity'],
                                  speed_overrides.get(name, settings['fine_alignment_speed_rad_s']))
                for name, a, b, limit in zip(teleop.names, physical_target, state['positions'],
                                             teleop.profile['joints'].values()))
            duration = max(settings['minimum_duration_s'], 1.5 * distance_time)
            if duration + settings['settle_timeout_s'] > settings['max_total_s']:
                raise RuntimeError('主臂自动对齐行程超过预算，停止本轮')
            teleop.target, teleop.aligned = target, False
            print('粗对齐通过；主臂正在低速小范围精对齐，请勿触碰主臂。', flush=True)
            previous_speed = teleop.native.cfg['servo_speed_rad_s']
            previous_overrides = teleop.native.cfg.get('servo_speed_overrides_rad_s')
            teleop.native.cfg['servo_speed_rad_s'] = min(
                previous_speed, settings['fine_alignment_speed_rad_s'])
            teleop.native.cfg['servo_speed_overrides_rad_s'] = {
                name: min(previous_speed, speed) for name, speed in speed_overrides.items()
            }
            try:
                teleop_smooth_move_to(teleop, target, duration_s=duration, fps=settings['fps'])
                deadline, stable = time.monotonic() + settings['settle_timeout_s'], None
                while time.monotonic() < deadline:
                    teleop.send_feedback(target)
                    # 官方插值阶段直接写位置；这里单独读取新鲜反馈完成对齐验收。
                    actual = teleop.native.read()['positions']
                    now = time.monotonic()
                    if max(abs(a - b) for a, b in zip(actual, physical_target)) <= settings['tolerance_rad']:
                        stable = now if stable is None else stable
                        if now - stable >= settings['settled_s']:
                            teleop.aligned = True
                            break
                    else:
                        stable = None
                    time.sleep(1 / settings['fps'])
            finally:
                teleop.native.cfg['servo_speed_rad_s'] = previous_speed
                if previous_overrides is None:
                    teleop.native.cfg.pop('servo_speed_overrides_rad_s', None)
                else:
                    teleop.native.cfg['servo_speed_overrides_rad_s'] = previous_overrides
            if not teleop.aligned:
                # 仅在通信和扭矩状态健康时保留上力；通信故障由disconnect按安全分支处理。
                teleop.keep_torque_on_disconnect = (
                    teleop.native.fault is None and teleop.torque.enabled is True)
                teleop.journal('alignment_failed', target=physical_target,
                               actual=actual, tolerance_rad=settings['tolerance_rad'],
                               keep_torque=teleop.keep_torque_on_disconnect)
                raise RuntimeError('主臂未实测对齐，禁止接管，不自动重试')
            # 对齐过程中可能读取过主臂动作；这些目标属于旧姿态，不能带入人工纠正。
            self._events.consume_transition()
            self._events.phase = DAggerPhase.PAUSED
            teleop.reset_correction_slew()
            teleop.journal('alignment_verified', target=physical_target, actual=actual,
                           planned_duration_s=duration)
            print('主臂已对齐。扶住主臂后按Tab，卸力后进行纠正；再按Tab保存。', flush=True)
            if timer is not None:
                timer.restart()
            return
        if old_phase == DAggerPhase.PAUSED and new_phase == DAggerPhase.CORRECTING:
            if not teleop.aligned or teleop.target is None:
                # 上一次接管前发生漂移时，b只重新进入对齐流程，不恢复模型也不直接接管。
                print('尚未通过接管对齐；请保持主臂支撑，重新手动摆近后再按b进行精对齐。', flush=True)
                self._apply_transition(DAggerPhase.AUTONOMOUS, DAggerPhase.PAUSED,
                                       engine, interpolator, teleop, ctx, prev_action, timer)
                return
            # Tab 接管采用原始LeRobot遥操语义，不再套用模型首跳/相对目标检查。
            # 底层关节范围、命令斜率、串口与舵机错误保护仍由设备层保留。
            teleop.manual_control = True
            ctx.hardware.robot_wrapper.inner.manual_control = True
            ctx.hardware.robot_wrapper.inner.native.cfg['manual_control'] = True
            teleop.reset_correction_slew()
            actual = teleop.get_action()
            held = ctx.hardware.robot_wrapper.inner.begin_fixed_hold()
            tolerance = teleop.settings['leader_handover']['tolerance_rad']
            drift = {key: actual[key] - held[key] for key in held}
            max_drift = max(abs(value) for value in drift.values())
            if max_drift > tolerance:
                teleop.journal('handover_rejected', reason='post_alignment_drift',
                               leader=actual, follower=held, drift=drift,
                               max_drift_rad=max_drift, tolerance_rad=tolerance)
                teleop.keep_torque_on_disconnect = (
                    teleop.native.fault is None and teleop.torque.enabled is True)
                teleop.aligned = False
                teleop.manual_control = False
                ctx.hardware.robot_wrapper.inner.manual_control = False
                ctx.hardware.robot_wrapper.inner.native.cfg['manual_control'] = False
                self._events.phase = DAggerPhase.PAUSED
                print('精对齐后主臂姿态发生变化；本次未接管。请保持支撑，手动摆近后按b重新对齐。', flush=True)
                return
            print('开始人工纠正录制；请人工完成剩余抓取和放置。完成整个任务后按1结束本轮并进入复位；'
                  'b/Tab只暂停当前纠正，不结束任务。', flush=True)
            self._attempt_intervened = True
            ctx.hardware.robot_wrapper.inner.end_fixed_hold()
            teleop.reset_correction_slew()
        # 人工已经完成整项任务时，主臂应自然卸力并进入成功后复位。
        # 官方的 correcting->paused 分支会重新上力并读取寄存器82，
        # 这既不是本流程需要的动作，也会让一次收尾依赖主臂配置读回。
        # 普通 b/Tab 暂停仍走官方分支，以便暂停纠正时保持主臂姿态。
        completed_by_human = (
            old_phase == DAggerPhase.CORRECTING
            and new_phase == DAggerPhase.PAUSED
            and self._human_completed_correction
        )
        if completed_by_human:
            teleop.manual_control = False
            ctx.hardware.robot_wrapper.inner.manual_control = False
            ctx.hardware.robot_wrapper.inner.native.cfg['manual_control'] = False
            ctx.hardware.robot_wrapper.inner.end_fixed_hold()
            teleop.aligned = False
            teleop.target = None
            teleop.keep_torque_on_disconnect = False
            teleop.journal('correction_completed_without_rearm')
            if timer is not None:
                timer.restart()
        else:
            super()._apply_transition(old_phase, new_phase, engine, interpolator, teleop,
                                      ctx, prev_action, timer)
        if new_phase == DAggerPhase.AUTONOMOUS:
            ctx.hardware.robot_wrapper.inner.end_fixed_hold()
            teleop.aligned = False
            teleop.manual_control = False
            ctx.hardware.robot_wrapper.inner.manual_control = False
            ctx.hardware.robot_wrapper.inner.native.cfg['manual_control'] = False
        if (old_phase == DAggerPhase.CORRECTING and new_phase == DAggerPhase.PAUSED
                and not completed_by_human):
            # 纠正结束恢复模型阶段保护；再次接管仍须与固定目标匹配。
            teleop.manual_control = False
            ctx.hardware.robot_wrapper.inner.manual_control = False
            ctx.hardware.robot_wrapper.inner.native.cfg['manual_control'] = False
            ctx.hardware.robot_wrapper.inner.end_fixed_hold()
            teleop.target = ctx.hardware.robot_wrapper.inner.begin_fixed_hold()
            teleop.aligned = True
    def setup(self, ctx):
        # Esc或进程停止在每个主臂插值周期检查，不等完整对齐结束。
        # 本入口会在同一个官方 context 内连续执行多轮任务；每轮
        # TaskSucceeded 只保存 episode，最终退出时才由 teardown finalize。
        self._finalize_dataset_on_run_exit = False
        self._follower = ctx.hardware.robot_wrapper.inner
        from model_coordinates import ModelCoordinates, install_dagger_coordinates
        coordinates = ModelCoordinates(self._follower.cfg, self._follower.profile, verify_sources=False)
        install_dagger_coordinates(ctx, coordinates)
        if coordinates.provenance is not None:
            self._follower.journal('model_coordinates', **coordinates.provenance)
        self._features = ctx.data.dataset_features
        self._rename = ctx.runtime.cfg.rename_map
        # 远程引擎通过 client 暴露能力；ACT 本地同步引擎直接使用策略配置。
        inference = ctx.policy.inference
        remote_client = getattr(inference, 'client', None)
        if remote_client is not None:
            capabilities = remote_client.capabilities
            self._expected_features = {feature.name for feature in capabilities.features}
        else:
            policy = ctx.policy.policy
            if policy is None or not hasattr(policy, 'config'):
                raise RuntimeError('本地推理缺少策略配置，无法确定观测特征')
            self._expected_features = set(policy.config.input_features)
        ctx.hardware.teleop.stop_requested = lambda: (
            ctx.runtime.shutdown_event.is_set() or self._events.stop_recording.is_set())
        ctx.hardware.teleop.follower_hold = lambda: self.hold_control_state(ctx.hardware)
        self._task_ctx = ctx
        super().setup(ctx)

    def _process_observation_and_notify(self, processors, obs_raw):
        # 使用真实双摄/串口中最早的采集时间，不能把HTTP返回时刻当作新图像。
        if self._cached_obs_processed is None or self._require_interpolator().needs_new_action():
            processed = processors.robot_observation_processor(obs_raw)
            frame = build_dataset_frame(self._features, processed, prefix='observation')
            mapped = {self._rename.get(key, key): value for key, value in frame.items()}
            if len(mapped) != len(frame):
                raise ValueError('观测名称映射冲突')
            mapped = {key: value for key, value in mapped.items() if key in self._expected_features}
            engine = self._require_engine()
            if hasattr(engine, 'submit_observation'):
                # 远程引擎需要显式记录采集时刻，避免网络发送时间续期。
                engine.submit_observation(mapped, self._follower.capture_monotonic)
            else:
                # 本地同步引擎没有远程队列；动作会在同一控制周期直接读取当前观测。
                engine.notify_observation(mapped)
            self._cached_obs_processed = processed
        return self._cached_obs_processed
