# -*- coding: utf-8 -*-
"""
hud_core.animations - 轻量、高性能 60fps 动效调度模块。

提供基础插值原语、纯 Python 缓动曲线、补间/循环任务管理，
以及支持零开销停止与可见性门控（Snap to Target）的中央调度器。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtGui import QColor, QGuiApplication, QScreen


def probe_screen_refresh_rate(screen: Optional[QScreen] = None) -> float:
    """探测当前屏幕物理刷新率（Hz），失败或异常时安全回退至 60.0。"""
    try:
        scr = screen
        if scr is None:
            app = QGuiApplication.instance()
            if app is not None and hasattr(app, "primaryScreen"):
                scr = app.primaryScreen()
        if scr is not None:
            rate = float(scr.refreshRate())
            if 20.0 <= rate <= 500.0:
                return rate
    except Exception:
        pass
    return 60.0


# ==========================================================================
# 1. 基础插值原语
# ==========================================================================


def lerp(a: float, b: float, t: float) -> float:
    """线性插值：a + (b - a) * t。"""
    return float(a + (b - a) * t)


def _to_qcolor(c: QColor | str) -> QColor:
    """将 QColor 或颜色字符串安全转换为有效 QColor。"""
    if isinstance(c, QColor):
        return QColor(c)
    col = QColor(str(c))
    if not col.isValid():
        return QColor(0, 0, 0, 255)
    return col


def lerp_color(c1: QColor | str, c2: QColor | str, t: float) -> QColor:
    """对两个颜色的 RGBA 通道进行平滑线性插值，返回 QColor。"""
    col1 = _to_qcolor(c1)
    col2 = _to_qcolor(c2)
    r = int(round(lerp(col1.red(), col2.red(), t)))
    g = int(round(lerp(col1.green(), col2.green(), t)))
    b = int(round(lerp(col1.blue(), col2.blue(), t)))
    a = int(round(lerp(col1.alpha(), col2.alpha(), t)))
    return QColor(
        max(0, min(255, r)),
        max(0, min(255, g)),
        max(0, min(255, b)),
        max(0, min(255, a)),
    )


# ==========================================================================
# 2. 纯 Python 高性能缓动函数（输入 t 范围 [0.0, 1.0]）
# ==========================================================================


def _clamp01(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return float(t)


def out_quart(t: float) -> float:
    """四次方减速缓动：1.0 - (1.0 - t) ** 4。"""
    u = _clamp01(t)
    inv = 1.0 - u
    return 1.0 - (inv * inv * inv * inv)


def out_back(t: float, s: float = 1.70158) -> float:
    """OutBack 弹性过冲缓动：1.0 + (s + 1.0) * ((t - 1.0) ** 3) + s * ((t - 1.0) ** 2)。"""
    u = _clamp01(t)
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    p = u - 1.0
    return 1.0 + (s + 1.0) * (p * p * p) + s * (p * p)


def in_out_cubic(t: float) -> float:
    """前半段三次加速、后半段三次减速的平滑曲线。"""
    u = _clamp01(t)
    if u < 0.5:
        return 4.0 * u * u * u
    p = -2.0 * u + 2.0
    return 1.0 - (p * p * p) * 0.5


def sine_breath(t: float) -> float:
    """余弦平滑映射到 [0.0, 1.0] 的完整周期呼吸曲线（t=0->0, t=0.5->1, t=1->0）。"""
    u = float(t) % 1.0 if (t < 0.0 or t > 1.0) else float(t)
    return 0.5 * (1.0 - math.cos(u * 2.0 * math.pi))


# ==========================================================================
# 3. 补间任务与循环任务数据结构
# ==========================================================================


@dataclass
class TweenTask:
    """单次属性补间任务。"""

    key: str
    start_val: float
    end_val: float
    duration_ms: int
    easing_fn: Callable[[float], float] = out_quart
    on_update: Optional[Callable[[float], None]] = None
    on_finished: Optional[Callable[[], None]] = None
    start_time: float = field(default_factory=time.perf_counter)


@dataclass
class LoopTask:
    """持续周期循环任务（用于呼吸灯、流光角等无尽循环）。"""

    key: str
    duration_ms: int
    on_tick: Callable[[float], None]
    start_time: float = field(default_factory=time.perf_counter)


# ==========================================================================
# 4. 单一中央调度器 AnimationCoordinator
# ==========================================================================


class AnimationCoordinator(QObject):
    """
    单一 QTimer 驱动的中央动效调度器。

    特性：
    - 统一使用单个 16ms（约 60fps）QTimer 驱动所有补间与循环任务。
    - 同 key 补间任务自动互斥覆盖。
    - 零开销状态：当无活跃 tween 且无活跃 loop 时立即停止 QTimer，绝不空转。
    - 待机与可见性门控（Snap to Target）：支持暂停并将所有进行中的 tween 直接跳至终态。
    """

    DEFAULT_INTERVAL_MS = 16

    def __init__(self, parent: Optional[QObject] = None, interval_ms: int = DEFAULT_INTERVAL_MS) -> None:
        super().__init__(parent)
        self._refresh_rate: float = 60.0
        self._interval_ms = max(4, int(interval_ms))
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._on_timer_tick)
        self.sync_with_screen()

        self._tweens: dict[str, TweenTask] = {}
        self._loops: dict[str, LoopTask] = {}
        self._paused: bool = False

    def sync_with_screen(self, screen: Optional[QScreen] = None) -> None:
        """根据目标显示器的物理刷新率自动调整 QTimer 帧间隔。"""
        rate = probe_screen_refresh_rate(screen)
        self._refresh_rate = rate
        # 帧间隔公式：1000.0 / rate（向下夹紧至 4ms，即最高 250 FPS；上限 33ms，即最低 30 FPS）
        interval_ms = max(4, min(33, int(round(1000.0 / rate))))
        self._interval_ms = interval_ms
        self._timer.setInterval(self._interval_ms)

    @property
    def refresh_rate(self) -> float:
        return getattr(self, "_refresh_rate", 60.0)

    @property
    def effective_fps(self) -> int:
        return int(round(1000.0 / max(1, self._interval_ms)))

    @property
    def timer(self) -> QTimer:
        """暴露内部 QTimer 供状态检查与测试断言。"""
        return self._timer

    @property
    def is_paused(self) -> bool:
        """当前调度器是否处于暂停门控状态。"""
        return self._paused

    def is_running(self) -> bool:
        """内部计时器是否正在运行。"""
        return self._timer.isActive()

    def active_tween_count(self) -> int:
        """当前活跃的补间任务数量。"""
        return len(self._tweens)

    def active_loop_count(self) -> int:
        """当前活跃的循环任务数量。"""
        return len(self._loops)

    def has_tween(self, key: str) -> bool:
        """判断指定 key 的补间任务是否在运行。"""
        return key in self._tweens

    def has_loop(self, key: str) -> bool:
        """判断指定 key 的循环任务是否在运行。"""
        return key in self._loops

    def is_loop_running(self, key: str) -> bool:
        """判断指定 key 的循环任务是否正在运行。"""
        return str(key) in self._loops and not self._paused

    def _sync_timer_state(self) -> None:
        """根据暂停状态与活跃任务列表同步 QTimer 启停，确保空闲时绝对 0 开销。"""
        if self._paused or (not self._tweens and not self._loops):
            if self._timer.isActive():
                self._timer.stop()
        else:
            if not self._timer.isActive():
                self._timer.start()

    def start_tween(
        self,
        key: str,
        start_val: float,
        end_val: float,
        duration_ms: int = 240,
        easing_fn: Callable[[float], float] = out_quart,
        on_update: Optional[Callable[[float], None]] = None,
        on_finished: Optional[Callable[[], None]] = None,
    ) -> Optional[TweenTask]:
        """
        启动或覆盖同 key 的补间任务。
        若当前处于暂停状态或 duration_ms <= 0，立即同步跳至终态而不启动定时器。
        """
        key_str = str(key)
        # 移除旧的同 key 任务（直接互斥替换）
        self._tweens.pop(key_str, None)

        s_val = float(start_val)
        e_val = float(end_val)
        dur = int(duration_ms)

        if self._paused or dur <= 0:
            if on_update is not None:
                on_update(e_val)
            if on_finished is not None:
                on_finished()
            self._sync_timer_state()
            return None

        task = TweenTask(
            key=key_str,
            start_val=s_val,
            end_val=e_val,
            duration_ms=dur,
            easing_fn=easing_fn,
            on_update=on_update,
            on_finished=on_finished,
            start_time=time.perf_counter(),
        )
        self._tweens[key_str] = task
        if on_update is not None:
            on_update(s_val)
        self._sync_timer_state()
        return task

    def stop_tween(self, key: str, snap_to_target: bool = False) -> None:
        """停止指定 key 的补间任务，可选直接跳至目标值。"""
        task = self._tweens.pop(str(key), None)
        if task is not None and snap_to_target:
            if task.on_update is not None:
                task.on_update(task.end_val)
            if task.on_finished is not None:
                task.on_finished()
        self._sync_timer_state()

    def start_loop(
        self,
        key: str,
        duration_ms: int,
        on_tick: Callable[[float], None],
    ) -> LoopTask:
        """
        注册或更新持续循环任务。
        若同 key 任务已在运行，保留其原有的 start_time（仅更新 duration_ms 与 on_tick），避免重置相位。
        on_tick 接收周期归一化相位 phase（范围 [0.0, 1.0)）。
        """
        key_str = str(key)
        dur = max(1, int(duration_ms))
        existing = self._loops.get(key_str)
        if existing is not None:
            existing.duration_ms = dur
            existing.on_tick = on_tick
            task = existing
        else:
            task = LoopTask(
                key=key_str,
                duration_ms=dur,
                on_tick=on_tick,
                start_time=time.perf_counter(),
            )
            self._loops[key_str] = task
        self._sync_timer_state()
        return task

    def stop_loop(self, key: str) -> None:
        """停止并移除指定 key 的循环任务。"""
        self._loops.pop(str(key), None)
        self._sync_timer_state()

    def pause_animations(self, snap_to_target: bool = True) -> None:
        """
        暂停动效调度并立即停止定时器（用于窗口隐藏、最小化或待机退避）。
        若 snap_to_target=True，所有未完成的 TweenTask 立即触发 on_update(end_val) 与 on_finished 并清空。
        """
        self._paused = True
        if self._timer.isActive():
            self._timer.stop()

        if snap_to_target and self._tweens:
            pending = list(self._tweens.values())
            self._tweens.clear()
            for task in pending:
                if task.on_update is not None:
                    task.on_update(task.end_val)
                if task.on_finished is not None:
                    task.on_finished()
        elif not snap_to_target:
            pass

    def resume_animations(self) -> None:
        """恢复动效调度；若存在活跃任务则重置其起始时间基准并重启定时器。"""
        if not self._paused:
            self._sync_timer_state()
            return
        self._paused = False
        now = time.perf_counter()
        for task in self._tweens.values():
            task.start_time = now
        for loop in self._loops.values():
            loop.start_time = now
        self._sync_timer_state()

    def stop_all(self, snap_to_target: bool = False) -> None:
        """清空所有补间与循环任务并停止计时器。"""
        if snap_to_target and self._tweens:
            pending = list(self._tweens.values())
            self._tweens.clear()
            for task in pending:
                if task.on_update is not None:
                    task.on_update(task.end_val)
                if task.on_finished is not None:
                    task.on_finished()
        else:
            self._tweens.clear()
        self._loops.clear()
        self._sync_timer_state()

    def _on_timer_tick(self) -> None:
        """单帧调度回调：推进所有活跃补间与循环任务。"""
        if self._paused:
            self._sync_timer_state()
            return

        now = time.perf_counter()

        # 1. 推进补间任务
        if self._tweens:
            finished_tasks: list[TweenTask] = []
            for key, task in list(self._tweens.items()):
                # 防止回调期间该 key 已被移除或替换
                if self._tweens.get(key) is not task:
                    continue
                elapsed_ms = (now - task.start_time) * 1000.0
                if task.duration_ms <= 0 or elapsed_ms >= task.duration_ms:
                    self._tweens.pop(key, None)
                    if task.on_update is not None:
                        task.on_update(task.end_val)
                    finished_tasks.append(task)
                else:
                    raw_t = max(0.0, min(1.0, elapsed_ms / float(task.duration_ms)))
                    eased_t = task.easing_fn(raw_t)
                    cur_val = lerp(task.start_val, task.end_val, eased_t)
                    if task.on_update is not None:
                        task.on_update(cur_val)

            for task in finished_tasks:
                if task.on_finished is not None:
                    task.on_finished()

        # 2. 推进循环任务
        if self._loops:
            for key, loop in list(self._loops.items()):
                if self._loops.get(key) is not loop:
                    continue
                elapsed_ms = max(0.0, (now - loop.start_time) * 1000.0)
                phase = (elapsed_ms % float(loop.duration_ms)) / float(loop.duration_ms)
                loop.on_tick(phase)

        # 3. 零开销检查：若队列已空，立即停止 QTimer
        self._sync_timer_state()


_GLOBAL_ANIMATOR: Optional[AnimationCoordinator] = None


def get_global_animator() -> AnimationCoordinator:
    """获取全局共享的 AnimationCoordinator 单例。"""
    global _GLOBAL_ANIMATOR
    if _GLOBAL_ANIMATOR is None:
        _GLOBAL_ANIMATOR = AnimationCoordinator()
    return _GLOBAL_ANIMATOR
