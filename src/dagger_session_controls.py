"""DAgger现场成功信号与回位；复用官方纠正录制，不把自主成功伪装成训练片段。"""
from contextlib import redirect_stdout
import sys
import termios
from threading import Event
import time

from lerobot.rollout.strategies.dagger import (
    DAggerEvents, DAggerPhase, _init_dagger_keyboard,
)

from dagger_native_devices import read_json


def discard_control_input():
    # 全局按键监听不消费终端输入；显示新口令前丢弃旧控制键及其回车。
    if sys.stdin.isatty():
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)


class TaskSucceeded(Exception):
    """结束当前自主尝试，交给现场确认的回位流程。"""


class CaptureStopped(Exception):
    """停止请求中断当前周期，禁止再发一条动作。"""


class CaptureEvents(DAggerEvents):
    """将成功请求与暂停/纠正转换分开，键盘线程不操作机械臂。"""

    def __init__(self):
        # 成功信号只在控制线程消费。
        super().__init__()
        self.success_requested = Event()
        self.correction_success_requested = Event()

    def request_transition(self, event):
        # 只有自主阶段接受成功；纠正片段必须通过b保存。
        if event == 'success':
            if self.phase == DAggerPhase.AUTONOMOUS:
                self.success_requested.set()
            elif self.phase == DAggerPhase.CORRECTING:
                # 人工接管期间的1表示：人工已完成剩余任务，保存完整纠正episode。
                self.correction_success_requested.set()
            return
        super().request_transition(event)

    def reset(self):
        # 丢弃上一次尝试的所有待处理按键。
        super().reset()
        self.success_requested.clear()
        self.correction_success_requested.clear()


