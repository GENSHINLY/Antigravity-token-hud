# -*- coding: utf-8 -*-
"""
hud_core.ui_widgets - 格式化工具、调色板派生、ResizeGripWidget、SplitterHandleWidget、SidebarRowWidget。
"""
from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING, Any, Callable, Optional

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QConicalGradient, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .animations import get_global_animator, lerp, lerp_color, out_quart, sine_breath
from .config import THEME_PRESETS, _HEX_COLOR_RE
from .pricing import format_cost_usd

if TYPE_CHECKING:
    from .hud_window import TokenHUDWindow


def format_num(n: int) -> str:
    """千分位格式化数字。"""
    return f"{n:,}"


def format_compact_tokens(n: int) -> str:
    """紧凑格式化 Token 数量（如 40.1M, 256k, 850）。"""
    val = max(0, int(n))
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        if val >= 100_000:
            return f"{val / 1_000:.0f}k"
        return f"{val / 1_000:.1f}k"
    return str(val)


def format_short_time(ts: float) -> str:
    """将 epoch 时间戳格式化为简短时间（今日显示 HH:MM，往日显示 MM-DD HH:MM）。"""
    if ts <= 0:
        return "--:--"
    try:
        dt = datetime.datetime.fromtimestamp(ts)
        if dt.date() == datetime.date.today():
            return dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return "--:--"


def format_duration(seconds: float) -> str:
    """格式化持续秒数为人类可读文本。"""
    sec = max(0, int(seconds))
    if sec < 60:
        return f"{sec}s"
    mins, rem = divmod(sec, 60)
    if mins < 60:
        return f"{mins}m {rem:02d}s"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h {mins:02d}m"


def _derive_theme_palette(theme_cfg: dict[str, Any]) -> dict[str, str]:
    """根据配置的 5 项核心色值派生完整 UI 调色板。"""
    default_t = THEME_PRESETS["Gruvbox Material"]
    bg_color = str(theme_cfg.get("bg_color") or default_t["bg_color"]).strip()
    fg_color = str(theme_cfg.get("fg_color") or default_t["fg_color"]).strip()
    accent_color = str(theme_cfg.get("accent_color") or default_t["accent_color"]).strip()
    card_bg_color = str(theme_cfg.get("card_bg_color") or default_t["card_bg_color"]).strip()
    border_color = str(theme_cfg.get("border_color") or default_t["border_color"]).strip()

    if not _HEX_COLOR_RE.match(bg_color):
        bg_color = default_t["bg_color"]
    if not _HEX_COLOR_RE.match(fg_color):
        fg_color = default_t["fg_color"]
    if not _HEX_COLOR_RE.match(accent_color):
        accent_color = default_t["accent_color"]
    if not _HEX_COLOR_RE.match(card_bg_color):
        card_bg_color = default_t["card_bg_color"]
    if not _HEX_COLOR_RE.match(border_color):
        border_color = default_t["border_color"]

    c_bg = QColor(bg_color)
    c_card = QColor(card_bg_color)
    c_fg = QColor(fg_color)
    c_acc = QColor(accent_color)

    # 派生内部面板色、悬停色、选中色与次要文字色
    inner_q = QColor(
        (c_bg.red() + c_card.red()) // 2,
        (c_bg.green() + c_card.green()) // 2,
        (c_bg.blue() + c_card.blue()) // 2,
    )
    if inner_q == c_card:
        inner_q = c_card.darker(115) if c_card.lightness() > 20 else c_card.lighter(135)

    hover_q = c_card.lighter(135) if c_card.lightness() < 128 else c_card.darker(115)
    if hover_q.lightness() < 25:
        hover_q = QColor(min(255, c_card.red() + 22), min(255, c_card.green() + 24), min(255, c_card.blue() + 26))

    sel_q = QColor(
        int(c_card.red() * 0.78 + c_acc.red() * 0.22),
        int(c_card.green() * 0.78 + c_acc.green() * 0.22),
        int(c_card.blue() * 0.78 + c_acc.blue() * 0.22),
    )
    muted_q = QColor(
        int(c_fg.red() * 0.62 + c_bg.red() * 0.38),
        int(c_fg.green() * 0.62 + c_bg.green() * 0.38),
        int(c_fg.blue() * 0.62 + c_bg.blue() * 0.38),
    )

    return {
        "BG_MAIN": bg_color,
        "BG_HEADER": bg_color,
        "BG_CARD": card_bg_color,
        "BG_INNER": inner_q.name(),
        "BG_HOVER": hover_q.name(),
        "BG_SELECTED": sel_q.name(),
        "FG_PRIMARY": fg_color,
        "FG_MUTED": muted_q.name(),
        "FG_ACCENT": accent_color,
        "BORDER_COLOR": border_color,
    }


