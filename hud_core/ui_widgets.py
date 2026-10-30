# -*- coding: utf-8 -*-
"""
hud_core.ui_widgets - 格式化工具、调色板派生、ResizeGripWidget、SplitterHandleWidget、SidebarRowWidget。
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

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
        self._press_pos = QPoint()
        self._press_g_pos = QPoint()
        self.setFixedWidth(14)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._sync_tooltip()

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
        if not is_collapsed:
            p1 = QPoint(cx + 2, cy - 5)
            p2 = QPoint(cx - 2, cy)
            p3 = QPoint(cx + 2, cy + 5)
        else:
            p1 = QPoint(cx - 2, cy - 5)
            p2 = QPoint(cx + 2, cy)
            p3 = QPoint(cx - 2, cy + 5)

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

    def set_selected(self, is_selected: bool) -> None:
        self.is_selected = is_selected
        if self._last_selected != is_selected:
            self._last_selected = is_selected
            self.name_lbl.setFont(self._font_bold if is_selected else self._font_normal)
            tok_color = "#D8A657" if is_selected else "#A9B665"
            self.tok_lbl.setStyleSheet(f"color: {tok_color}; background: transparent; border: none;")
            self._refresh_row_layout_and_elide()
            self.update()

    def enterEvent(self, event: Any) -> None:
        self.is_hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self.is_hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.row_clicked.emit(self.eng, self.file_path)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect().adjusted(0, 0, -1, -1)

        if self.is_hovered:
            bg = self._bg_hover
        elif self.is_selected:
            bg = self._bg_selected
        else:
            bg = self._bg_inner

        border = self._border_selected if self.is_selected else self._border_normal
        painter.fillRect(rect, bg)
        painter.setPen(QPen(border, 1))
        painter.drawRect(rect)


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