class CaptureSessionControl:
    """给原生DAgger增加成功后回位，并按完整轨迹数量继续采集。"""

    def __init__(self, config):
        # 统计与官方数据集片段数分开。
        super().__init__(config)
        self._events = CaptureEvents()
        self.autonomous_successes = 0
        self.completed_tasks = 0
        self.intervened_tasks = 0
        self._attempt_intervened = False
        self._human_completed_correction = False
        self._capture_deadline = None
        self._task_ctx = None

    def _tick(self, timer, *, new_cycle=False):
        # 每周期先处理人工意图，再允许新的模型动作。
        super()._tick(timer, new_cycle=new_cycle)
        if self._events.stop_recording.is_set() or self._task_ctx.runtime.shutdown_event.is_set():
            raise CaptureStopped()
        if self._events.success_requested.is_set():
            self._events.success_requested.clear()
            self._require_engine().pause()
            self._events.phase = DAggerPhase.PAUSED
            self.hold_control_state(self._task_ctx.hardware)
            raise TaskSucceeded()
        if (self._events.phase == DAggerPhase.CORRECTING
                and self._events.correction_success_requested.is_set()):
            self._events.correction_success_requested.clear()
            self._human_completed_correction = True
            # 人工按1表示整轮任务已完成。先执行官方 correcting->paused 的
            # 收尾（恢复保持、关闭人工模式），再抛出任务完成信号；外层
            # finally 会把自主帧和人工帧一起落盘，然后进入复位流程。
            self._events.request_transition('correction')
            transition = self._events.consume_transition()
            if transition is None:
                raise RuntimeError('人工完成信号未能结束纠正状态')
            print('已收到人工完成信号1，正在保存自主帧和人工帧；保存完成后进入复位确认。', flush=True)
            self._apply_transition(
                transition[0], transition[1], self._require_engine(),
                self._require_interpolator(),
                self._require_teleop(self._task_ctx.hardware),
                self._task_ctx, None, timer,
            )
            raise TaskSucceeded()
        if self._events.phase == DAggerPhase.PAUSED:
            self.hold_control_state(self._task_ctx.hardware)

    def _success_home(self, ctx):
        # 每轮成功后先释放主臂，让操作者手动摆回起始姿态；从臂再交给原home。
        from mac_home import run as home_run
        follower = ctx.hardware.robot_wrapper.inner
        leader = ctx.hardware.teleop
        engine = self._require_engine()
        if engine.failed or ctx.runtime.shutdown_event.is_set() or self._events.stop_recording.is_set():
            raise RuntimeError('采集已停止或推理故障，不进入成功回位')
        catalog = read_json(follower.settings['operator_config'])
        settings = read_json(catalog['home_config'])
        output = follower.journal.path.parent / f'success-home-{self.completed_tasks:03d}'
        listener = self._listener
        if listener is not None:
            listener.stop()
            self._listener = None
        discard_control_input()
        if getattr(leader, 'connected', False) is True:
            leader.disable_torque()
            leader.aligned = False
            print('本轮完成。主臂已卸力，请手动摆回下一轮起始位置；摆好后按回车继续：',
                  end='', flush=True)
            input()
            discard_control_input()
            if ctx.runtime.shutdown_event.is_set() or self._events.stop_recording.is_set():
                return
            # 重新以当前手动摆好的姿态上力，保证下一次主臂自动对齐有稳定起点。
            leader.enable_torque()
            print('主臂起始姿态已确认并保持。现在进入从臂自动复位。', flush=True)
        follower.native.close()
        follower.connected = False
        terminal = sys.stdout
        def confirm(prompt):
            # 回位过程写日志，但现场路径确认始终显示在终端。
            print(prompt, end='', file=terminal, flush=True)
            return input()
        log_path = output.parent / (output.name + '-console.log')
        print('回位详细日志：' + str(log_path), flush=True)
        with log_path.open('x', encoding='utf-8') as log, redirect_stdout(log):
            result = home_run(settings, 'auto', output, execute=True, ask=confirm)
        if result.get('error') or not result.get('aligned'):
            raise RuntimeError(f'成功后复位未完成：{result.get("error") or "未到达目标"}；'
                               f'采集已停止，记录：{output / "summary.json"}')
        if ctx.runtime.shutdown_event.is_set() or self._events.stop_recording.is_set():
            return
        # 回位之后重新连接只读核对，不写扭矩；不清除任何已锁存故障。
        if engine.failed or follower.native.fault or follower.guard.fault:
            raise RuntimeError('从臂存在故障，禁止回位后继续')
        follower.feedback_history.clear()
        if follower.native.velocity_guard is not None:
            follower.native.velocity_guard.history.clear()
        follower.native.open()
        follower.native.verify_torque()
        follower.connected = True
        follower.manual_control = False
        follower.native.cfg['manual_control'] = False
        ctx.hardware.teleop.manual_control = False
        ctx.hardware.teleop.aligned = False
        self._cached_obs_processed = None
        follower.end_fixed_hold()
        self.hold_control_state(ctx.hardware)
        print('复位完成。从臂保持；请摆好下一轮场景、双手离开后按p开始；q退出。', flush=True)
        # 此处使用新键盘队列，避免“开始复位”输入串成后续控制指令。
        self._events.reset()
        self._events.phase = DAggerPhase.PAUSED
        self._listener = _init_dagger_keyboard(self._events, self.config.keyboard)
        while not ctx.runtime.shutdown_event.is_set() and not self._events.stop_recording.is_set():
            if engine.failed:
                raise RuntimeError('等待下一轮时推理服务故障，采集已停止')
            if self._capture_deadline is not None and time.perf_counter() >= self._capture_deadline:
                return
            self.hold_control_state(ctx.hardware)
            transition = self._events.consume_transition()
            if transition is not None and transition[1] == DAggerPhase.AUTONOMOUS:
                follower.end_fixed_hold()
                return
            time.sleep(.05)

    def run(self, ctx):
        # 每次成功只重开自主尝试；完整轨迹数量按数据集实际落盘数量扣减。
        self._task_ctx = ctx
        dataset = self._require_dataset(ctx.data)
        initial_count = dataset.num_episodes
        target = self.config.num_episodes
        original_duration = ctx.runtime.cfg.duration
        deadline = time.perf_counter() + original_duration if original_duration > 0 else None
        self._capture_deadline = deadline
        try:
            while (dataset.num_episodes - initial_count < target
                   and not ctx.runtime.shutdown_event.is_set()
                   and not self._events.stop_recording.is_set()):
                if deadline is not None:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    ctx.runtime.cfg.duration = remaining
                self.config.num_episodes = target - (dataset.num_episodes - initial_count)
                self._attempt_intervened = False
                self._human_completed_correction = False
                try:
                    super().run(ctx)
                    break
                except CaptureStopped:
                    break
                except TaskSucceeded:
                    self.completed_tasks += 1
                    if not self._attempt_intervened:
                        self.autonomous_successes += 1
                    else:
                        self.intervened_tasks += 1
                    self._follower.journal('task_success', count=self.completed_tasks,
                        autonomous=not self._attempt_intervened,
                        saved_episodes=dataset.num_episodes - initial_count,
                        intervened_tasks=self.intervened_tasks)
                    print(f'完成 {self.completed_tasks} 次（自主成功{self.autonomous_successes}次，'
                          f'人工接管{self.intervened_tasks}次）；已保存完整轨迹 '
                          f'{dataset.num_episodes - initial_count}/{target} 段。进入复位确认。', flush=True)
                    self._success_home(ctx)
                    if ctx.runtime.shutdown_event.is_set() or self._events.stop_recording.is_set():
                        break
                    self._follower.end_fixed_hold()
        finally:
            self.config.num_episodes = target
            ctx.runtime.cfg.duration = original_duration
            self._capture_deadline = None