class ResizeGripWidget(QWidget):
    """
    右下角自定义缩放手柄组件：
    - 使用 QPainter 绘制 Gruvbox 风格斜纹防滑线，悬停或拖拽时高亮；
    - 支持左键拖拽实时无级缩放窗口宽高。
    """

    def __init__(self, hud_window: "TokenHUDWindow"):
        super().__init__(hud_window)
        self._hud = hud_window
        self._hovered = False
        self._dragging = False
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("拖拽调整窗口大小")

    def enterEvent(self, event: Any) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            g_pos = event.globalPosition().toPoint()
            self._hud._on_resize_press(g_pos.x(), g_pos.y())
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            g_pos = event.globalPosition().toPoint()
            self._hud._on_resize_motion(g_pos.x(), g_pos.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            g_pos = event.globalPosition().toPoint()
            self._hud._on_resize_release(g_pos.x(), g_pos.y())
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent_hex = getattr(self._hud, "FG_ACCENT", "#E78A4E")
        muted_hex = getattr(self._hud, "FG_MUTED", "#928374")
        color = QColor(accent_hex) if (self._hovered or self._dragging) else QColor(muted_hex)
        pen = QPen(color, 1.4)
        painter.setPen(pen)
        w, h = self.width(), self.height()
        for offset in (4, 8, 12):
            painter.drawLine(w - offset, h - 2, w - 2, h - offset)


class SplitterHandleWidget(QWidget):
    """
    左右双栏（子菜单与右侧 Token 统计页）之间的一体化居中胶囊折叠与比例调整手柄：
    - 单击切换侧边栏折叠/展开，按住左右拖拽实时调整子菜单与右侧统计页宽度占比；
    - 居中显示圆角胶囊与折叠方向指示箭头，悬停或拖拽时高亮。
    """

    def __init__(self, hud_window: "TokenHUDWindow", parent: QWidget | None = None):
        super().__init__(parent)
        self._hud = hud_window
        self._hovered = False
        self._dragging = False
        self._drag_started = False
        self._arrow_progress: float = 0.0
        self._press_pos = QPoint()
        self._press_g_pos = QPoint()
        self.setFixedWidth(14)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._sync_tooltip()

    def set_arrow_progress(self, progress: float) -> None:
        self._arrow_progress = max(0.0, min(1.0, float(progress)))
        self.update()

    def _sync_tooltip(self) -> None:
        is_collapsed = getattr(self._hud, "_sidebar_collapsed", False)
        if is_collapsed:
            self.setToolTip("单击展开侧边栏")
        else:
            self.setToolTip("单击折叠侧边栏，按住拖拽调整分栏宽度")

    def enterEvent(self, event: Any) -> None:
        self._hovered = True
        self._sync_tooltip()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_started = False
            self._press_pos = event.pos()
            self._press_g_pos = event.globalPosition().toPoint()
            self._hud._on_splitter_press(self._press_g_pos.x())
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            if not self._drag_started:
                if (event.pos() - self._press_pos).manhattanLength() > 4:
                    self._drag_started = True
            if self._drag_started:
                g_pos = event.globalPosition().toPoint()
                self._hud._on_splitter_motion(g_pos.x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            if not self._drag_started:
                self._hud._is_splitting = False
                self._hud.toggle_sidebar()
            else:
                g_pos = event.globalPosition().toPoint()
                self._hud._on_splitter_release(g_pos.x())
            self._drag_started = False
            self._sync_tooltip()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: Any) -> None:
        self._sync_tooltip()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        cx = w // 2
        cy = h // 2

        accent_hex = getattr(self._hud, "FG_ACCENT", "#E78A4E")
        border_hex = getattr(self._hud, "BORDER_COLOR", "#504945")
        bg_color = getattr(self._hud, "BG_COLOR", "#1D2021")
        _ = bg_color

        is_active = self._hovered or self._dragging
        active_color = QColor(accent_hex) if is_active else QColor(border_hex)

        # 绘制贯穿顶底的 1px 垂直中线
        line_color = QColor(accent_hex) if is_active else QColor(border_hex)
        if is_active:
            line_color.setAlpha(150)
        line_pen = QPen(line_color, 1.0)
        painter.setPen(line_pen)
        painter.drawLine(cx, 0, cx, h)

        # 绘制居中圆角胶囊
        pill_w = 12
        pill_h = 38
        pill_x = (w - pill_w) / 2.0
        pill_y = (h - pill_h) // 2
        pill_rect = QRectF(pill_x, float(pill_y), float(pill_w), float(pill_h))

        painter.setBrush(QColor(30, 32, 33, 220))
        painter.setPen(QPen(active_color, 1.2))
        painter.drawRoundedRect(pill_rect, 6.0, 6.0)

        # 绘制微型折叠指示箭头
        is_collapsed = getattr(self._hud, "_sidebar_collapsed", False)
        _ = is_collapsed
        # 根据 _arrow_progress 平滑过渡：0.0 偏移量 +2/-2，1.0 偏移量 -2/+2
        offset = 2.0 - 4.0 * self._arrow_progress
        p1 = QPoint(int(round(cx + offset)), cy - 5)
        p2 = QPoint(int(round(cx - offset)), cy)
        p3 = QPoint(int(round(cx + offset)), cy + 5)

        arrow_color = QColor(accent_hex) if is_active else QColor(getattr(self._hud, "FG_MUTED", "#928374"))
        arrow_pen = QPen(arrow_color, 1.8)
        arrow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        arrow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(arrow_pen)
        painter.drawLine(p1, p2)
        painter.drawLine(p2, p3)


class _SnapshotBridge(QObject):
    """跨线程信号桥，将后台线程采集的快照安全投递至 Qt 主线程。"""

    snapshot_ready = Signal(dict)


class _HoverLabel(QLabel):
    """支持鼠标点击与进入/离开回调的轻量级 QLabel（不创建独立 Win32 子窗口句柄）。"""

    clicked = Signal()
    hovered = Signal(bool)

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def enterEvent(self, event: Any) -> None:
        self.hovered.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self.hovered.emit(False)
        super().leaveEvent(event)


class AnimatedNumberLabel(QLabel):
    """
    平滑数值滚动标签：
    - 支持数字变动的平滑滚动补间动画；
    - 动画启动前预先按目标格式化文本测量并锁定字号，防止插值过程中因位数变动引发抖动；
    - 保留全量 QLabel 接口兼容性（text(), setText() 等），在 instant=True 或无动画时立即显示终态。
    """

    def __init__(self, text: str = "0", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self._current_value: float = self._parse_num(text)
        self._target_value: float = self._current_value
        self._format_fn: Callable[[float], str] = lambda v: format_num(int(round(v)))

    @staticmethod
    def _parse_num(val_str: Any) -> float:
        try:
            s = str(val_str).replace(",", "").strip()
            # 支持提取百分比或纯数字
            if s.endswith("%"):
                return float(s[:-1])
            return float(s)
        except Exception:
            return 0.0

    def current_value(self) -> float:
        return self._current_value

    def target_value(self) -> float:
        return self._target_value

    def snap_to_target(self) -> None:
        """立即跳至终态值。"""
        get_global_animator().stop_tween(f"num_lbl_{id(self)}", snap_to_target=True)

    def set_target_value(
        self,
        val: float,
        format_fn: Optional[Callable[[float], str]] = None,
        duration_ms: int = 240,
        instant: bool = False,
    ) -> None:
        """设置目标数值并平滑插值滚动。"""
        if format_fn is not None:
            self._format_fn = format_fn
        self._target_value = float(val)

        # 终态文本
        final_text = self._format_fn(self._target_value)

        # 若处于 instant 模式或无需动画
        if instant or duration_ms <= 0 or get_global_animator().is_paused:
            get_global_animator().stop_tween(f"num_lbl_{id(self)}")
            self._current_value = self._target_value
            super().setText(final_text)
            return

        def _on_update(cur_v: float) -> None:
            self._current_value = cur_v
            super(AnimatedNumberLabel, self).setText(self._format_fn(cur_v))

        def _on_finished() -> None:
            self._current_value = self._target_value
            super(AnimatedNumberLabel, self).setText(final_text)

        get_global_animator().start_tween(
            key=f"num_lbl_{id(self)}",
            start_val=self._current_value,
            end_val=self._target_value,
            duration_ms=duration_ms,
            easing_fn=out_quart,
            on_update=_on_update,
            on_finished=_on_finished,
        )

    def setText(self, text: str) -> None:
        """兼容传统 setText 赋值：若传入数值字符串则同步更新内部值，立即显示。"""
        parsed = self._parse_num(text)
        self._current_value = parsed
        self._target_value = parsed
        get_global_animator().stop_tween(f"num_lbl_{id(self)}")
        super().setText(str(text))


class StatusIndicatorWidget(QWidget):
    """
    顶栏状态指示器组件：
    - running: 中心实心核心点 + 外扩衰减涟漪环（1000ms 循环）；
    - active: 余弦呼吸灯（2000ms 循环）；
    - idle: 静态灰点（零定时器开销）；
    - 兼容旧 QLabel 接口（setFont, setStyleSheet, text）。
    """

    _COLOR_CSS_RE = re.compile(r"color\s*:\s*([^;]+)", re.IGNORECASE)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._status: str = "idle"
        self._color: QColor = QColor("#928374")
        self._phase: float = 0.0
        self._font: Optional[QFont] = None
        self._style_sheet: str = ""

    def set_status(self, status: str, color_hex: str = "") -> None:
        norm_status = str(status or "idle").strip().lower()
        if norm_status not in ("idle", "active", "running"):
            norm_status = "idle"

        new_color = QColor(color_hex) if color_hex else QColor(self._color)
        if not new_color.isValid():
            if norm_status == "running":
                new_color = QColor("#A9B665")
            elif norm_status == "active":
                new_color = QColor("#D8A657")
            else:
                new_color = QColor("#928374")
        elif not color_hex:
            if norm_status == "running" and self._color.name().lower() == "#928374":
                new_color = QColor("#A9B665")
            elif norm_status == "active" and self._color.name().lower() == "#928374":
                new_color = QColor("#D8A657")
            elif norm_status == "idle":
                new_color = QColor("#928374")

        animator = get_global_animator()
        loop_key = f"status_{id(self)}"

        if (
            self._status == norm_status
            and self._color.name().lower() == new_color.name().lower()
            and (
                (norm_status == "idle" and not animator.has_loop(loop_key))
                or (norm_status in ("running", "active") and (animator.is_paused or animator.has_loop(loop_key)))
            )
        ):
            return

        self._status = norm_status
        self._color = new_color

        if norm_status == "running" and not animator.is_paused:
            animator.start_loop(loop_key, duration_ms=1000, on_tick=self._on_tick)
        elif norm_status == "active" and not animator.is_paused:
            animator.start_loop(loop_key, duration_ms=2000, on_tick=self._on_tick)
        else:
            animator.stop_loop(loop_key)
            self._phase = 0.0
        self.update()

    def _on_tick(self, phase: float) -> None:
        self._phase = float(phase)
        self.update()

    def setFont(self, font: Any) -> None:  # noqa: N802
        self._font = font

    def setStyleSheet(self, style: str) -> None:  # noqa: N802
        self._style_sheet = str(style or "")
        m = self._COLOR_CSS_RE.search(self._style_sheet)
        if m:
            raw_col = m.group(1).strip()
            col = QColor(raw_col)
            if col.isValid():
                hex_low = col.name().lower()
                if hex_low in ("#a9b665", "#4caf50", "#00ff00", "#8ec07c", "#b8bb26"):
                    self.set_status("running", col.name())
                elif hex_low in ("#d8a657", "#fabd2f", "#e78a4e", "#fe8019", "#ffeb3b"):
                    self.set_status("active", col.name())
                elif hex_low in ("#928374", "#7c6f64", "#888888", "#666666"):
                    self.set_status("idle", col.name())
                else:
                    self._color = col
                    self.update()

    def text(self) -> str:
        return "●"

    def setText(self, _text: str) -> None:  # noqa: N802
        pass

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        _ = event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        center = QPointF(cx, cy)

        if self._status == "running":
            r = 4.0 + self._phase * 3.5
            alpha = max(0, min(255, int(round((1.0 - self._phase) * 160))))
            ring_col = QColor(self._color)
            ring_col.setAlpha(alpha)
            pen = QPen(ring_col, 1.2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, r, r)

            core_col = QColor(self._color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(core_col))
            painter.drawEllipse(center, 3.2, 3.2)
        elif self._status == "active":
            breath = sine_breath(self._phase)
            r = 2.8 + breath * 0.8
            alpha = max(0, min(255, int(round(120 + breath * 135))))
            col = QColor(self._color)
            col.setAlpha(alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(col))
            painter.drawEllipse(center, r, r)
        else:
            col = QColor(self._color if self._color.isValid() else "#928374")
            col.setAlpha(160)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(col))
            painter.drawEllipse(center, 3.0, 3.0)


class MetricCellWidget(QFrame):
    """
    2x2 核心指标网格单元格容器：
    - 支持数据跳变时触发高亮边框脉冲与右上角微型增量胶囊（trigger_pulse），通过 QPainter 叠加绘制；
    - 避免频繁修改 QSS 引发整树重排。
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._pulse_alpha: float = 0.0
        self._delta_alpha: float = 0.0
        self._delta_dy: float = 0.0
        self._delta_text: str = ""
        self._delta_font = QFont("Consolas", 8, QFont.Weight.Bold)
        self._accent_color: QColor = QColor("#E78A4E")

    def set_accent_color(self, color: QColor | str) -> None:
        col = QColor(color) if not isinstance(color, QColor) else QColor(color)
        if col.isValid():
            self._accent_color = col

    def trigger_pulse(
        self,
        duration_ms: int = 1200,
        color: Optional[QColor | str] = None,
        delta: int = 0,
    ) -> None:
        if color is not None:
            self.set_accent_color(color)
        self._delta_text = StepDeltaBadgeWidget.format_delta(int(delta)) if int(delta) > 0 else ""

        animator = get_global_animator()
        if animator.is_paused:
            self._pulse_alpha = 0.0
            self._delta_alpha = 0.0
            self._delta_dy = 0.0
            self.update()
            return

        def _on_update(t: float) -> None:
            u = max(0.0, min(1.0, float(t)))
            if u <= 0.5:
                self._pulse_alpha = max(0.0, 1.0 - (u / 0.5))
            else:
                self._pulse_alpha = 0.0

            if self._delta_text:
                if u <= 0.15:
                    p = out_quart(u / 0.15)
                    self._delta_alpha = p
                    self._delta_dy = lerp(3.0, 0.0, p)
                elif u <= 0.50:
                    self._delta_alpha = 1.0
                    self._delta_dy = 0.0
                else:
                    p = (u - 0.50) / 0.50
                    self._delta_alpha = max(0.0, 1.0 - p)
                    self._delta_dy = lerp(0.0, -4.0, out_quart(p))
            else:
                self._delta_alpha = 0.0
                self._delta_dy = 0.0
            self.update()

        def _on_finished() -> None:
            self._pulse_alpha = 0.0
            self._delta_alpha = 0.0
            self._delta_dy = 0.0
            self.update()

        animator.start_tween(
            f"cell_fx_{id(self)}",
            start_val=0.0,
            end_val=1.0,
            duration_ms=duration_ms,
            easing_fn=lambda x: x,
            on_update=_on_update,
            on_finished=_on_finished,
        )

    def _on_pulse_update(self, val: float) -> None:
        self._pulse_alpha = max(0.0, min(1.0, float(val)))
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._pulse_alpha <= 0.01 and (self._delta_alpha <= 0.001 or not self._delta_text):
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._pulse_alpha > 0.01:
            col = QColor(self._accent_color)
            col.setAlpha(max(0, min(255, int(self._pulse_alpha * 150))))
            pen = QPen(col, 1.2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect().adjusted(1, 1, -2, -2)), 3.0, 3.0)

        if self._delta_alpha > 0.001 and self._delta_text:
            painter.setFont(self._delta_font)
            fm = QFontMetrics(self._delta_font)
            pill_w = float(fm.horizontalAdvance(self._delta_text) + 10)
            pill_h = 15.0
            pill_x = float(self.width()) - pill_w - 4.0
            pill_y = 3.0 + self._delta_dy
            pill_rect = QRectF(pill_x, pill_y, pill_w, pill_h)

            bg_col = QColor(self._accent_color)
            bg_col.setAlpha(max(0, min(255, int(32 * self._delta_alpha))))
            fg_col = QColor(self._accent_color)
            fg_col.setAlpha(max(0, min(255, int(255 * self._delta_alpha))))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(bg_col))
            painter.drawRoundedRect(pill_rect, 3.5, 3.5)

            painter.setPen(QPen(fg_col, 1.0))
            painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, self._delta_text)


class StepDeltaBadgeWidget(QWidget):
    """
    展开态汇总卡片大号总 Token 右侧的单步增量浮动胶囊徽章（位置 A）：
    - 静止态 _alpha = 0.0，固定占位尺寸，彻底消除布局重排（Zero Relayout）；
    - 使用 AnimationCoordinator.start_tween 驱动 1400ms 三段式弹出-停留-上浮淡出动效；
    - 纯 QPainter 直绘微透圆角胶囊与 Consolas Bold 增量文本。
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(72, 24)
        self._alpha: float = 0.0
        self._dy: float = 0.0
        self._text: str = ""
        self._font = QFont("Consolas", 9, QFont.Weight.Bold)
        self._base_color = QColor(169, 182, 101)

    @staticmethod
    def format_delta(delta: int) -> str:
        val = int(delta)
        if val <= 0:
            return ""
        if val < 1_000:
            return f"+{val}"
        if val < 100_000:
            return f"+{val / 1000:.1f}k"
        if val < 1_000_000:
            return f"+{val / 1000:.0f}k"
        return f"+{val / 1_000_000:.1f}M"

    def set_base_color(self, color: QColor | str) -> None:
        col = QColor(color) if not isinstance(color, QColor) else QColor(color)
        if col.isValid():
            self._base_color = col
            if self._alpha > 0.001:
                self.update()

    def trigger_delta(self, delta: int) -> None:
        val = int(delta)
        if val <= 0:
            return
        self._text = self.format_delta(val)
        animator = get_global_animator()
        if animator.is_paused:
            self._alpha = 0.0
            self._dy = 0.0
            self.update()
            return

        def _on_update(t: float) -> None:
            u = max(0.0, min(1.0, float(t)))
            if u <= 0.15:
                p = out_quart(u / 0.15)
                self._alpha = p
                self._dy = lerp(5.0, 0.0, p)
            elif u <= 0.65:
                self._alpha = 1.0
                self._dy = 0.0
            else:
                p = (u - 0.65) / 0.35
                self._alpha = max(0.0, 1.0 - p)
                self._dy = lerp(0.0, -6.0, out_quart(p))
            self.update()

        def _on_finished() -> None:
            self._alpha = 0.0
            self._dy = 0.0
            self.update()

        animator.start_tween(
            key=f"step_delta_{id(self)}",
            start_val=0.0,
            end_val=1.0,
            duration_ms=1400,
            easing_fn=lambda x: x,
            on_update=_on_update,
            on_finished=_on_finished,
        )

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        _ = event
        if self._alpha <= 0.001 or not self._text:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(self._font)

        fm = QFontMetrics(self._font)
        text_w = fm.horizontalAdvance(self._text)
        pill_w = min(float(self.width()) - 2.0, float(text_w + 12))
        pill_h = 18.0
        pill_x = 1.0
        pill_y = (self.height() - pill_h) / 2.0 + self._dy
        pill_rect = QRectF(pill_x, pill_y, pill_w, pill_h)

        bg_col = QColor(
            self._base_color.red(),
            self._base_color.green(),
            self._base_color.blue(),
            max(0, min(255, int(35 * self._alpha))),
        )
        fg_col = QColor(
            self._base_color.red(),
            self._base_color.green(),
            self._base_color.blue(),
            max(0, min(255, int(255 * self._alpha))),
        )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg_col))
        painter.drawRoundedRect(pill_rect, 4.0, 4.0)

        painter.setPen(QPen(fg_col, 1.0))
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, self._text)


class SidebarRowWidget(QWidget):
    """
    左侧历史会话单行组件：
    - 使用 QPainter.paintEvent 直接在内存画布上绘制背景与选中高亮边框，彻底杜绝 QSS 样式表重解析开销；
    - 切换选中或悬停时仅触发毫秒级局部脏矩形重绘（update()），零销毁、零闪烁、零布局抖动。
    """

    row_clicked = Signal(str, str)

    COLOR_BG_INNER = QColor("#181A1B")
    COLOR_BG_HOVER = QColor("#262A2C")
    COLOR_BG_SELECTED = QColor("#32302F")
    COLOR_BORDER_NORMAL = QColor("#1D2021")
    COLOR_BORDER_SELECTED = QColor("#E78A4E")
    COLOR_FG_PRIMARY = "#D4BE98"
    COLOR_FG_MUTED = "#928374"
    COLOR_FG_ACCENT = "#E78A4E"

    def __init__(self, eng: str, file_path: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.eng = eng
        self.file_path = file_path
        self.is_selected = False
        self.is_hovered = False
        self._hover_alpha: float = 0.0
        self._select_alpha: float = 1.0 if self.is_selected else 0.0

        self._bg_inner = QColor(self.COLOR_BG_INNER)
        self._bg_hover = QColor(self.COLOR_BG_HOVER)
        self._bg_selected = QColor(self.COLOR_BG_SELECTED)
        self._border_normal = QColor(self.COLOR_BORDER_NORMAL)
        self._border_selected = QColor(self.COLOR_BORDER_SELECTED)
        self._fg_primary = self.COLOR_FG_PRIMARY
        self._fg_muted = self.COLOR_FG_MUTED
        self._fg_accent = self.COLOR_FG_ACCENT

        self._raw_name = ""
        self._raw_tok = "0"
        self._raw_meta = ""
        self._raw_time = ""
        self._sidebar_w = 240
        self._max_name_chars = 17
        self._last_name = ""
        self._last_tok = ""
        self._last_meta = ""
        self._last_time = ""
        self._last_selected: bool | None = None
        self._applied_scale_sig: tuple[int, int] | None = None
        self._applied_fg_sig: tuple[str, str, str, str] | None = None

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setFixedHeight(44)

        self._root_lay = QVBoxLayout(self)
        self._root_lay.setContentsMargins(7, 4, 7, 4)
        self._root_lay.setSpacing(2)

        self._top_lay = QHBoxLayout()
        self._top_lay.setContentsMargins(0, 0, 0, 0)
        self._top_lay.setSpacing(4)

        badge_text = "[主]" if eng == "main" else "[从]"
        badge_color = self._fg_accent if eng == "main" else "#A9B665"
        self.badge_lbl = QLabel(badge_text, self)
        self.badge_lbl.setFont(QFont("Microsoft YaHei UI", 7, QFont.Weight.Bold))
        self.badge_lbl.setStyleSheet(f"color: {badge_color}; background: transparent; border: none;")
        self.badge_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.badge_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._top_lay.addWidget(self.badge_lbl, 0)

        self.name_lbl = QLabel("", self)
        self._font_normal = QFont("Microsoft YaHei UI", 8, QFont.Weight.Normal)
        self._font_bold = QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold)
        self.name_lbl.setFont(self._font_normal)
        self.name_lbl.setStyleSheet(f"color: {self._fg_primary}; background: transparent; border: none;")
        self.name_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.name_lbl.setMinimumWidth(0)
        self.name_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._top_lay.addWidget(self.name_lbl, 1)

        self.tok_lbl = QLabel("0", self)
        self.tok_lbl.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        self.tok_lbl.setStyleSheet("color: #A9B665; background: transparent; border: none;")
        self.tok_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tok_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.tok_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._top_lay.addWidget(self.tok_lbl, 0)

        self._bot_lay = QHBoxLayout()
        self._bot_lay.setContentsMargins(0, 0, 0, 0)
        self._bot_lay.setSpacing(4)

        meta_font = QFont("Consolas", 7)
        self.meta_left = QLabel("", self)
        self.meta_left.setFont(meta_font)
        self.meta_left.setStyleSheet(f"color: {self._fg_muted}; background: transparent; border: none;")
        self.meta_left.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.meta_left.setMinimumWidth(0)
        self.meta_left.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._bot_lay.addWidget(self.meta_left, 1)

        self.time_lbl = QLabel("", self)
        self.time_lbl.setFont(meta_font)
        self.time_lbl.setStyleSheet(f"color: {self._fg_muted}; background: transparent; border: none;")
        self.time_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.time_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._bot_lay.addWidget(self.time_lbl, 0)

        self._root_lay.addLayout(self._top_lay)
        self._root_lay.addLayout(self._bot_lay)

    def apply_bg_opacity(self, bg_opacity: float = 0.65) -> None:
        """轻量热更新单行背景透明度：仅修改内存 QColor Alpha 值，零 QSS 解析开销。"""
        op = max(0.05, min(1.0, float(bg_opacity if bg_opacity is not None else 0.65)))
        base_alpha = int(round(op * 255))
        hover_alpha = min(255, base_alpha + 38)
        active_alpha = min(255, base_alpha + 50)
        self._bg_inner.setAlpha(base_alpha)
        self._bg_hover.setAlpha(hover_alpha)
        self._bg_selected.setAlpha(active_alpha)

    def apply_theme(
        self,
        palette: dict[str, str],
        bg_opacity: float = 0.65,
        glass_enabled: bool = False,
    ) -> None:
        """应用动态配色方案并立即重绘。"""
        self._bg_inner = QColor(palette.get("BG_INNER", "#181A1B"))
        self._bg_hover = QColor(palette.get("BG_HOVER", "#262A2C"))
        self._bg_selected = QColor(palette.get("BG_SELECTED", "#32302F"))
        self.apply_bg_opacity(bg_opacity)
        self._border_normal = QColor(palette.get("BORDER_COLOR", "#1D2021"))
        self._border_selected = QColor(palette.get("FG_ACCENT", "#E78A4E"))
        self._fg_primary = palette.get("FG_PRIMARY", "#D4BE98")
        self._fg_muted = palette.get("FG_MUTED", "#928374")
        self._fg_accent = palette.get("FG_ACCENT", "#E78A4E")

        badge_color = self._fg_accent if self.eng == "main" else "#A9B665"
        tok_color = "#D8A657" if self.is_selected else "#A9B665"
        fg_sig = (badge_color, self._fg_primary, self._fg_muted, tok_color)
        if self._applied_fg_sig != fg_sig:
            self._applied_fg_sig = fg_sig
            self.badge_lbl.setStyleSheet(f"color: {badge_color}; background: transparent; border: none;")
            self.name_lbl.setStyleSheet(f"color: {self._fg_primary}; background: transparent; border: none;")
            self.meta_left.setStyleSheet(f"color: {self._fg_muted}; background: transparent; border: none;")
            self.time_lbl.setStyleSheet(f"color: {self._fg_muted}; background: transparent; border: none;")
            self.tok_lbl.setStyleSheet(f"color: {tok_color}; background: transparent; border: none;")
        self.update()

    def _refresh_row_layout_and_elide(self, hint_row_w: int = 0) -> None:
        """
        严格为右侧【总使用量 (tok_lbl)】、【日期时间 (time_lbl)】与左侧【主/从徽章 (badge_lbl)】
        预留完整像素宽度（setFixedWidth），无论左侧子菜单如何伸缩，仅压缩会话名 (name_lbl)。
        """
        badge_fm = QFontMetrics(self.badge_lbl.font())
        badge_w = badge_fm.horizontalAdvance(self.badge_lbl.text()) + 2
        if self.badge_lbl.width() != badge_w:
            self.badge_lbl.setFixedWidth(badge_w)

        tok_text = self._raw_tok or "0"
        if tok_text != self._last_tok:
            self._last_tok = tok_text
            self.tok_lbl.setText(tok_text)
        tok_fm = QFontMetrics(self.tok_lbl.font())
        tok_w = tok_fm.horizontalAdvance(tok_text) + 4
        if self.tok_lbl.width() != tok_w:
            self.tok_lbl.setFixedWidth(tok_w)

        time_text = self._raw_time or ""
        if time_text != self._last_time:
            self._last_time = time_text
            self.time_lbl.setText(time_text)
        time_fm = QFontMetrics(self.time_lbl.font())
        time_w = time_fm.horizontalAdvance(time_text) + 4
        if self.time_lbl.width() != time_w:
            self.time_lbl.setFixedWidth(time_w)

        if hint_row_w > 60:
            eff_w = int(hint_row_w)
        elif self.isVisible() and self.width() > 60:
            eff_w = int(self.width())
        else:
            eff_w = max(80, self._sidebar_w - 12)

        margins = self._root_lay.contentsMargins()
        inner_w = max(30, eff_w - margins.left() - margins.right())

        clean_name = " ".join(self._raw_name.split()) if self._raw_name else ""
        top_spacing = self._top_lay.spacing() * 2
        avail_name_px = max(12, inner_w - badge_w - tok_w - top_spacing - 2)
        name_fm = QFontMetrics(self.name_lbl.font())
        elided_name = name_fm.elidedText(clean_name, Qt.TextElideMode.ElideRight, avail_name_px)
        if elided_name != self._last_name:
            self._last_name = elided_name
            self.name_lbl.setText(elided_name)

        bot_spacing = self._bot_lay.spacing()
        avail_meta_px = max(12, inner_w - time_w - bot_spacing - 2)
        meta_fm = QFontMetrics(self.meta_left.font())
        elided_meta = meta_fm.elidedText(self._raw_meta, Qt.TextElideMode.ElideRight, avail_meta_px)
        if elided_meta != self._last_meta:
            self._last_meta = elided_meta
            self.meta_left.setText(elided_meta)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if self.width() > 60:
            self._refresh_row_layout_and_elide(self.width())

    def apply_scale(self, scale: float, sidebar_w: int) -> None:
        self._sidebar_w = int(sidebar_w)
        scale_key = (int(round(scale * 100)), int(sidebar_w))
        if self._applied_scale_sig == scale_key:
            return
        self._applied_scale_sig = scale_key

        row_h = max(34, min(72, int(round(44 * scale))))
        self.setFixedHeight(row_h)
        pad_x = max(5, min(14, int(round(7 * scale))))
        pad_y = max(2, min(8, int(round(4 * scale))))
        self._root_lay.setContentsMargins(pad_x, pad_y, pad_x, pad_y)

        badge_pt = max(6, min(13, int(round(7 * scale))))
        name_pt = max(7, min(14, int(round(8 * scale))))
        meta_pt = max(6, min(12, int(round(7 * scale))))

        self.badge_lbl.setFont(QFont("Microsoft YaHei UI", badge_pt, QFont.Weight.Bold))
        self._font_normal = QFont("Microsoft YaHei UI", name_pt, QFont.Weight.Normal)
        self._font_bold = QFont("Microsoft YaHei UI", name_pt, QFont.Weight.Bold)
        self.name_lbl.setFont(self._font_bold if self.is_selected else self._font_normal)
        self.tok_lbl.setFont(QFont("Consolas", name_pt, QFont.Weight.Bold))

        m_font = QFont("Consolas", meta_pt)
        self.meta_left.setFont(m_font)
        self.time_lbl.setFont(m_font)

        self._refresh_row_layout_and_elide(max(60, sidebar_w - 12))

    def update_session_data(self, sess: dict[str, Any], is_selected: bool) -> None:
        if self.eng == "sub":
            sub_name = str(sess.get("sub_agent_name") or sess.get("agent_name") or "").strip()
            badge_txt = f"[{sub_name}]" if sub_name else "[从]"
            if self.badge_lbl.text() != badge_txt:
                self.badge_lbl.setText(badge_txt)
        raw_name = str(sess.get("session_name") or sess.get("cid") or sess.get("session_id") or "未命名")
        self._raw_name = raw_name
        total_tok_val = int(sess.get("total_tokens") or 0)
        self._raw_tok = format_compact_tokens(total_tok_val)

        raw_id = str(sess.get("cid") if self.eng == "main" else sess.get("session_id") or "")
        short_id = raw_id[:8] if raw_id else "-"
        steps_num = int(sess.get("steps") or 0)
        sess_cost_usd = float(
            sess.get("total_cost_usd", sess.get("cost_usd", sess.get("today_cost_usd", 0.0))) or 0.0
        )
        today_cost_usd = float(sess.get("today_cost_usd", sess_cost_usd) or 0.0)
        cost_str = format_cost_usd(sess_cost_usd)
        today_cost_str = format_cost_usd(today_cost_usd)
        self._raw_meta = f"#{short_id} · {steps_num}轮 · ≈ {cost_str}"
        self._raw_time = format_short_time(float(sess.get("last_updated") or 0.0))

        model_str = str(sess.get("model") or "-")
        self.setToolTip(
            f"会话: {raw_name}\n"
            f"模型: {model_str}\n"
            f"总 Token: {format_num(total_tok_val)} ({steps_num} 轮)\n"
            f"累计折算费用: ≈ {cost_str} (今日: ≈ {today_cost_str})"
        )

        self.set_selected(is_selected)
        self._refresh_row_layout_and_elide()

    def _on_hover_alpha_update(self, val: float) -> None:
        self._hover_alpha = max(0.0, min(1.0, float(val)))
        self.update()

    def _on_select_alpha_update(self, val: float) -> None:
        self._select_alpha = max(0.0, min(1.0, float(val)))
        self.update()

    def set_selected(self, is_selected: bool) -> None:
        self.is_selected = is_selected
        if self._last_selected != is_selected:
            self._last_selected = is_selected
            self.name_lbl.setFont(self._font_bold if is_selected else self._font_normal)
            tok_color = "#D8A657" if is_selected else "#A9B665"
            self.tok_lbl.setStyleSheet(f"color: {tok_color}; background: transparent; border: none;")
            self._refresh_row_layout_and_elide()
            target_alpha = 1.0 if is_selected else 0.0
            get_global_animator().start_tween(
                key=f"sb_select_{id(self)}",
                start_val=self._select_alpha,
                end_val=target_alpha,
                duration_ms=180,
                easing_fn=out_quart,
                on_update=self._on_select_alpha_update,
            )
            self.update()

    def enterEvent(self, event: Any) -> None:
        self.is_hovered = True
        get_global_animator().start_tween(
            key=f"sb_hover_{id(self)}",
            start_val=self._hover_alpha,
            end_val=1.0,
            duration_ms=120,
            easing_fn=out_quart,
            on_update=self._on_hover_alpha_update,
        )
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self.is_hovered = False
        get_global_animator().start_tween(
            key=f"sb_hover_{id(self)}",
            start_val=self._hover_alpha,
            end_val=0.0,
            duration_ms=150,
            easing_fn=out_quart,
            on_update=self._on_hover_alpha_update,
        )
        super().leaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.row_clicked.emit(self.eng, self.file_path)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)

        # 底色平滑插值：inner -> hover -> selected
        bg = lerp_color(self._bg_inner, self._bg_hover, self._hover_alpha)
        if self._select_alpha > 0.001:
            bg = lerp_color(bg, self._bg_selected, self._select_alpha)

        border = lerp_color(self._border_normal, self._border_selected, self._select_alpha)

        painter.fillRect(rect, bg)
        painter.setPen(QPen(border, 1))
        painter.drawRect(rect)

        # 左侧 3px 选中指示胶囊条
        if self._select_alpha > 0.01:
            cap_color = QColor(self._border_selected)
            cap_color.setAlpha(int(round(self._select_alpha * 255)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(cap_color)
            cap_rect = QRectF(rect.x() + 1.0, rect.y() + 4.0, 3.0, max(2.0, rect.height() - 8.0))
            painter.drawRoundedRect(cap_rect, 1.5, 1.5)


class _CompatLabelAdapter:
    """兼容旧自检脚本的 `.cget('text')` 与 `.configure(...)` 访问接口。"""

    def __init__(self, qlabel: QLabel):
        self._qlabel = qlabel

    def cget(self, key: str) -> str:
        if key == "text":
            return self._qlabel.text()
        return ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._qlabel, name)


class _CompatWidgetGeoAdapter:
    """兼容自检脚本对 `winfo_*` 几何查询方法的调用。"""

    def __init__(self, qwidget: QWidget):
        self._w = qwidget

    def update_idletasks(self) -> None:
        get_global_animator().stop_all(snap_to_target=True)
        QApplication.processEvents()

    def winfo_reqwidth(self) -> int:
        return self._w.width()

    def winfo_reqheight(self) -> int:
        return self._w.height()

    def winfo_width(self) -> int:
        return self._w.width()

    def winfo_height(self) -> int:
        return self._w.height()

    def winfo_x(self) -> int:
        return self._w.x()

    def winfo_y(self) -> int:
        return self._w.y()

    def winfo_exists(self) -> bool:
        return self._w.isVisible()


class IslandPeekPopup(QWidget):
    """
    灵动岛折叠胶囊态下的悬停 Peek 迷你预览浮层（260x54px）。
    纯 QPainter 直绘深色半透明圆角背景与 2x2 核心指标（输入、缓存、输出、思考），
    由 AnimationCoordinator 驱动 180ms 垂直微滑入 + 淡入、150ms 淡出收回。
    """

    POPUP_W = 260
    POPUP_H = 54
    GAP_PX = 4
    SLIDE_OFFSET_PX = 4.0

    def __init__(self, hud_window: "TokenHUDWindow", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._hud = hud_window
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(self.POPUP_W, self.POPUP_H)

        self.is_hovered: bool = False
        self._progress: float = 0.0
        self._is_showing: bool = False
        self._anchor_x: int = 0
        self._anchor_y: int = 0
        self._expand_upward: bool = False

        # 指标数据缓存
        self._in_tokens: int = 0
        self._cache_tokens: int = 0
        self._cache_rate: float = 0.0
        self._out_tokens: int = 0
        self._thought_tokens: int = 0
        self._is_pinned_mode: bool = False

        # 主题颜色缓存
        self._bg_color = QColor("#141617")
        self._bg_color.setAlpha(238)
        self._cell_bg_color = QColor("#1D2021")
        self._cell_bg_color.setAlpha(180)
        self._border_color = QColor("#282828")
        self._fg_muted = QColor("#928374")
        self._fg_in = QColor("#D4BE98")
        self._fg_cache = QColor("#D3869B")
        self._fg_out = QColor("#D8A657")
        self._fg_thought = QColor("#E78A4E")

        self._font_label = QFont("Microsoft YaHei UI", 7)
        self._font_val = QFont("Consolas", 8, QFont.Weight.Bold)

    @property
    def progress(self) -> float:
        return self._progress

    def apply_theme(
        self,
        bg_main: str,
        bg_card: str,
        border_color: str,
        fg_muted: str,
        fg_primary: str,
        fg_purple: str,
        fg_yellow: str,
        fg_accent: str,
    ) -> None:
        self._bg_color = QColor(bg_main)
        self._bg_color.setAlpha(240)
        self._cell_bg_color = QColor(bg_card)
        self._cell_bg_color.setAlpha(175)
        self._border_color = QColor(border_color)
        self._fg_muted = QColor(fg_muted)
        self._fg_in = QColor(fg_primary)
        self._fg_cache = QColor(fg_purple)
        self._fg_out = QColor(fg_yellow)
        self._fg_thought = QColor(fg_accent)
        if self.isVisible():
            self.update()

    def update_metrics(
        self,
        in_tokens: int,
        cache_tokens: int,
        out_tokens: int,
        thought_tokens: int,
        is_pinned: bool = False,
    ) -> None:
        in_v = max(0, int(in_tokens))
        cache_v = max(0, int(cache_tokens))
        out_v = max(0, int(out_tokens))
        thought_v = max(0, int(thought_tokens))
        rate_v = (cache_v * 100.0 / in_v) if in_v > 0 else 0.0

        changed = (
            self._in_tokens != in_v
            or self._cache_tokens != cache_v
            or self._out_tokens != out_v
            or self._thought_tokens != thought_v
            or self._is_pinned_mode != bool(is_pinned)
        )
        self._in_tokens = in_v
        self._cache_tokens = cache_v
        self._cache_rate = rate_v
        self._out_tokens = out_v
        self._thought_tokens = thought_v
        self._is_pinned_mode = bool(is_pinned)
        if changed and self.isVisible():
            self.update()

    def compute_anchor_position(self) -> tuple[int, int, bool]:
        """计算相对于胶囊主窗口的目标锚点坐标 (x, y, expand_upward)。"""
        hud = self._hud
        pill_x = int(hud.x())
        pill_y = int(hud.y())
        pill_w = int(hud.width())
        pill_h = int(hud.height())

        target_x = pill_x + (pill_w - self.POPUP_W) // 2
        below_y = pill_y + pill_h + self.GAP_PX
        above_y = pill_y - self.POPUP_H - self.GAP_PX

        expand_upward = False
        try:
            app = QApplication.instance()
            screen = (
                (hud.screen() if hasattr(hud, "screen") else None)
                or (app.screenAt(hud.geometry().center()) if app and hasattr(app, "screenAt") else None)
                or (app.primaryScreen() if app and hasattr(app, "primaryScreen") else None)
            )
            if screen is not None:
                avail = screen.availableGeometry()
                if below_y + self.POPUP_H > avail.bottom() - 4 and above_y >= avail.top() + 4:
                    expand_upward = True
                target_x = max(avail.left() + 4, min(target_x, avail.right() - self.POPUP_W - 4))
        except Exception:
            pass

        target_y = above_y if expand_upward else below_y
        return target_x, target_y, expand_upward

    def sync_position(self) -> None:
        """同步跟随主窗口位置并应用当前动效垂直偏移。"""
        self._anchor_x, self._anchor_y, self._expand_upward = self.compute_anchor_position()
        self._apply_animated_geometry()

    def _apply_animated_geometry(self) -> None:
        # 展开时从偏移 4px 滑入落位：下展时从 -4px 下落到 0；上展时从 +4px 上浮到 0
        offset_dir = 1.0 if self._expand_upward else -1.0
        dy = int(round((1.0 - self._progress) * self.SLIDE_OFFSET_PX * offset_dir))
        self.move(self._anchor_x, self._anchor_y + dy)

    def _on_anim_update(self, val: float) -> None:
        self._progress = max(0.0, min(1.0, float(val)))
        self._apply_animated_geometry()
        self.update()

    def show_peek(self, instant: bool = False) -> None:
        """触发 180ms 垂直微滑入 + 淡入展示。"""
        self._is_showing = True
        self._anchor_x, self._anchor_y, self._expand_upward = self.compute_anchor_position()
        animator = get_global_animator()
        if not self.isVisible():
            self._progress = 1.0 if (instant or animator.is_paused) else 0.0
            self._apply_animated_geometry()
            self.show()

        if instant or animator.is_paused:
            animator.stop_tween(f"island_peek_{id(self)}")
            self._on_anim_update(1.0)
            return

        animator.start_tween(
            key=f"island_peek_{id(self)}",
            start_val=self._progress,
            end_val=1.0,
            duration_ms=180,
            easing_fn=out_quart,
            on_update=self._on_anim_update,
        )

    def hide_peek(self, instant: bool = False) -> None:
        """触发 150ms 淡出收回，或立即隐藏。"""
        self._is_showing = False
        animator = get_global_animator()
        tween_key = f"island_peek_{id(self)}"
        if instant or animator.is_paused or not self.isVisible():
            animator.stop_tween(tween_key)
            self._progress = 0.0
            self.is_hovered = False
            if self.isVisible():
                self.hide()
            return

        def _on_hide_done() -> None:
            if not self._is_showing:
                self._progress = 0.0
                self.is_hovered = False
                self.hide()

        animator.start_tween(
            key=tween_key,
            start_val=self._progress,
            end_val=0.0,
            duration_ms=150,
            easing_fn=out_quart,
            on_update=self._on_anim_update,
            on_finished=_on_hide_done,
        )

    def enterEvent(self, event: Any) -> None:
        self.is_hovered = True
        if hasattr(self._hud, "_on_peek_popup_enter"):
            self._hud._on_peek_popup_enter()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self.is_hovered = False
        if hasattr(self._hud, "_on_peek_popup_leave"):
            self._hud._on_peek_popup_leave()
        super().leaveEvent(event)

    def paintEvent(self, event: Any) -> None:
        if self._progress <= 0.005:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(max(0.0, min(1.0, self._progress * float(getattr(self._hud, "alpha", 0.94)))))

        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        radius = 9.0

        # 外框深色半透明底色与微透边框
        painter.setBrush(self._bg_color)
        painter.setPen(QPen(self._border_color, 1.1))
        painter.drawRoundedRect(rect, radius, radius)

        # 内部 2x2 紧凑单元格布局
        pad_x = 7.0
        pad_y = 5.0
        gap_x = 6.0
        gap_y = 4.0
        inner_w = rect.width() - pad_x * 2.0
        inner_h = rect.height() - pad_y * 2.0
        cell_w = (inner_w - gap_x) * 0.5
        cell_h = (inner_h - gap_y) * 0.5

        in_str = format_compact_tokens(self._in_tokens)
        cache_pct = int(round(self._cache_rate))
        cache_str = f"{format_compact_tokens(self._cache_tokens)} ({cache_pct}%)"
        out_str = format_compact_tokens(self._out_tokens)
        thought_str = format_compact_tokens(self._thought_tokens)

        cells = [
            (0, 0, "输入", in_str, self._fg_in),
            (0, 1, "缓存", cache_str, self._fg_cache),
            (1, 0, "输出", out_str, self._fg_out),
            (1, 1, "思考", thought_str, self._fg_thought),
        ]

        for r_idx, c_idx, label_txt, val_txt, val_col in cells:
            cx = rect.left() + pad_x + c_idx * (cell_w + gap_x)
            cy = rect.top() + pad_y + r_idx * (cell_h + gap_y)
            c_rect = QRectF(cx, cy, cell_w, cell_h)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._cell_bg_color)
            painter.drawRoundedRect(c_rect, 4.5, 4.5)

            text_rect = c_rect.adjusted(6.0, 0.0, -6.0, 0.0)
            painter.setFont(self._font_label)
            painter.setPen(self._fg_muted)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label_txt,
            )

            painter.setFont(self._font_val)
            painter.setPen(val_col)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                val_txt,
            )

