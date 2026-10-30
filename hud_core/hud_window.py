# -*- coding: utf-8 -*-
"""
hud_core.hud_window - TokenHUDWindow 主悬浮窗视图、布局自适应、异步快照刷新与交互控制。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime
import math
from pathlib import Path
import sys
import threading
import time
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .config import ConfigManager
from .monitor import TokenMonitorCache, export_history_to_csv
from .parsers import PARSER_REGISTRY, _truncate_session_name, _update_session_pricing
from .pricing import (
    DEFAULT_USD_TO_CNY_RATE,
    TokenPricingEngine,
    fetch_online_usd_rate,
    fetch_remote_pricing_table,
    format_cost_cny,
    format_cost_usd,
    merge_remote_pricing_into_config,
)
from .settings_dialog import SettingsDialog


class _StartupPricingBridge(QObject):
    """用于将启动时后台线程静默同步的汇率与价格表结果安全投递回主线程。"""

    sync_finished = Signal(bool)
from .ui_widgets import (
    ResizeGripWidget,
    SidebarRowWidget,
    SplitterHandleWidget,
    _CompatLabelAdapter,
    _CompatWidgetGeoAdapter,
    _HoverLabel,
    _SnapshotBridge,
    _derive_theme_palette,
    format_compact_tokens,
    format_duration,
    format_num,
    format_short_time,
)
from .win32_dwm import apply_win11_mica_and_rounded_corners, resolve_top_level_hwnd


class _SidebarFrame(QFrame):
    """使用 QPainter 直接绘制侧边栏圆角背景与边框，避免对父容器调用 setStyleSheet 引发子行递归重抛光。"""

    def __init__(self, hud_window: "TokenHUDWindow", parent: QWidget | None = None):
        super().__init__(parent)
        self._hud = hud_window

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bg_color = getattr(self._hud, "_bg_card_qcolor", QColor(29, 32, 33, 166))
        border_color = getattr(self._hud, "_border_qcolor", QColor("#282828"))
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setBrush(bg_color)
        painter.setPen(QPen(border_color, 1))
        painter.drawRoundedRect(rect, 4, 4)


class TokenHUDWindow(QWidget):
    """
    基于 PySide6 (Qt 6) + Windows 11 DWM (圆角 + Mica) 的单窗口常驻双栏 Token 监控悬浮窗。
    """

    BG_MAIN = "#141617"
    BG_CARD = "#1D2021"
    BG_INNER = "#181A1B"
    BG_HEADER = "#141617"
    FG_PRIMARY = "#D4BE98"
    FG_MUTED = "#928374"
    FG_ACCENT = "#E78A4E"
    FG_GREEN = "#A9B665"
    FG_YELLOW = "#D8A657"
    FG_PURPLE = "#D3869B"
    FG_RED = "#EA6962"
    BORDER_COLOR = "#282828"
    BG_HOVER = "#262A2C"
    BG_SELECTED = "#32302F"

    CARD_TITLES = [
        "[Token 总用量]",
    ]

    MIN_CARD_W = 450
    MAX_CARD_W = 555
    SIDEBAR_W = 265
    MIN_WIN_W = 740
    MAX_WIN_W = 840
    MIN_WIN_H = 330
    MAX_WIN_H = 460
    RESIZE_MIN_W = 540
    RESIZE_MIN_H = 240
    IDLE_BACKOFF_THRESHOLD_SEC = 30.0
    STANDBY_REFRESH_MS = 3000

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    VK_T = 0x54
    HOTKEY_ID = 0x9001
    WM_HOTKEY = 0x0312
    TRAY_MENU_LABELS = [
        "显示 / 隐藏悬浮窗",
        "立即重新扫描",
        "全量重新扫描 (完整历史)",
        "导出历史为 CSV...",
        "设置...",
        "退出",
    ]

    def __init__(
        self,
        monitor: TokenMonitorCache | None = None,
        config_mgr: ConfigManager | None = None,
        refresh_ms: int = 1000,
        target_session: str | None = None,
        target_project: str | None = None,
        auto_close_idle_sec: float = 0.0,
        alpha: float = 0.94,
    ):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setQuitOnLastWindowClosed(False)
        super().__init__(None)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        if config_mgr is not None:
            self.config_mgr = config_mgr
        elif monitor is not None and hasattr(monitor, "config_mgr") and monitor.config_mgr is not None:
            self.config_mgr = monitor.config_mgr
        else:
            self.config_mgr = ConfigManager()

        self.monitor = monitor if monitor is not None else TokenMonitorCache(config_mgr=self.config_mgr)
        self.monitor.sync_pricing_from_config()
        self.pricing_engine: TokenPricingEngine = self.monitor.pricing_engine
        cfg_poll_ms = int(self.config_mgr.get("poll_interval_ms", refresh_ms) or refresh_ms)
        self.refresh_ms = max(250, refresh_ms if refresh_ms != 1000 else cfg_poll_ms)
        self.target_session = target_session
        self.target_project = target_project
        self.auto_close_idle_sec = auto_close_idle_sec
        theme_init_cfg = self.config_mgr.get("theme", {})
        self.alpha = 0.94
        self.bg_opacity = 0.65
        self.backdrop_type = (
            str(theme_init_cfg.get("backdrop") or "mica").strip().lower()
            if isinstance(theme_init_cfg, dict)
            else "mica"
        )
        if self.backdrop_type not in ("mica", "acrylic", "mica_alt", "none"):
            self.backdrop_type = "mica"
        self._settings_dialog: SettingsDialog | None = None

        self.current_card_idx = 0
        self._in_mainloop = False
        self.sidebar_filter = "all"  # "all" | "main" | "sub"
        self.pinned_main_file: str | None = None
        self.pinned_sub_file: str | None = None
        self._last_main_sessions: list[dict[str, Any]] = []
        self._last_sub_sessions: list[dict[str, Any]] = []
        self._last_sessions_by_agent: dict[str, list[dict[str, Any]]] = {}
        self._last_today_by_agent: dict[str, dict[str, Any]] = {}
        self._grand_total_compact_text = "今日累计: 0"
        self._grand_total_hover_text = "今日累计总 Token: 0"
        self._is_hovering_grand_total = False

        self._sidebar_order_sig: Any = None
        self._sidebar_rows: dict[tuple[str, str], SidebarRowWidget] = {}
        self._active_row_keys: list[tuple[str, str]] = []

        self._drag_x = 0
        self._drag_y = 0
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._drag_moved = False
        self._drag_in_header = False
        self._is_dragging = False
        self._user_moved_window = False
        self._user_resized_window = False
        self._is_resizing = False
        self._resize_start_x = 0
        self._resize_start_y = 0
        self._resize_orig_w = self.MIN_WIN_W
        self._resize_orig_h = self.MIN_WIN_H
        self._sidebar_collapsed: bool = False
        self._saved_sidebar_ratio: float = 0.28
        self._sidebar_ratio = 0.355
        self._is_splitting = False
        self._split_start_x = 0
        self._split_orig_sidebar_w = self.SIDEBAR_W
        self._current_scale = 1.0
        self._current_sidebar_w = self.SIDEBAR_W
        self._top_hwnd = 0
        self._seen_running = False
        self._idle_since: float | None = None
        self._worker_busy = False
        self.last_activity_time: float = float(
            getattr(self.monitor, "last_activity_time", time.time())
        )
        self.idle_backoff_sec: float = self.IDLE_BACKOFF_THRESHOLD_SEC
        self.standby_refresh_ms: int = max(self.STANDBY_REFRESH_MS, self.refresh_ms)
        self._is_polling_backoff: bool = False
        self._render_paused: bool = False
        self._pending_render_snap: dict[str, Any] | None = None
        self._render_count: int = 0
        self.dwm_info: dict[str, Any] = {}

        # 初始化时同步一次主题调色板
        self._sync_palette_from_config()

        # 无边框 + 置顶 + 工具窗口属性 + 逐像素 Alpha 透明背景（配合 DWM Mica/Acrylic 毛玻璃）
        self.setWindowTitle("Antigravity & Argon Token HUD")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowOpacity(self.alpha)
        self.setMinimumSize(self.RESIZE_MIN_W, self.RESIZE_MIN_H)
        self.setStyleSheet("QLabel { background: transparent; border: none; }")

        # 初始定位在屏幕右上角并严格锁定初始宽高
        screen = self._app.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        win_w, win_h = self.MIN_WIN_W, self.MIN_WIN_H
        pos_x = max(20, avail.right() - win_w - 28)
        pos_y = max(20, avail.top() + 36)
        self._locked_win_w = win_w
        self._locked_win_h = win_h
        self.setGeometry(pos_x, pos_y, win_w, win_h)

        self._build_ui()
        self.apply_theme()

        # 跨线程后台数据更新桥
        self._bridge = _SnapshotBridge(self)
        self._bridge.snapshot_ready.connect(self._apply_snapshot)
        self._startup_pricing_bridge = _StartupPricingBridge(self)
        self._startup_pricing_bridge.sync_finished.connect(self._on_startup_pricing_sync_finished)

        self._timer = QTimer(self)
        self._timer.setInterval(self.refresh_ms)
        self._timer.timeout.connect(self._schedule_tick)

        # 显示并应用 Windows 11 DWM 圆角与材质特效
        self.show()
        QApplication.processEvents()
        self.dwm_info = apply_win11_mica_and_rounded_corners(
            self, backdrop_type=self.backdrop_type
        )
        self._top_hwnd = resolve_top_level_hwnd(self) or int(self.dwm_info.get("hwnd") or 0)

        self._hotkey_registered: bool = False
        self._hotkey_hwnd: int = 0
        self.tray_icon: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None
        self.tray_actions: dict[str, QAction] = {}
        self._init_system_tray()
        self._register_global_hotkey()

        # 兼容属性封装（供自动化自检脚本无缝读取）
        self.root = _CompatWidgetGeoAdapter(self)
        self.title_lbl = _CompatLabelAdapter(self._title_qlabel)
        self.grand_total_lbl = _CompatLabelAdapter(self._grand_total_qlabel)
        self.sidebar_frame = _CompatWidgetGeoAdapter(self._sidebar_qframe)
        self.deck = _CompatWidgetGeoAdapter(self._deck_qwidget)

        QTimer.singleShot(1500, self._async_startup_pricing_update)

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        outer_layout.setContentsMargins(1, 1, 1, 1)
        outer_layout.setSpacing(0)

        # ------------------------------------------------------------------
        # 1. 顶部状态栏（高度 34px，随窗口自适应缩放）
        # ------------------------------------------------------------------
        self.header = QFrame(self)
        self.header.setFixedHeight(34)
        self.header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.header.setMinimumWidth(0)
        self.header.setStyleSheet(f"background-color: {self.BG_HEADER_CSS}; border: none;")
        self.header_lay = QHBoxLayout(self.header)
        self.header_lay.setContentsMargins(10, 0, 6, 0)
        self.header_lay.setSpacing(6)

        self.status_dot = QLabel("●", self.header)
        self.status_dot.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.status_dot.setStyleSheet(f"color: {self.FG_GREEN}; background: transparent; border: none;")
        self.status_dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.header_lay.addWidget(self.status_dot, 0)

        self._title_qlabel = QLabel(self.CARD_TITLES[0], self.header)
        self._title_qlabel.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Bold))
        self._title_qlabel.setStyleSheet(f"color: {self.FG_PRIMARY}; background: transparent; border: none;")
        self._title_qlabel.setMinimumWidth(0)
        self._title_qlabel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.header_lay.addWidget(self._title_qlabel, 0)

        self.header_lay.addStretch(1)

        self.reset_active_btn = _HoverLabel("[返回总用量]", self.header)
        self.reset_active_btn.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        self.reset_active_btn.setStyleSheet(
            f"color: {self.FG_GREEN}; background-color: {self.BG_INNER_CSS}; border: none; padding: 2px 6px; border-radius: 3px;"
        )
        self.reset_active_btn.clicked.connect(self._reset_pinned_session)
        self.reset_active_btn.hide()
        self.header_lay.addWidget(self.reset_active_btn, 0)

        self.switch_hint_lbl = QLabel("", self.header)
        self.switch_hint_lbl.setFont(QFont("Microsoft YaHei UI", 8))
        self.switch_hint_lbl.setStyleSheet(f"color: {self.FG_ACCENT}; background: transparent; border: none; padding: 0 4px;")
        self.switch_hint_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.switch_hint_lbl.hide()
        self.header_lay.addWidget(self.switch_hint_lbl, 0)

        self._grand_total_qlabel = _HoverLabel(self._grand_total_compact_text, self.header)
        self._grand_total_qlabel.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        self._grand_total_qlabel.setMinimumWidth(0)
        self._grand_total_qlabel.setStyleSheet(
            f"color: {self.FG_YELLOW}; background: transparent; border: none; padding: 2px 6px;"
        )
        self._grand_total_qlabel.hovered.connect(self._on_grand_total_hovered)
        self._grand_total_qlabel.clicked.connect(self._reset_pinned_session)
        self.header_lay.addWidget(self._grand_total_qlabel, 0)

        self.settings_btn = _HoverLabel("⚙", self.header)
        self.settings_btn.setFont(QFont("Segoe UI Symbol", 9, QFont.Weight.Bold))
        self.settings_btn.setToolTip("打开设置面板")
        self.settings_btn.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none; padding: 2px 6px;")
        self.settings_btn.hovered.connect(
            lambda h: self.settings_btn.setStyleSheet(
                f"color: {self.FG_ACCENT if h else self.FG_MUTED}; background: transparent; border: none; padding: 2px 6px;"
            )
        )
        self.settings_btn.clicked.connect(self.open_settings)
        self.header_lay.addWidget(self.settings_btn, 0)

        self.close_btn = _HoverLabel("✕", self.header)
        self.close_btn.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.close_btn.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none; padding: 2px 8px;")
        self.close_btn.hovered.connect(
            lambda h: self.close_btn.setStyleSheet(
                f"color: {self.FG_RED if h else self.FG_MUTED}; background: transparent; border: none; padding: 2px 8px;"
            )
        )
        self.close_btn.clicked.connect(self.close)
        self.header_lay.addWidget(self.close_btn, 0)

        outer_layout.addWidget(self.header, 0)

        # ------------------------------------------------------------------
        # 2. 主体常驻左右双栏容器（中间含可拖拽横向分隔手柄）
        # ------------------------------------------------------------------
        self.body_frame = QFrame(self)
        self.body_frame.setStyleSheet("background: transparent; border: none;")
        self.body_lay = QHBoxLayout(self.body_frame)
        self.body_lay.setContentsMargins(8, 6, 8, 8)
        self.body_lay.setSpacing(0)

        self._build_sidebar()
        self.body_lay.addWidget(self._sidebar_qframe, 0)

        self.splitter_handle = SplitterHandleWidget(self, self.body_frame)
        self.body_lay.addWidget(self.splitter_handle, 0)

        self._deck_qwidget = QStackedWidget(self.body_frame)
        self._deck_qwidget.setStyleSheet("background: transparent; border: none;")

        self.card_views: list[dict[str, Any]] = [
            self._build_summary_card(),
        ]
        for v in self.card_views:
            self._deck_qwidget.addWidget(v["frame"])

        self.body_lay.addWidget(self._deck_qwidget, 1)
        outer_layout.addWidget(self.body_frame, 1)

        # ------------------------------------------------------------------
        # 3. 右下角自定义缩放手柄（置于最顶层）
        # ------------------------------------------------------------------
        self.resize_grip = ResizeGripWidget(self)
        self.resize_grip.move(self.width() - 17, self.height() - 17)
        self.resize_grip.raise_()

        self._show_card(0)

    def _build_sidebar(self) -> None:
        """构建左侧常驻历史会话列表（无滚动条，纯静默滚轮滑动，单句柄绘制度）。"""
        self._sidebar_qframe = _SidebarFrame(self, self.body_frame)
        self._sidebar_qframe.setFixedWidth(self.SIDEBAR_W)
        self._sidebar_qframe.setObjectName("sidebarRoot")

        self.sb_lay = QVBoxLayout(self._sidebar_qframe)
        self.sb_lay.setContentsMargins(4, 4, 4, 4)
        self.sb_lay.setSpacing(3)

        # 顶部过滤切换栏（专注主 Agent 模式常驻隐藏）
        self.filter_bar = QFrame(self._sidebar_qframe)
        self.filter_bar.setStyleSheet(f"background-color: {self.BG_INNER_CSS}; border: none; border-radius: 3px;")
        fb_lay = QHBoxLayout(self.filter_bar)
        fb_lay.setContentsMargins(3, 3, 3, 3)
        fb_lay.setSpacing(3)

        self.filter_btns: dict[str, _HoverLabel] = {}
        for f_key, f_text in [("all", "[全部]"), ("main", "[主Agent]"), ("sub", "[子Agent]")]:
            btn = _HoverLabel(f_text, self.filter_bar)
            btn.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
            btn.setAlignment(Qt.AlignmentFlag.AlignCenter)
            btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            btn.setMinimumWidth(0)
            btn.setFixedHeight(22)
            btn.clicked.connect(lambda k=f_key: self._set_sidebar_filter(k))
            fb_lay.addWidget(btn, 1)
            self.filter_btns[f_key] = btn

        self._update_filter_btn_styles()
        self.sb_lay.addWidget(self.filter_bar, 0)
        self.filter_bar.hide()

        self.sidebar_count_lbl = QLabel("共 0 条会话 · 点击切换", self._sidebar_qframe)
        self.sidebar_count_lbl.setFont(QFont("Microsoft YaHei UI", 7))
        self.sidebar_count_lbl.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none; padding: 1px 4px;")
        self.sidebar_count_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sidebar_count_lbl.setMinimumWidth(0)
        self.sb_lay.addWidget(self.sidebar_count_lbl, 0)

        # 静默滚动区域（彻底隐藏垂直与水平滚动条）
        self.sidebar_scroll = QScrollArea(self._sidebar_qframe)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setStyleSheet("background: transparent; border: none;")

        self.sidebar_list_inner = QWidget(self.sidebar_scroll)
        self.sidebar_list_inner.setStyleSheet("background: transparent; border: none;")
        self.sidebar_list_inner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sidebar_list_inner.setMinimumWidth(0)
        self.sidebar_list_layout = QVBoxLayout(self.sidebar_list_inner)
        self.sidebar_list_layout.setContentsMargins(1, 1, 1, 1)
        self.sidebar_list_layout.setSpacing(3)
        self.sidebar_list_layout.addStretch(1)

        self.empty_sidebar_lbl = QLabel("暂无匹配的会话记录", self.sidebar_list_inner)
        self.empty_sidebar_lbl.setFont(QFont("Microsoft YaHei UI", 8))
        self.empty_sidebar_lbl.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none; padding: 16px;")
        self.empty_sidebar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_sidebar_lbl.hide()
        self.sidebar_list_layout.insertWidget(0, self.empty_sidebar_lbl)

        self.sidebar_scroll.setWidget(self.sidebar_list_inner)
        self.sb_lay.addWidget(self.sidebar_scroll, 1)

    def _update_filter_btn_styles(self) -> None:
        card_css = getattr(self, "BG_CARD_CSS", self.BG_CARD)
        inner_css = getattr(self, "BG_INNER_CSS", self.BG_INNER)
        for k, btn in self.filter_btns.items():
            if k == self.sidebar_filter:
                btn.setStyleSheet(
                    f"color: {self.FG_ACCENT}; background-color: {card_css}; border: none; border-radius: 2px;"
                )
            else:
                btn.setStyleSheet(
                    f"color: {self.FG_MUTED}; background-color: {inner_css}; border: none; border-radius: 2px;"
                )

    def _build_agent_card(self, engine_label: str, accent_color: str) -> dict[str, Any]:
        """构建主/子 Agent 卡片视图（全高度自适应填充 + 2x2 细分指标大字网格，消除底部留白）。"""
        frame = _SidebarFrame(self, self)
        frame.setObjectName("agentCard")
        frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        frame.setMinimumSize(0, 0)
        lay = QVBoxLayout(frame)
        lay.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # 1. 顶部引擎标题行（固定紧凑占比）
        row_head = QHBoxLayout()
        row_head.setContentsMargins(0, 0, 0, 0)
        engine_title_lbl = QLabel(engine_label, frame)
        engine_title_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        engine_title_lbl.setStyleSheet(f"color: {accent_color}; background: transparent; border: none;")
        engine_title_lbl.setMinimumWidth(0)
        engine_title_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row_head.addWidget(engine_title_lbl, 0)

        row_head.addStretch(1)

        back_active_lbl = _HoverLabel("[返回总用量]", frame)
        back_active_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        back_active_lbl.setStyleSheet(
            f"color: {self.FG_GREEN}; background-color: {self.BG_INNER_CSS}; border: none; padding: 1px 6px; border-radius: 3px;"
        )
        back_active_lbl.clicked.connect(self._reset_pinned_session)
        back_active_lbl.hide()
        row_head.addWidget(back_active_lbl, 0)

        status_lbl = QLabel("[就绪]", frame)
        status_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        status_lbl.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        status_lbl.setMinimumWidth(0)
        status_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row_head.addWidget(status_lbl, 0)
        lay.addLayout(row_head, 0)

        # 2. 会话元信息面板（随高度弹性伸展，内部垂直均匀分布）
        info_frame = QFrame(frame)
        info_frame.setStyleSheet("background: transparent; border: none;")
        info_frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        info_frame.setMinimumWidth(0)
        info_frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay = QVBoxLayout(info_frame)
        info_lay.setContentsMargins(4, 3, 4, 3)
        info_lay.setSpacing(2)

        session_lbl = QLabel("会话: 无", info_frame)
        session_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        session_lbl.setStyleSheet(f"color: {self.FG_PRIMARY}; background: transparent; border: none;")
        session_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        session_lbl.setMinimumWidth(0)
        session_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(session_lbl, 1)

        meta_lbl = QLabel("ID: - | 模型: - | 耗时: 0s", info_frame)
        meta_lbl.setFont(QFont("Consolas", 8))
        meta_lbl.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        meta_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        meta_lbl.setMinimumWidth(0)
        meta_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(meta_lbl, 1)

        step_lbl = QLabel("进度: 0 轮 | 最近工具: -", info_frame)
        step_lbl.setFont(QFont("Microsoft YaHei UI", 8))
        step_lbl.setStyleSheet(f"color: {self.FG_PRIMARY}; background: transparent; border: none;")
        step_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        step_lbl.setMinimumWidth(0)
        step_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(step_lbl, 1)
        lay.addWidget(info_frame, 3)

        # 3. 核心大字指标框（本会话累计 Token + 当前上下文水位）
        tot_frame = QFrame(frame)
        tot_frame.setObjectName("totFrame")
        tot_frame.setStyleSheet("QFrame#totFrame { background: transparent; border: none; }")
        tot_frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        tot_frame.setMinimumWidth(0)
        tot_frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        tot_lay = QHBoxLayout(tot_frame)
        tot_lay.setContentsMargins(4, 4, 4, 4)

        tot_left = QVBoxLayout()
        tot_left.setSpacing(2)
        tot_left.addStretch(1)
        lbl_t1 = QLabel("本会话累计 Token", tot_frame)
        lbl_t1.setFont(QFont("Microsoft YaHei UI", 7))
        lbl_t1.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        lbl_t1.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lbl_t1.setMinimumWidth(0)
        total_val = QLabel("0", tot_frame)
        total_val.setFont(QFont("Consolas", 16, QFont.Weight.Bold))
        total_val.setStyleSheet(f"color: {self.FG_GREEN}; background: transparent; border: none;")
        total_val.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        total_val.setMinimumWidth(0)
        cost_val = QLabel("本会话折合: ≈ $0.00", tot_frame)
        cost_val.setFont(QFont("Microsoft YaHei UI", 7, QFont.Weight.Bold))
        cost_val.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        cost_val.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        cost_val.setMinimumWidth(0)
        tot_left.addWidget(lbl_t1)
        tot_left.addWidget(total_val)
        tot_left.addWidget(cost_val)
        tot_left.addStretch(1)
        tot_lay.addLayout(tot_left, 3)

        tot_right = QVBoxLayout()
        tot_right.setSpacing(2)
        tot_right.addStretch(1)
        lbl_t2 = QLabel("当前上下文水位", tot_frame)
        lbl_t2.setFont(QFont("Microsoft YaHei UI", 7))
        lbl_t2.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        lbl_t2.setAlignment(Qt.AlignmentFlag.AlignRight)
        lbl_t2.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lbl_t2.setMinimumWidth(0)
        ctx_val = QLabel("0", tot_frame)
        ctx_val.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
        ctx_val.setStyleSheet(f"color: {self.FG_ACCENT}; background: transparent; border: none;")
        ctx_val.setAlignment(Qt.AlignmentFlag.AlignRight)
        ctx_val.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        ctx_val.setMinimumWidth(0)
        tot_right.addWidget(lbl_t2)
        tot_right.addWidget(ctx_val)
        tot_right.addStretch(1)
        tot_lay.addLayout(tot_right, 2)
        lay.addWidget(tot_frame, 4)

        # 4. 2x2 双行双列细分指标网格（铺满下半区，消除底部空白，大幅增加单格横向宽度）
        grid_lay = QGridLayout()
        grid_lay.setContentsMargins(0, 0, 0, 0)
        grid_lay.setSpacing(6)
        grid_lay.setColumnStretch(0, 1)
        grid_lay.setColumnStretch(1, 1)
        grid_lay.setRowStretch(0, 1)
        grid_lay.setRowStretch(1, 1)

        grid_cells: list[dict[str, Any]] = []
        lbl_in = self._create_metric_cell(grid_lay, 0, 0, "输入 (Input)", self.FG_PRIMARY, grid_cells)
        lbl_cache = self._create_metric_cell(grid_lay, 0, 1, "缓存 (Cached)", self.FG_PURPLE, grid_cells)
        lbl_out = self._create_metric_cell(grid_lay, 1, 0, "输出 (Output)", self.FG_YELLOW, grid_cells)
        lbl_thought = self._create_metric_cell(grid_lay, 1, 1, "思考 (Thought)", self.FG_ACCENT, grid_cells)
        lay.addLayout(grid_lay, 5)

        return {
            "kind": "agent",
            "frame": frame,
            "info_frame": info_frame,
            "tot_frame": tot_frame,
            "card_lay": lay,
            "info_lay": info_lay,
            "tot_lay": tot_lay,
            "grid_lay": grid_lay,
            "grid_cells": grid_cells,
            "engine_title_lbl": engine_title_lbl,
            "back_active_lbl": back_active_lbl,
            "status_lbl": status_lbl,
            "session_lbl": session_lbl,
            "meta_lbl": meta_lbl,
            "step_lbl": step_lbl,
            "lbl_t1": lbl_t1,
            "lbl_t2": lbl_t2,
            "total_val": total_val,
            "cost_val": cost_val,
            "ctx_val": ctx_val,
            "lbl_in": lbl_in,
            "lbl_cache": lbl_cache,
            "lbl_out": lbl_out,
            "lbl_thought": lbl_thought,
            "_raw_session_name": "无",
            "_raw_short_id": "-",
            "_raw_model": "-",
            "_raw_dur_str": "0s",
            "_raw_steps": 0,
            "_raw_t_calls": 0,
            "_raw_last_tool": "-",
        }

    def _build_summary_card(self) -> dict[str, Any]:
        """构建默认视图【Token 总用量】卡片（全高度自适应填充 + 2x2 细分指标网格）。"""
        frame = _SidebarFrame(self, self)
        frame.setObjectName("summaryCard")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        row_head = QHBoxLayout()
        row_head.setContentsMargins(0, 0, 0, 0)
        t_lbl = QLabel("Token 总用量", frame)
        t_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        t_lbl.setStyleSheet(f"color: {self.FG_ACCENT}; background: transparent; border: none;")
        t_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row_head.addWidget(t_lbl, 0)
        row_head.addStretch(1)

        date_lbl = QLabel(datetime.date.today().strftime("%Y-%m-%d"), frame)
        date_lbl.setFont(QFont("Consolas", 8))
        date_lbl.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        date_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row_head.addWidget(date_lbl, 0)
        lay.addLayout(row_head, 0)

        info_frame = QFrame(frame)
        info_frame.setStyleSheet("background: transparent; border: none;")
        info_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        info_frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay = QVBoxLayout(info_frame)
        info_lay.setContentsMargins(4, 3, 4, 3)
        info_lay.setSpacing(2)

        counts_lbl = QLabel("总会话: 0 次 (主 0 / 从 0) | 总步数: 0 步 (主 0 / 从 0)", info_frame)
        counts_lbl.setFont(QFont("Microsoft YaHei UI", 8))
        counts_lbl.setStyleSheet(f"color: {self.FG_PRIMARY}; background: transparent; border: none;")
        counts_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(counts_lbl, 1)

        split_lbl = QLabel("主 Agent 累计: 0 | 子 Agent 累计: 0", info_frame)
        split_lbl.setFont(QFont("Consolas", 8))
        split_lbl.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        split_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(split_lbl, 1)
        lay.addWidget(info_frame, 3)

        tot_frame = QFrame(frame)
        tot_frame.setObjectName("totFrame")
        tot_frame.setStyleSheet("QFrame#totFrame { background: transparent; border: none; }")
        tot_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        tot_frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        tot_lay = QHBoxLayout(tot_frame)
        tot_lay.setContentsMargins(4, 4, 4, 4)

        tot_left = QVBoxLayout()
        tot_left.setSpacing(2)
        tot_left.addStretch(1)
        lbl_t1 = QLabel("今日累计总 Token", tot_frame)
        lbl_t1.setFont(QFont("Microsoft YaHei UI", 7))
        lbl_t1.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        total_val = QLabel("0", tot_frame)
        total_val.setFont(QFont("Consolas", 16, QFont.Weight.Bold))
        total_val.setStyleSheet(f"color: {self.FG_ACCENT}; background: transparent; border: none;")
        cost_val = QLabel("今日折合: ≈ $0.00", tot_frame)
        cost_val.setFont(QFont("Microsoft YaHei UI", 7, QFont.Weight.Bold))
        cost_val.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        tot_left.addWidget(lbl_t1)
        tot_left.addWidget(total_val)
        tot_left.addWidget(cost_val)
        tot_left.addStretch(1)
        tot_lay.addLayout(tot_left, 3)

        tot_right = QVBoxLayout()
        tot_right.setSpacing(2)
        tot_right.addStretch(1)
        lbl_t2 = QLabel("综合缓存命中率", tot_frame)
        lbl_t2.setFont(QFont("Microsoft YaHei UI", 7))
        lbl_t2.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        lbl_t2.setAlignment(Qt.AlignmentFlag.AlignRight)
        cache_rate_val = QLabel("0.0%", tot_frame)
        cache_rate_val.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
        cache_rate_val.setStyleSheet(f"color: {self.FG_GREEN}; background: transparent; border: none;")
        cache_rate_val.setAlignment(Qt.AlignmentFlag.AlignRight)
        tot_right.addWidget(lbl_t2)
        tot_right.addWidget(cache_rate_val)
        tot_right.addStretch(1)
        tot_lay.addLayout(tot_right, 2)
        lay.addWidget(tot_frame, 4)

        grid_lay = QGridLayout()
        grid_lay.setContentsMargins(0, 0, 0, 0)
        grid_lay.setSpacing(6)
        grid_lay.setColumnStretch(0, 1)
        grid_lay.setColumnStretch(1, 1)
        grid_lay.setRowStretch(0, 1)
        grid_lay.setRowStretch(1, 1)

        grid_cells: list[dict[str, Any]] = []
        lbl_in = self._create_metric_cell(grid_lay, 0, 0, "总输入 (Input)", self.FG_PRIMARY, grid_cells)
        lbl_cache = self._create_metric_cell(grid_lay, 0, 1, "总缓存 (Cached)", self.FG_PURPLE, grid_cells)
        lbl_out = self._create_metric_cell(grid_lay, 1, 0, "总输出 (Output)", self.FG_YELLOW, grid_cells)
        lbl_thought = self._create_metric_cell(grid_lay, 1, 1, "总思考 (Thought)", self.FG_ACCENT, grid_cells)
        lay.addLayout(grid_lay, 5)

        return {
            "kind": "summary",
            "frame": frame,
            "info_frame": info_frame,
            "tot_frame": tot_frame,
            "card_lay": lay,
            "info_lay": info_lay,
            "tot_lay": tot_lay,
            "grid_lay": grid_lay,
            "grid_cells": grid_cells,
            "t_lbl": t_lbl,
            "date_lbl": date_lbl,
            "counts_lbl": counts_lbl,
            "split_lbl": split_lbl,
            "lbl_t1": lbl_t1,
            "lbl_t2": lbl_t2,
            "total_val": total_val,
            "cost_val": cost_val,
            "cache_rate_val": cache_rate_val,
            "lbl_in": lbl_in,
            "lbl_cache": lbl_cache,
            "lbl_out": lbl_out,
            "lbl_thought": lbl_thought,
        }

    def _create_metric_cell(
        self,
        grid: QGridLayout,
        row: int,
        col: int,
        title: str,
        color: str,
        cell_registry: list[dict[str, Any]] | None = None,
    ) -> QLabel:
        cell = QFrame(self)
        cell.setStyleSheet("background: transparent; border: none;")
        cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        cell.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        c_lay = QVBoxLayout(cell)
        c_lay.setContentsMargins(4, 3, 4, 3)
        c_lay.setSpacing(2)
        c_lay.addStretch(1)

        t_lbl = QLabel(title, cell)
        t_lbl.setFont(QFont("Microsoft YaHei UI", 7))
        t_lbl.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
        val_lbl = QLabel("0", cell)
        val_lbl.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        val_lbl.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        c_lay.addWidget(t_lbl)
        c_lay.addWidget(val_lbl)
        c_lay.addStretch(1)
        grid.addWidget(cell, row, col)
        if cell_registry is not None:
            cell_registry.append({"cell": cell, "lay": c_lay, "title_lbl": t_lbl, "val_lbl": val_lbl})
        return val_lbl

    @staticmethod
    def _fit_number_label_font(
        lbl: QLabel,
        family: str,
        target_pt: int,
        min_pt: int,
        max_px_width: int,
        max_px_height: int = 999,
        bold: bool = True,
    ) -> int:
        """
        根据可用像素宽高与当前数字文本长度，动态计算并设置最合适的字号：
        - 当窗口或右侧统计区放大时，按宽高比例同步放大数字；
        - 当数字较长或区域缩小时，自动下调字号防止水平/垂直裁切。
        """
        text = lbl.text() or "0"
        weight = QFont.Weight.Bold if bold else QFont.Weight.Normal
        chosen_pt = max(min_pt, target_pt)
        if max_px_width > 20 and max_px_height > 10:
            for pt in range(chosen_pt, min_pt - 1, -1):
                f = QFont(family, pt, weight)
                fm = QFontMetrics(f)
                if fm.horizontalAdvance(text) <= max_px_width and fm.height() <= max_px_height:
                    chosen_pt = pt
                    break
            else:
                chosen_pt = min_pt
        cur_font = lbl.font()
        if cur_font.pointSize() != chosen_pt or cur_font.family() != family:
            lbl.setFont(QFont(family, chosen_pt, weight))
        return chosen_pt

    def _apply_responsive_scale(self) -> None:
        """根据当前窗口宽高与左右分隔比例，对侧边栏、右侧卡片容器、控件字体及数字大小执行全量自适应缩放。"""
        w = max(self.RESIZE_MIN_W, self.width())
        h = max(self.RESIZE_MIN_H, self.height())

        sx = w / float(self.MIN_WIN_W)
        sy = h / float(self.MIN_WIN_H)
        scale = max(0.74, min(2.4, sx * 0.50 + sy * 0.50))
        self._current_scale = scale

        # 1. 右下角缩放手柄定位
        if hasattr(self, "resize_grip"):
            self.resize_grip.move(
                self.width() - self.resize_grip.width() - 1,
                self.height() - self.resize_grip.height() - 1,
            )
            self.resize_grip.raise_()

        # 2. 顶部标题栏自适应
        header_h = max(28, min(52, int(round(34 * scale))))
        self.header.setFixedHeight(header_h)
        h_pad_x = max(6, min(18, int(round(10 * scale))))
        self.header_lay.setContentsMargins(h_pad_x, 0, max(4, h_pad_x - 3), 0)
        self.header_lay.setSpacing(max(3, min(12, int(round(6 * scale)))))

        dot_pt = max(8, min(16, int(round(10 * scale))))
        title_pt = max(7, min(15, int(round(9 * scale))))
        badge_pt = max(7, min(13, int(round(8 * scale))))

        self.status_dot.setFont(QFont("Segoe UI", dot_pt, QFont.Weight.Bold))
        self._title_qlabel.setFont(QFont("Microsoft YaHei UI", title_pt, QFont.Weight.Bold))
        self._grand_total_qlabel.setFont(QFont("Microsoft YaHei UI", badge_pt, QFont.Weight.Bold))
        self.reset_active_btn.setFont(QFont("Microsoft YaHei UI", badge_pt, QFont.Weight.Bold))
        self.switch_hint_lbl.setFont(QFont("Microsoft YaHei UI", badge_pt))
        if hasattr(self, "settings_btn"):
            self.settings_btn.setFont(QFont("Segoe UI Symbol", max(8, title_pt), QFont.Weight.Bold))
        self.close_btn.setFont(QFont("Segoe UI", max(8, title_pt), QFont.Weight.Bold))
        has_sub_cfg = bool(self.config_mgr.get("sub_agents", []))
        self.switch_hint_lbl.setVisible(False)

        # 3. 左右双栏比例（支持横向分隔手柄拖拽占比与折叠）与间距自适应
        b_pad = max(5, min(16, int(round(8 * scale))))
        b_top = max(4, min(14, int(round(6 * scale))))
        self.body_lay.setContentsMargins(b_pad, b_top, b_pad, b_pad)
        self.body_lay.setSpacing(0)

        splitter_w = 14
        if hasattr(self, "splitter_handle"):
            self.splitter_handle.setFixedWidth(splitter_w)

        avail_body_w = max(380, w - b_pad * 2 - splitter_w)
        min_sb_w = 155
        max_sb_w = max(min_sb_w + 20, avail_body_w - 250)
        if self._sidebar_collapsed:
            self._sidebar_qframe.setFixedWidth(0)
            sidebar_w = 0
        else:
            sidebar_w = max(min_sb_w, min(max_sb_w, int(round(avail_body_w * self._sidebar_ratio))))
            self._sidebar_qframe.setFixedWidth(sidebar_w)
        self._current_sidebar_w = sidebar_w

        filter_h = max(18, min(36, int(round(22 * scale))))
        filter_pt = max(7, min(13, int(round(8 * scale))))
        for btn in self.filter_btns.values():
            btn.setFixedHeight(filter_h)
            btn.setFont(QFont("Microsoft YaHei UI", filter_pt, QFont.Weight.Bold))

        cnt_pt = max(6, min(12, int(round(7 * scale))))
        self.sidebar_count_lbl.setFont(QFont("Microsoft YaHei UI", cnt_pt))

        if not self._sidebar_collapsed:
            for row in self._sidebar_rows.values():
                row.apply_scale(scale, sidebar_w)

        # 4. 右侧 Token 统计页全区域宽高自适应（结合右侧实际宽高二维缩放）
        deck_avail_w = avail_body_w if self._sidebar_collapsed else max(250, avail_body_w - sidebar_w)
        card_avail_h = max(195, h - header_h - b_top - b_pad - 2)

        deck_sx = deck_avail_w / 465.0
        deck_sy = card_avail_h / 280.0
        card_scale = max(0.72, min(2.5, deck_sx * 0.48 + deck_sy * 0.52))

        c_pad_x = max(7, min(22, int(round(10 * card_scale))))
        c_pad_y = max(5, min(18, int(round(7 * card_scale))))
        c_space = max(3, min(12, int(round(5 * card_scale))))

        info_pad_x = max(2, min(10, int(round(4 * card_scale))))
        info_pad_y = max(2, min(10, int(round(3 * card_scale))))
        tot_pad_x = max(2, min(10, int(round(4 * card_scale))))
        tot_pad_y = max(3, min(12, int(round(4 * card_scale))))
        grid_space = max(4, min(14, int(round(6 * card_scale))))
        cell_pad_x = max(2, min(10, int(round(4 * card_scale))))
        cell_pad_y = max(2, min(10, int(round(3 * card_scale))))

        head_pt = max(7, min(15, int(round(8.5 * card_scale))))
        text_pt = max(7, min(14, int(round(8.2 * card_scale))))
        sub_lbl_pt = max(6, min(13, int(round(7.5 * card_scale))))

        # 根据卡片各区块分配的垂直高度与水平宽度，计算大字与 2x2 单元格数字的目标字号与最大像素边界
        inner_h = max(140, card_avail_h - c_pad_y * 2 - c_space * 3 - 22)
        tot_box_h = int(inner_h * (4.0 / 12.0))
        grid_box_h = int(inner_h * (5.0 / 12.0))
        single_cell_h = max(28, (grid_box_h - grid_space) // 2)

        big_avail_h = max(16, tot_box_h - tot_pad_y * 2 - int(sub_lbl_pt * 1.7) - 4)
        cell_val_avail_h = max(14, single_cell_h - cell_pad_y * 2 - int(sub_lbl_pt * 1.6) - 2)

        big_main_target_pt = max(12, min(34, int(round(16.5 * card_scale))))
        big_sub_target_pt = max(10, min(28, int(round(14.0 * card_scale))))
        grid_val_target_pt = max(9, min(24, int(round(12.5 * card_scale))))

        tot_inner_w = max(160, deck_avail_w - c_pad_x * 2 - tot_pad_x * 2)
        big_left_max_w = int(tot_inner_w * 0.56)
        big_right_max_w = int(tot_inner_w * 0.40)
        cell_max_w = max(70, int((deck_avail_w - c_pad_x * 2 - grid_space) / 2) - cell_pad_x * 2 - 4)

        for view in self.card_views:
            view["card_lay"].setContentsMargins(c_pad_x, c_pad_y, c_pad_x, c_pad_y)
            view["card_lay"].setSpacing(c_space)
            view["info_lay"].setContentsMargins(info_pad_x, info_pad_y, info_pad_x, info_pad_y)
            view["tot_lay"].setContentsMargins(tot_pad_x, tot_pad_y, tot_pad_x, tot_pad_y)
            view["grid_lay"].setSpacing(grid_space)

            view["lbl_t1"].setFont(QFont("Microsoft YaHei UI", sub_lbl_pt))
            view["lbl_t2"].setFont(QFont("Microsoft YaHei UI", sub_lbl_pt))
            if "cost_val" in view:
                view["cost_val"].setFont(QFont("Microsoft YaHei UI", sub_lbl_pt, QFont.Weight.Bold))

            if view["kind"] == "agent":
                view["engine_title_lbl"].setFont(QFont("Microsoft YaHei UI", head_pt, QFont.Weight.Bold))
                view["back_active_lbl"].setFont(QFont("Microsoft YaHei UI", max(6, head_pt - 1), QFont.Weight.Bold))
                view["status_lbl"].setFont(QFont("Microsoft YaHei UI", head_pt, QFont.Weight.Bold))
                view["session_lbl"].setFont(QFont("Microsoft YaHei UI", text_pt, QFont.Weight.Bold))
                view["meta_lbl"].setFont(QFont("Consolas", text_pt))
                view["step_lbl"].setFont(QFont("Microsoft YaHei UI", text_pt))
                self._refresh_agent_card_elided_texts(view, deck_avail_w - c_pad_x * 2 - info_pad_x * 2, text_pt)
                self._fit_number_label_font(
                    view["total_val"],
                    "Consolas",
                    big_main_target_pt,
                    9,
                    big_left_max_w,
                    big_avail_h,
                    bold=True,
                )
                self._fit_number_label_font(
                    view["ctx_val"],
                    "Consolas",
                    big_sub_target_pt,
                    8,
                    big_right_max_w,
                    big_avail_h,
                    bold=True,
                )
            else:
                view["t_lbl"].setFont(QFont("Microsoft YaHei UI", head_pt, QFont.Weight.Bold))
                view["date_lbl"].setFont(QFont("Consolas", head_pt))
                view["counts_lbl"].setFont(QFont("Microsoft YaHei UI", text_pt))
                view["split_lbl"].setFont(QFont("Consolas", text_pt))
                self._fit_number_label_font(
                    view["total_val"],
                    "Consolas",
                    big_main_target_pt,
                    9,
                    big_left_max_w,
                    big_avail_h,
                    bold=True,
                )
                self._fit_number_label_font(
                    view["cache_rate_val"],
                    "Consolas",
                    big_sub_target_pt,
                    8,
                    big_right_max_w,
                    big_avail_h,
                    bold=True,
                )

            for cell_info in view["grid_cells"]:
                cell_info["lay"].setContentsMargins(cell_pad_x, cell_pad_y, cell_pad_x, cell_pad_y)
                cell_info["title_lbl"].setFont(QFont("Microsoft YaHei UI", sub_lbl_pt))
                self._fit_number_label_font(
                    cell_info["val_lbl"],
                    "Consolas",
                    grid_val_target_pt,
                    7,
                    cell_max_w,
                    cell_val_avail_h,
                    bold=True,
                )

    def _refresh_agent_card_elided_texts(self, view: dict[str, Any], avail_text_w: int, text_pt: int) -> None:
        """根据右侧卡片当前可用像素宽度，动态调整会话名、模型名与最近工具名的截断长度。"""
        raw_session = str(view.get("_raw_session_name") or "无")
        if raw_session == "无":
            view["session_lbl"].setText("会话: 无")
            return

        char_unit = max(6.5, text_pt * 1.05)
        max_sess_chars = max(16, int(avail_text_w / char_unit) - 4)
        short_sess = _truncate_session_name(raw_session, max_sess_chars)
        view["session_lbl"].setText(f"会话: {short_sess}")

        short_id = str(view.get("_raw_short_id") or "-")
        raw_model = str(view.get("_raw_model") or "-")
        dur_str = str(view.get("_raw_dur_str") or "0s")
        max_model_chars = max(12, int(avail_text_w / max(5.5, text_pt * 0.85)) - 26)
        model_disp = raw_model if len(raw_model) <= max_model_chars else (raw_model[: max(8, max_model_chars - 2)] + "..")
        view["meta_lbl"].setText(f"ID: {short_id} | 模型: {model_disp} | 耗时: {dur_str}")

        steps = int(view.get("_raw_steps") or 0)
        t_calls = int(view.get("_raw_t_calls") or 0)
        raw_tool = str(view.get("_raw_last_tool") or "-")
        max_tool_chars = max(12, int(avail_text_w / max(6.0, text_pt * 0.9)) - 22)
        tool_disp = raw_tool if len(raw_tool) <= max_tool_chars else (raw_tool[: max(8, max_tool_chars - 2)] + "..")
        view["step_lbl"].setText(f"进度: {steps} 轮 ({t_calls} 次工具) | 最近工具: {tool_disp}")

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._apply_responsive_scale()

    def _on_grand_total_hovered(self, hovered: bool) -> None:
        self._is_hovering_grand_total = hovered
        if hovered:
            self._grand_total_qlabel.setText(self._grand_total_hover_text)
            self._grand_total_qlabel.setStyleSheet(
                f"color: {self.FG_PURPLE}; background: transparent; border: none; padding: 2px 6px;"
            )
        else:
            self._grand_total_qlabel.setText(self._grand_total_compact_text)
            self._grand_total_qlabel.setStyleSheet(
                f"color: {self.FG_YELLOW}; background: transparent; border: none; padding: 2px 6px;"
            )
        self._adjust_window_geometry()

    def _on_mousewheel(self, event: Any) -> None:
        """兼容自检与滚轮滚动调用。"""
        delta = getattr(event, "delta", -120)
        bar = self.sidebar_scroll.verticalScrollBar()
        bar.setValue(bar.value() - int(delta / 2))

    def _set_sidebar_filter(self, filter_key: str) -> None:
        if not self._has_enabled_sub_agents():
            self.sidebar_filter = "main"
        elif filter_key in ("all", "main", "sub"):
            self.sidebar_filter = filter_key
        else:
            self.sidebar_filter = "all"
        self._update_filter_btn_styles()
        self._sidebar_order_sig = None
        self._refresh_sidebar_items()

    def _update_sidebar_selection_visuals(self) -> None:
        """毫秒级选中态更新：仅触发受影响行的 QPainter 局部重绘，零布局重排。"""
        for (eng, fp), row_widget in self._sidebar_rows.items():
            is_sel = bool(
                (eng == "main" and fp and fp == self.pinned_main_file)
                or (eng == "sub" and fp and fp == self.pinned_sub_file)
            )
            row_widget.set_selected(is_sel)

    def _select_history_session(self, engine_type: str, file_path: str) -> None:
        lazy_resolved = False
        if engine_type == "main":
            self.pinned_main_file = file_path
            self.pinned_sub_file = None
            for idx, s in enumerate(self._last_main_sessions):
                if str(s.get("file_path") or "") == file_path and bool(s.get("is_lazy_loaded")):
                    loaded = self.monitor.resolve_single_session_full(
                        file_path, parser_type="antigravity_transcript"
                    )
                    if isinstance(loaded, dict):
                        loaded.setdefault("agent_id", s.get("agent_id", "main"))
                        s.update(loaded)
                        self._last_main_sessions[idx] = s
                        lazy_resolved = True
                    break
        elif engine_type == "sub":
            self.pinned_sub_file = file_path
            self.pinned_main_file = None
            for idx, s in enumerate(self._last_sub_sessions):
                if str(s.get("file_path") or "") == file_path and bool(s.get("is_lazy_loaded")):
                    p_type = str(s.get("parser_type") or "") or None
                    loaded = self.monitor.resolve_single_session_full(
                        file_path, parser_type=p_type
                    )
                    if isinstance(loaded, dict):
                        for meta_k in ("agent_id", "agent_name", "sub_agent_name", "parser_type", "live_state_path"):
                            if meta_k in s and not loaded.get(meta_k):
                                loaded[meta_k] = s[meta_k]
                        if p_type == "antigravity_transcript":
                            if not loaded.get("session_id"):
                                loaded["session_id"] = str(loaded.get("cid") or "")
                            if not loaded.get("project"):
                                loaded["project"] = s.get("agent_name", "")
                        s.update(loaded)
                        self._last_sub_sessions[idx] = s
                        lazy_resolved = True
                    break
        if lazy_resolved:
            self._refresh_sidebar_items()
        self._show_card(0)
        self._update_sidebar_selection_visuals()
        self._refresh_displayed_cards()

    def _reset_pinned_session(self, event: Any = None) -> None:
        self.pinned_main_file = None
        self.pinned_sub_file = None
        self._show_card(0)
        self._update_sidebar_selection_visuals()
        self._refresh_displayed_cards()

    def _refresh_sidebar_items(self) -> None:
        """
        渲染侧边栏主/子 Agent 会话列表：
        - 无已启用子 Agent 时隐藏 filter_bar 并仅展示主 Agent 会话；
        - 存在已启用子 Agent 时显示 filter_bar 并按 all / main / sub 过滤渲染；
        - 所有 SidebarRowWidget 实例永久缓存在 self._sidebar_rows 中。
        """
        has_sub = self._has_enabled_sub_agents()
        if hasattr(self, "filter_bar"):
            self.filter_bar.setVisible(has_sub)

        eff_filter = self.sidebar_filter if has_sub else "main"
        if eff_filter not in ("all", "main", "sub"):
            eff_filter = "all" if has_sub else "main"

        items: list[tuple[str, dict[str, Any]]] = []
        if eff_filter in ("all", "main"):
            for s in self._last_main_sessions:
                if int(s.get("steps", 0)) > 0 or int(s.get("total_tokens", 0)) > 0 or bool(s.get("is_lazy_loaded")):
                    items.append(("main", s))
        if has_sub and eff_filter in ("all", "sub"):
            for s in self._last_sub_sessions:
                if int(s.get("steps", 0)) > 0 or int(s.get("total_tokens", 0)) > 0 or bool(s.get("is_lazy_loaded")):
                    items.append(("sub", s))

        items.sort(key=lambda pair: float(pair[1].get("last_updated") or 0.0), reverse=True)
        self.sidebar_count_lbl.setText(f"共 {len(items)} 条会话 · 点击切换")

        order_keys = [(eng, str(d.get("file_path") or "")) for eng, d in items]
        order_sig = (has_sub, eff_filter, tuple(order_keys))

        palette = self._get_current_palette()
        # 无论顺序是否改变，先原地更新每个条目的文本与选中状态
        for eng, sess in items:
            fpath = str(sess.get("file_path") or "")
            key = (eng, fpath)
            is_selected = bool(
                (eng == "main" and fpath and fpath == self.pinned_main_file)
                or (eng == "sub" and fpath and fpath == self.pinned_sub_file)
            )
            row = self._sidebar_rows.get(key)
            if row is None:
                row = SidebarRowWidget(eng, fpath, self.sidebar_list_inner)
                row.row_clicked.connect(self._select_history_session)
                row.apply_theme(
                    palette,
                    bg_opacity=self.bg_opacity,
                    glass_enabled=(self.backdrop_type != "none"),
                )
                row.apply_scale(self._current_scale, self._current_sidebar_w)
                self._sidebar_rows[key] = row
            row.update_session_data(sess, is_selected)

        # 若顺序和过滤集合未变，直接返回（0 布局操作）
        if order_sig == self._sidebar_order_sig:
            return
        self._sidebar_order_sig = order_sig
        self._active_row_keys = order_keys

        self.sidebar_list_inner.setUpdatesEnabled(False)
        try:
            # 从布局中移除旧项（不销毁组件）
            while self.sidebar_list_layout.count() > 0:
                item = self.sidebar_list_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.hide()

            if not items:
                self.empty_sidebar_lbl.show()
                self.sidebar_list_layout.addWidget(self.empty_sidebar_lbl)
                self.sidebar_list_layout.addStretch(1)
            else:
                self.empty_sidebar_lbl.hide()
                for key in order_keys:
                    row = self._sidebar_rows[key]
                    self.sidebar_list_layout.addWidget(row)
                    row.show()
                self.sidebar_list_layout.addStretch(1)
        finally:
            self.sidebar_list_inner.setUpdatesEnabled(True)

    @staticmethod
    def _hex_to_rgba_css(hex_color: str, alpha: float) -> str:
        c = QColor(hex_color)
        if not c.isValid():
            return hex_color
        a_clamped = max(0.0, min(1.0, float(alpha)))
        if a_clamped >= 0.999:
            return hex_color
        return f"rgba({c.red()}, {c.green()}, {c.blue()}, {a_clamped:.2f})"

    def _sync_palette_from_config(self) -> dict[str, str]:
        theme_cfg = self.config_mgr.get("theme", {})
        if not isinstance(theme_cfg, dict):
            theme_cfg = {}
        palette = _derive_theme_palette(theme_cfg)
        self.BG_MAIN = palette["BG_MAIN"]
        self.BG_HEADER = palette["BG_HEADER"]
        self.BG_CARD = palette["BG_CARD"]
        self.BG_INNER = palette["BG_INNER"]
        self.BG_HOVER = palette["BG_HOVER"]
        self.BG_SELECTED = palette["BG_SELECTED"]
        self.FG_PRIMARY = palette["FG_PRIMARY"]
        self.FG_MUTED = palette["FG_MUTED"]
        self.FG_ACCENT = palette["FG_ACCENT"]
        self.BORDER_COLOR = palette["BORDER_COLOR"]
        try:
            self.alpha = float(theme_cfg.get("opacity", 0.94) or 0.94)
            self.alpha = max(0.2, min(1.0, self.alpha))
        except Exception:
            self.alpha = 0.94
        raw_bd = str(theme_cfg.get("backdrop") or "mica").strip().lower()
        self.backdrop_type = raw_bd if raw_bd in ("mica", "acrylic", "mica_alt", "none") else "mica"

        try:
            self.bg_opacity = float(theme_cfg.get("bg_opacity", 0.65) or 0.65)
            self.bg_opacity = max(0.05, min(1.0, self.bg_opacity))
        except Exception:
            self.bg_opacity = 0.65

        self._update_bg_opacity_css_and_colors(self.bg_opacity)
        return palette

    def _update_bg_opacity_css_and_colors(self, opacity: float | None = None) -> None:
        if opacity is not None:
            try:
                self.bg_opacity = max(0.05, min(1.0, float(opacity)))
            except Exception:
                self.bg_opacity = 0.65

        alpha_card = self.bg_opacity
        alpha_main = max(0.02, self.bg_opacity * 0.55)

        if self.backdrop_type != "none" or self.bg_opacity < 1.0:
            self.BG_MAIN_CSS = self._hex_to_rgba_css(self.BG_MAIN, alpha_main)
            self.BG_HEADER_CSS = self._hex_to_rgba_css(self.BG_HEADER, alpha_card)
            self.BG_CARD_CSS = self._hex_to_rgba_css(self.BG_CARD, alpha_card)
            self.BG_INNER_CSS = self._hex_to_rgba_css(self.BG_INNER, alpha_card)
            eff_alpha_main = int(round(alpha_main * 255))
            eff_alpha_card = int(round(alpha_card * 255))
        else:
            self.BG_MAIN_CSS = self.BG_MAIN
            self.BG_HEADER_CSS = self.BG_HEADER
            self.BG_CARD_CSS = self.BG_CARD
            self.BG_INNER_CSS = self.BG_INNER
            eff_alpha_main = 255
            eff_alpha_card = 255

        c_main = QColor(self.BG_MAIN)
        c_main.setAlpha(eff_alpha_main)
        self._bg_main_qcolor = c_main

        c_card = QColor(self.BG_CARD)
        c_card.setAlpha(eff_alpha_card)
        self._bg_card_qcolor = c_card

        self._border_qcolor = QColor(self.BORDER_COLOR)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        bg_color = getattr(self, "_bg_main_qcolor", QColor(20, 22, 23, 91))
        border_color = getattr(self, "_border_qcolor", QColor("#282828"))
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.fillRect(rect, bg_color)
        painter.setPen(QPen(border_color, 1))
        painter.drawRect(rect)

    def _get_current_palette(self) -> dict[str, str]:
        return {
            "BG_MAIN": self.BG_MAIN,
            "BG_HEADER": self.BG_HEADER,
            "BG_CARD": self.BG_CARD,
            "BG_INNER": self.BG_INNER,
            "BG_HOVER": self.BG_HOVER,
            "BG_SELECTED": self.BG_SELECTED,
            "FG_PRIMARY": self.FG_PRIMARY,
            "FG_MUTED": self.FG_MUTED,
            "FG_ACCENT": self.FG_ACCENT,
            "BORDER_COLOR": self.BORDER_COLOR,
        }

    def apply_bg_opacity(self, opacity: float | None = None) -> None:
        """轻量热更新背景不透明度：绝不调用 DWM API，不触发全量 QSS 重排，仅刷新受背景透明度影响的关键容器。"""
        self._update_bg_opacity_css_and_colors(opacity)

        if hasattr(self, "header"):
            self.header.setStyleSheet(f"background-color: {self.BG_HEADER_CSS}; border: none;")
        if hasattr(self, "reset_active_btn") and self.reset_active_btn.isVisible():
            self.reset_active_btn.setStyleSheet(
                f"color: {self.FG_GREEN}; background-color: {self.BG_INNER_CSS}; border: none; padding: 2px 6px; border-radius: 3px;"
            )
        if hasattr(self, "filter_bar") and self.filter_bar.isVisible():
            self.filter_bar.setStyleSheet(f"background-color: {self.BG_INNER_CSS}; border: none; border-radius: 3px;")

        if hasattr(self, "card_views"):
            for view in self.card_views:
                if view.get("kind") != "summary" and "back_active_lbl" in view and view["back_active_lbl"].isVisible():
                    view["back_active_lbl"].setStyleSheet(
                        f"color: {self.FG_GREEN}; background-color: {self.BG_INNER_CSS}; border: none; padding: 1px 6px; border-radius: 3px;"
                    )

        for row in self._sidebar_rows.values():
            row.apply_bg_opacity(self.bg_opacity)

        self.update()

    def apply_theme(self) -> None:
        """动态热重载主窗口、卡片视图与所有 SidebarRowWidget 的主题配色样式与 DWM 毛玻璃背景材质。"""
        palette = self._sync_palette_from_config()
        self.setWindowOpacity(self.alpha)
        if getattr(self, "_applied_dwm_backdrop", None) != self.backdrop_type or not self.dwm_info:
            self.dwm_info = apply_win11_mica_and_rounded_corners(
                self, backdrop_type=self.backdrop_type
            )
            self._applied_dwm_backdrop = self.backdrop_type
        glass_enabled = self.backdrop_type != "none"

        if hasattr(self, "header"):
            self.header.setStyleSheet(f"background-color: {self.BG_HEADER_CSS}; border: none;")
        if hasattr(self, "_title_qlabel"):
            self._title_qlabel.setStyleSheet(f"color: {self.FG_PRIMARY}; background: transparent; border: none;")
        if hasattr(self, "_grand_total_qlabel"):
            self._on_grand_total_hovered(self._is_hovering_grand_total)
        if hasattr(self, "reset_active_btn"):
            self.reset_active_btn.setStyleSheet(
                f"color: {self.FG_GREEN}; background-color: {self.BG_INNER_CSS}; border: none; padding: 2px 6px; border-radius: 3px;"
            )
        if hasattr(self, "switch_hint_lbl"):
            self.switch_hint_lbl.setStyleSheet(f"color: {self.FG_ACCENT}; background: transparent; border: none; padding: 0 4px;")
        if hasattr(self, "settings_btn"):
            self.settings_btn.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none; padding: 2px 6px;")
        if hasattr(self, "close_btn"):
            self.close_btn.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none; padding: 2px 8px;")

        if hasattr(self, "body_frame"):
            self.body_frame.setStyleSheet("background: transparent; border: none;")
        if hasattr(self, "_deck_qwidget"):
            self._deck_qwidget.setStyleSheet("background: transparent; border: none;")

        if hasattr(self, "_sidebar_qframe"):
            self._sidebar_qframe.update()
        if hasattr(self, "filter_bar"):
            self.filter_bar.setStyleSheet(f"background-color: {self.BG_INNER_CSS}; border: none; border-radius: 3px;")
            self.filter_bar.setVisible(self._has_enabled_sub_agents())
        if hasattr(self, "filter_btns"):
            self._update_filter_btn_styles()
        if hasattr(self, "sidebar_count_lbl"):
            self.sidebar_count_lbl.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none; padding: 1px 4px;")
        if hasattr(self, "sidebar_scroll"):
            self.sidebar_scroll.setStyleSheet("background: transparent; border: none;")
        if hasattr(self, "sidebar_list_inner"):
            self.sidebar_list_inner.setStyleSheet("background: transparent; border: none;")
        if hasattr(self, "empty_sidebar_lbl"):
            self.empty_sidebar_lbl.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none; padding: 16px;")

        if hasattr(self, "card_views"):
            for view in self.card_views:
                kind = view.get("kind")
                view["frame"].update()
                if "info_frame" in view:
                    view["info_frame"].setStyleSheet("background: transparent; border: none;")
                if "tot_frame" in view:
                    view["tot_frame"].setStyleSheet("QFrame#totFrame { background: transparent; border: none; }")
                view["lbl_t1"].setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
                view["lbl_t2"].setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
                if "cost_val" in view:
                    view["cost_val"].setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
                view["lbl_in"].setStyleSheet(f"color: {self.FG_PRIMARY}; background: transparent; border: none;")
                view["lbl_cache"].setStyleSheet(f"color: {self.FG_PURPLE}; background: transparent; border: none;")
                view["lbl_out"].setStyleSheet(f"color: {self.FG_YELLOW}; background: transparent; border: none;")
                view["lbl_thought"].setStyleSheet(f"color: {self.FG_ACCENT}; background: transparent; border: none;")

                if kind == "summary":
                    view["t_lbl"].setStyleSheet(f"color: {self.FG_ACCENT}; background: transparent; border: none;")
                    view["date_lbl"].setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
                    view["counts_lbl"].setStyleSheet(f"color: {self.FG_PRIMARY}; background: transparent; border: none;")
                    view["split_lbl"].setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
                    view["total_val"].setStyleSheet(f"color: {self.FG_ACCENT}; background: transparent; border: none;")
                    view["cache_rate_val"].setStyleSheet(f"color: {self.FG_GREEN}; background: transparent; border: none;")
                else:
                    view["back_active_lbl"].setStyleSheet(
                        f"color: {self.FG_GREEN}; background-color: {self.BG_INNER_CSS}; border: none; padding: 1px 6px; border-radius: 3px;"
                    )
                    view["session_lbl"].setStyleSheet(f"color: {self.FG_PRIMARY}; background: transparent; border: none;")
                    view["meta_lbl"].setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
                    view["step_lbl"].setStyleSheet(f"color: {self.FG_PRIMARY}; background: transparent; border: none;")
                    view["total_val"].setStyleSheet(f"color: {self.FG_GREEN}; background: transparent; border: none;")
                    view["ctx_val"].setStyleSheet(f"color: {self.FG_ACCENT}; background: transparent; border: none;")

                for cell_info in view.get("grid_cells", []):
                    cell_info["cell"].setStyleSheet("background: transparent; border: none;")
                    cell_info["title_lbl"].setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")

        for row in self._sidebar_rows.values():
            row.apply_theme(palette, bg_opacity=self.bg_opacity, glass_enabled=glass_enabled)

        if hasattr(self, "splitter_handle"):
            self.splitter_handle.update()
        if hasattr(self, "resize_grip"):
            self.resize_grip.update()
        if getattr(self, "tray_menu", None) is not None:
            self._apply_tray_menu_style()
        self.update()

    def _recalculate_snapshot_pricing_in_place(self, snap: dict[str, Any]) -> None:
        """基于最新定价引擎重新计算快照中各会话及今日汇总的 USD / CNY 折算费用。"""
        if not isinstance(snap, dict):
            return
        main_sessions = snap.get("all_main_sessions") or []
        sub_sessions = snap.get("all_sub_sessions") or []
        main_today_u = 0.0
        main_today_c = 0.0
        main_tot_u = 0.0
        main_tot_c = 0.0
        for ms in main_sessions:
            if isinstance(ms, dict):
                if "today_input_tokens" not in ms and "input_tokens" in ms:
                    ms["today_input_tokens"] = int(ms.get("input_tokens", 0) or 0)
                    ms["today_cached_tokens"] = int(ms.get("cached_tokens", 0) or 0)
                    ms["today_output_tokens"] = int(ms.get("output_tokens", 0) or 0)
                    ms["today_thought_tokens"] = int(ms.get("thought_tokens", 0) or 0)
                _update_session_pricing(ms)
                main_tot_u = round(main_tot_u + float(ms.get("total_cost_usd", 0.0) or 0.0), 6)
                main_tot_c = round(main_tot_c + float(ms.get("total_cost_cny", 0.0) or 0.0), 6)
                if int(ms.get("today_steps", ms.get("steps", 0)) or 0) > 0:
                    main_today_u = round(main_today_u + float(ms.get("today_cost_usd", 0.0) or 0.0), 6)
                    main_today_c = round(main_today_c + float(ms.get("today_cost_cny", 0.0) or 0.0), 6)

        sub_today_u = 0.0
        sub_today_c = 0.0
        sub_tot_u = 0.0
        sub_tot_c = 0.0
        for ss in sub_sessions:
            if isinstance(ss, dict):
                if "today_input_tokens" not in ss and "input_tokens" in ss:
                    ss["today_input_tokens"] = int(ss.get("input_tokens", 0) or 0)
                    ss["today_cached_tokens"] = int(ss.get("cached_tokens", 0) or 0)
                    ss["today_output_tokens"] = int(ss.get("output_tokens", 0) or 0)
                    ss["today_thought_tokens"] = int(ss.get("thought_tokens", 0) or 0)
                _update_session_pricing(ss)
                sub_tot_u = round(sub_tot_u + float(ss.get("total_cost_usd", 0.0) or 0.0), 6)
                sub_tot_c = round(sub_tot_c + float(ss.get("total_cost_cny", 0.0) or 0.0), 6)
                if int(ss.get("today_steps", ss.get("steps", 0)) or 0) > 0:
                    sub_today_u = round(sub_today_u + float(ss.get("today_cost_usd", 0.0) or 0.0), 6)
                    sub_today_c = round(sub_today_c + float(ss.get("today_cost_cny", 0.0) or 0.0), 6)

        if isinstance(snap.get("main_session"), dict) and main_sessions:
            snap["main_session"] = dict(main_sessions[0])
        if isinstance(snap.get("active_session"), dict) and sub_sessions:
            snap["active_session"] = dict(sub_sessions[0])

        comb_today_u = round(main_today_u + sub_today_u, 6)
        comb_today_c = round(main_today_c + sub_today_c, 6)
        comb_tot_u = round(main_tot_u + sub_tot_u, 6)
        comb_tot_c = round(main_tot_c + sub_tot_c, 6)

        today_stats = snap.get("today_stats")
        if isinstance(today_stats, dict):
            today_stats["main_cost_usd"] = main_today_u
            today_stats["sub_cost_usd"] = sub_today_u
            today_stats["main_total_cost_usd"] = main_tot_u
            today_stats["sub_total_cost_usd"] = sub_tot_u
            today_stats["cost_usd"] = comb_today_u
            today_stats["cost_cny"] = comb_today_c
            today_stats["today_cost_usd"] = comb_today_u
            today_stats["today_cost_cny"] = comb_today_c
            today_stats["total_cost_usd"] = comb_tot_u
            today_stats["total_cost_cny"] = comb_tot_c
        snap["cost_usd"] = comb_today_u
        snap["cost_cny"] = comb_today_c
        snap["today_cost_usd"] = comb_today_u
        snap["today_cost_cny"] = comb_today_c
        snap["total_cost_usd"] = comb_tot_u
        snap["total_cost_cny"] = comb_tot_c

    def apply_settings(self) -> None:
        """设置保存后的热重载入口：动态更新定时器周期、目录扫描 TTL、模型定价与汇率、主题配色并立即重扫刷新费用显示。"""
        poll_ms = int(self.config_mgr.get("poll_interval_ms", self.refresh_ms) or self.refresh_ms)
        self.refresh_ms = max(250, min(5000, poll_ms))
        self.standby_refresh_ms = max(self.STANDBY_REFRESH_MS, self.refresh_ms)
        now_ts = time.time()
        self.last_activity_time = now_ts
        self.monitor.last_activity_time = now_ts
        self._is_polling_backoff = False
        if hasattr(self, "_timer"):
            self._timer.setInterval(self.refresh_ms)

        ttl_sec = float(self.config_mgr.get("scan_ttl_sec", 2.5) or 2.5)
        self.monitor.scan_ttl_sec = max(1.0, min(10.0, ttl_sec))

        self.monitor.sync_pricing_from_config()
        self.pricing_engine = self.monitor.pricing_engine

        self.apply_theme()
        self._sidebar_order_sig = None

        prev_snap = getattr(self, "_last_snap", None)
        snap = self.monitor.collect_snapshot(
            target_session=self.target_session,
            target_project=self.target_project,
            force_scan=True,
            today_date=getattr(self.monitor, "_current_date", None),
        )
        if (
            not (snap.get("all_main_sessions") or snap.get("all_sub_sessions"))
            and isinstance(prev_snap, dict)
            and (prev_snap.get("all_main_sessions") or prev_snap.get("all_sub_sessions"))
        ):
            self._recalculate_snapshot_pricing_in_place(prev_snap)
            self._apply_snapshot(prev_snap)
        else:
            self._apply_snapshot(snap)

    def _async_startup_pricing_update(self) -> None:
        """启动后若配置开启 auto_update_pricing，则在独立后台守护线程静默同步汇率与模型价格表，全程零阻塞。"""
        try:
            if not bool(self.config_mgr.get("auto_update_pricing", True)):
                return
        except Exception:
            return

        proxy_val = str(self.config_mgr.get("proxy", "") or "").strip()

        def _worker() -> None:
            try:
                changed = False
                online_rate = fetch_online_usd_rate(proxy=proxy_val, timeout=3.0)
                if online_rate is not None:
                    self.config_mgr.set("usd_to_cny_rate", float(online_rate))
                    changed = True

                remote_table = fetch_remote_pricing_table(proxy=proxy_val, timeout=3.0)
                if isinstance(remote_table, dict) and remote_table:
                    local_pricing = self.config_mgr.get("pricing", {})
                    merged = merge_remote_pricing_into_config(
                        local_pricing=local_pricing if isinstance(local_pricing, dict) else {},
                        remote_pricing=remote_table,
                    )
                    self.config_mgr.set("pricing", merged)
                    changed = True

                if changed:
                    try:
                        self.config_mgr.save()
                    except Exception:
                        pass
                    self._startup_pricing_bridge.sync_finished.emit(True)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_startup_pricing_sync_finished(self, changed: bool) -> None:
        if not changed:
            return
        try:
            self.apply_settings()
        except Exception:
            pass

    def open_settings(self) -> int:
        """实例化并弹出设置对话框。"""
        dlg = SettingsDialog(self, self.config_mgr)
        self._settings_dialog = dlg
        return int(dlg.exec())

    # ------------------------------------------------------------------
    # 系统托盘常驻 (QSystemTrayIcon) 与 Win32 全局热键 (Ctrl + Alt + T)
    # ------------------------------------------------------------------
    def _render_hud_icon_pixmap(self, size: int) -> QPixmap:
        """按 64x64 基准坐标系等比缩放绘制【方案 1：HUD 仪表弧光】矢量图标。"""
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale = size / 64.0
        painter.scale(scale, scale)

        # 1. 深暗色圆角底盘（撑满边缘，提升边框线宽与对比度）
        painter.setBrush(QColor(self.BG_CARD))
        border_pen = QPen(QColor(self.BORDER_COLOR), 2.4)
        painter.setPen(border_pen)
        painter.drawRoundedRect(QRectF(1.5, 1.5, 61.0, 61.0), 14.0, 14.0)

        # 2. 270° HUD 仪表量表弧线（开口朝下，半径 26.5，线宽加粗至 7.0）
        painter.setBrush(Qt.BrushStyle.NoBrush)
        arc_pen = QPen(QColor(self.FG_ACCENT), 7.0)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        start_angle = -45 * 16
        span_angle = 270 * 16
        painter.drawArc(QRectF(5.5, 5.5, 53.0, 53.0), start_angle, span_angle)

        # 弧线两个端点修饰圆点（半径 3.4px，中心位于半径 26.5 的 -45° 与 225° 处）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d79921"))
        for deg in (-45.0, 225.0):
            rad = math.radians(deg)
            ep_x = 32.0 + 26.5 * math.cos(rad)
            ep_y = 32.0 - 26.5 * math.sin(rad)
            painter.drawEllipse(QPointF(ep_x, ep_y), 3.4, 3.4)

        # 3. 核心 Token 菱形晶体（Token Core，放大至 16~48 / 17~47）
        poly = QPolygonF(
            [
                QPointF(32.0, 16.0),
                QPointF(47.0, 32.0),
                QPointF(32.0, 48.0),
                QPointF(17.0, 32.0),
            ]
        )
        painter.setBrush(QColor(self.FG_GREEN))
        painter.setPen(QPen(QColor(self.BG_CARD), 2.2))
        painter.drawPolygon(poly)

        # 4. 中心呼吸亮点（半径 4.0px）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.FG_PRIMARY))
        painter.drawEllipse(QPointF(32.0, 32.0), 4.0, 4.0)

        painter.end()
        return pix

    def _create_tray_icon(self) -> QIcon:
        """动态绘制多尺寸【HUD 仪表弧光】托盘图标并强制覆盖导出 assets/icon.png，同时支持加载自定义扩展图标。"""
        assets_dir = Path(__file__).resolve().parent.parent / "assets"
        try:
            assets_dir.mkdir(parents=True, exist_ok=True)
            icon_path = assets_dir / "icon.png"
            self._render_hud_icon_pixmap(256).save(str(icon_path), "PNG")
        except OSError:
            pass

        for name in ("icon.ico", "tray.ico", "tray.png", "hud-icon.png", "icon.png"):
            candidate = assets_dir / name
            if candidate.exists() and candidate.is_file():
                ico = QIcon(str(candidate))
                if not ico.isNull():
                    for sz in (64, 32, 16):
                        ico.addPixmap(self._render_hud_icon_pixmap(sz))
                    return ico

        icon = QIcon()
        pix_64 = self._render_hud_icon_pixmap(64)
        for pix in (pix_64, self._render_hud_icon_pixmap(32), self._render_hud_icon_pixmap(16)):
            icon.addPixmap(pix)

        return icon

    def _apply_tray_menu_style(self) -> None:
        if self.tray_menu is None:
            return
        self.tray_menu.setStyleSheet(
            f"""
            QMenu {{
                background-color: {self.BG_CARD};
                color: {self.FG_PRIMARY};
                border: 1px solid {self.BORDER_COLOR};
                font-family: "Microsoft YaHei UI";
                font-size: 9pt;
            }}
            QMenu::item {{
                padding: 5px 18px;
            }}
            QMenu::item:selected {{
                background-color: {self.FG_ACCENT};
                color: {self.BG_MAIN};
            }}
            """
        )

    def _init_system_tray(self) -> None:
        """初始化系统托盘图标与右键菜单。"""
        icon = self._create_tray_icon()
        self.setWindowIcon(icon)

        menu = QMenu(self)
        self.tray_menu = menu
        self._apply_tray_menu_style()

        act_toggle = QAction("显示 / 隐藏悬浮窗", self)
        act_toggle.triggered.connect(self.toggle_window_visibility)
        menu.addAction(act_toggle)

        act_rescan = QAction("立即重新扫描", self)
        act_rescan.triggered.connect(lambda: self.rescan_now(full_history=False))
        menu.addAction(act_rescan)

        act_full_rescan = QAction("全量重新扫描 (完整历史)", self)
        act_full_rescan.triggered.connect(lambda: self.rescan_now(full_history=True))
        menu.addAction(act_full_rescan)

        act_export = QAction("导出历史为 CSV...", self)
        act_export.triggered.connect(lambda: self.export_history_to_csv(None))
        menu.addAction(act_export)

        act_settings = QAction("设置...", self)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)

        menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        menu.addAction(act_quit)

        self.tray_actions = {
            "toggle": act_toggle,
            "rescan": act_rescan,
            "full_rescan": act_full_rescan,
            "export_csv": act_export,
            "settings": act_settings,
            "quit": act_quit,
        }

        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("Antigravity & Argon Token HUD (Ctrl+Alt+T)")
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray.show()
        self.tray_icon = tray

    def get_tray_menu_labels(self) -> list[str]:
        if self.tray_menu is None:
            return []
        return [a.text() for a in self.tray_menu.actions() if not a.isSeparator() and a.text()]

    def _on_tray_activated(self, reason: Any) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_window_visibility()

    def toggle_window_visibility(self) -> bool:
        """切换悬浮窗显示或隐藏状态，返回切换后的可见性布尔值。"""
        if self.isVisible() and not self.isMinimized():
            self.hide()
            return False
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self._resume_render_if_needed()
        return True

    def rescan_now(self, full_history: bool = False) -> dict[str, Any]:
        """强制立即重新扫描所有会话目录并更新界面。"""
        snap = self.monitor.collect_snapshot(
            target_session=self.target_session,
            target_project=self.target_project,
            force_scan=True,
            full_history=bool(full_history),
        )
        self._apply_snapshot(snap)
        return snap

    def export_history_to_csv(self, output_path: Path | str | None = None) -> Path | None:
        """先持久化当日归档数据，再将全部历史记录导出为结构化 CSV 文件。"""
        try:
            self.monitor.save_history()
        except Exception:
            pass
        if output_path is None:
            default_csv = str(Path.home() / "token_hud_history.csv")
            selected, _ = QFileDialog.getSaveFileName(
                self,
                "导出历史为 CSV",
                default_csv,
                "CSV Files (*.csv);;All Files (*)",
            )
            if not selected:
                return None
            output_path = selected
        return export_history_to_csv(output_path=output_path, monitor=self.monitor)

    def _register_global_hotkey(self) -> bool:
        """调用 Win32 RegisterHotKey 注册全局热键 Ctrl + Alt + T (ID = 0x9001)。"""
        if sys.platform != "win32":
            return False
        try:
            hwnd = int(self.winId())
        except Exception:
            hwnd = self._top_hwnd
        if not hwnd:
            return False
        self._hotkey_hwnd = hwnd
        try:
            user32 = ctypes.windll.user32
            user32.UnregisterHotKey(wintypes.HWND(hwnd), int(self.HOTKEY_ID))
            ok = bool(
                user32.RegisterHotKey(
                    wintypes.HWND(hwnd),
                    int(self.HOTKEY_ID),
                    int(self.MOD_CONTROL | self.MOD_ALT),
                    int(self.VK_T),
                )
            )
            self._hotkey_registered = ok
            return ok
        except Exception:
            self._hotkey_registered = False
            return False

    def _unregister_global_hotkey(self) -> bool:
        """注销已注册的 Win32 全局热键。"""
        if sys.platform != "win32":
            return False
        hwnd = self._hotkey_hwnd
        if not hwnd:
            try:
                hwnd = int(self.winId())
            except Exception:
                hwnd = 0
        was_reg = self._hotkey_registered
        self._hotkey_registered = False
        self._hotkey_hwnd = 0
        if not hwnd:
            return False
        try:
            return bool(ctypes.windll.user32.UnregisterHotKey(wintypes.HWND(hwnd), int(self.HOTKEY_ID))) or was_reg
        except Exception:
            return False

    def nativeEvent(self, eventType: Any, message: Any) -> tuple[bool, int]:
        """拦截 Win32 WM_HOTKEY (0x0312)，实现全局热键唤起/收起悬浮窗。"""
        if sys.platform == "win32":
            try:
                addr = int(message)
                if addr:
                    msg = wintypes.MSG.from_address(addr)
                    if msg.message == self.WM_HOTKEY and int(msg.wParam) == self.HOTKEY_ID:
                        self.toggle_window_visibility()
                        return True, 0
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def _adjust_window_geometry(self) -> None:
        """
        窗口宽高严格锁定，不再随内部标签文本或卡片内容自动伸缩；
        仅在当前窗口尺寸下执行内部控件与字号的自适应缩放。
        """
        self._apply_responsive_scale()

    def _reset_window_geometry(self) -> None:
        """恢复默认窗口大小、左右双栏比例与右上角位置。"""
        self._user_resized_window = False
        self._user_moved_window = False
        self._sidebar_collapsed = False
        self._sidebar_qframe.setVisible(True)
        self._sidebar_ratio = 0.355
        screen = self._app.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        win_w, win_h = self.MIN_WIN_W, self.MIN_WIN_H
        pos_x = max(20, avail.right() - win_w - 28)
        pos_y = max(20, avail.top() + 36)
        self.setGeometry(pos_x, pos_y, win_w, win_h)
        self._apply_responsive_scale()
        if hasattr(self, "splitter_handle"):
            self.splitter_handle.update()

    def toggle_sidebar(self) -> None:
        """切换左侧历史会话栏的折叠与展开状态，并记忆/恢复宽度占比。"""
        self._sidebar_collapsed = not self._sidebar_collapsed
        if self._sidebar_collapsed:
            self._saved_sidebar_ratio = max(0.18, min(0.65, self._sidebar_ratio))
            self._sidebar_qframe.setVisible(False)
        else:
            self._sidebar_ratio = self._saved_sidebar_ratio
            self._sidebar_qframe.setVisible(True)
        self._apply_responsive_scale()
        if hasattr(self, "splitter_handle"):
            self.splitter_handle.update()

    def _has_enabled_sub_agents(self) -> bool:
        sub_agents = self.config_mgr.get("sub_agents", [])
        if not isinstance(sub_agents, list):
            return False
        for item in sub_agents:
            if not isinstance(item, dict):
                continue
            if bool(item.get("enabled", True)) and str(item.get("path") or "").strip():
                return True
        return False

    def _has_sub_agents(self) -> bool:
        return self._has_enabled_sub_agents()

    def _get_sub_agent_display_name(self, sub_sess: dict[str, Any] | None = None) -> str:
        if sub_sess:
            sess_agent_name = str(sub_sess.get("sub_agent_name") or sub_sess.get("agent_name") or "").strip()
            if sess_agent_name:
                return sess_agent_name
        sub_agents = self.config_mgr.get("sub_agents", [])
        if isinstance(sub_agents, list) and sub_agents:
            names: list[str] = []
            for item in sub_agents:
                if isinstance(item, dict) and bool(item.get("enabled", True)):
                    resolved = ConfigManager._resolve_sub_agent_display_name(
                        item.get("name"), item.get("path")
                    )
                    if resolved and resolved not in names:
                        names.append(resolved)
            if names:
                return " / ".join(names[:2])
        return "子 Agent"

    def _get_dynamic_card_titles(self, sub_sess: dict[str, Any] | None = None) -> list[str]:
        return ["[Token 总用量]"]

    def _show_card(self, idx: int) -> None:
        self.current_card_idx = 0
        self._deck_qwidget.setCurrentIndex(0)
        if self.pinned_main_file or self.pinned_sub_file:
            self._title_qlabel.setText("[当前选中对话]")
        else:
            self._title_qlabel.setText("[Token 总用量]")
        self._adjust_window_geometry()

    def cycle_card(self, event: Any = None) -> str:
        self._show_card(0)
        return "break"

    # ------------------------------------------------------------------
    # 原生无抖动鼠标拖拽、左右分隔手柄调整、右下角缩放与右键菜单事件处理
    # ------------------------------------------------------------------
    def _get_window_screen_origin(self) -> tuple[int, int]:
        pos = self.pos()
        return int(pos.x()), int(pos.y())

    def _move_window_fast(self, new_x: int, new_y: int) -> None:
        self.move(int(new_x), int(new_y))

    def _on_splitter_press(self, x_root: int) -> None:
        """供左右双栏横向调整手柄与 --self-test 统一调用的按下入口。"""
        self._is_splitting = True
        self._split_start_x = int(x_root)
        self._split_orig_sidebar_w = int(self._sidebar_qframe.width())

    def _on_splitter_motion(self, x_root: int) -> None:
        """供左右双栏横向调整手柄与 --self-test 统一调用的拖拽入口。"""
        if not self._is_splitting:
            return
        if self._sidebar_collapsed:
            self._sidebar_collapsed = False
            self._sidebar_qframe.setVisible(True)
        dx = int(x_root) - self._split_start_x
        b_pad = self.body_lay.contentsMargins().left()
        splitter_w = self.splitter_handle.width() if hasattr(self, "splitter_handle") else 14
        avail_body_w = max(380, self.width() - b_pad * 2 - splitter_w)
        min_sb_w = 155
        max_sb_w = max(min_sb_w + 20, avail_body_w - 250)
        new_sb_w = max(min_sb_w, min(max_sb_w, self._split_orig_sidebar_w + dx))
        self._sidebar_ratio = max(0.18, min(0.65, new_sb_w / float(avail_body_w)))
        self._apply_responsive_scale()

    def _on_splitter_release(self, x_root: int) -> None:
        """供左右双栏横向调整手柄与 --self-test 统一调用的释放入口。"""
        if not self._is_splitting:
            return
        self._on_splitter_motion(x_root)
        self._is_splitting = False

    def _on_resize_press(self, x_root: int, y_root: int) -> None:
        """供右下角缩放手柄与 --self-test 统一调用的按下入口。"""
        self._is_resizing = True
        self._resize_start_x = int(x_root)
        self._resize_start_y = int(y_root)
        self._resize_orig_w = int(self.width())
        self._resize_orig_h = int(self.height())

    def _on_resize_motion(self, x_root: int, y_root: int) -> None:
        """供右下角缩放手柄与 --self-test 统一调用的拖拽缩放入口。"""
        if not self._is_resizing:
            return
        dx = int(x_root) - self._resize_start_x
        dy = int(y_root) - self._resize_start_y
        if abs(dx) > 2 or abs(dy) > 2:
            self._user_resized_window = True

        screen = self._app.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        max_w = max(self.MIN_WIN_W, avail.width() - 20)
        max_h = max(self.MIN_WIN_H, avail.height() - 20)

        new_w = max(self.RESIZE_MIN_W, min(max_w, self._resize_orig_w + dx))
        new_h = max(self.RESIZE_MIN_H, min(max_h, self._resize_orig_h + dy))
        if new_w != self.width() or new_h != self.height():
            self.resize(new_w, new_h)

    def _on_resize_release(self, x_root: int, y_root: int) -> None:
        """供右下角缩放手柄与 --self-test 统一调用的缩放释放入口。"""
        if not self._is_resizing:
            return
        self._on_resize_motion(x_root, y_root)
        self._is_resizing = False
        self._apply_responsive_scale()

    def _on_header_press(self, event: Any) -> None:
        """供内部与 --self-test 统一调用的标题栏按下入口。"""
        win_x, win_y = self._get_window_screen_origin()
        x_root = int(getattr(event, "x_root", 0))
        y_root = int(getattr(event, "y_root", 0))
        self._drag_start_x = x_root
        self._drag_start_y = y_root
        self._drag_x = x_root - win_x
        self._drag_y = y_root - win_y
        self._drag_moved = False
        self._drag_in_header = True
        self._is_dragging = True

    def _on_drag_motion(self, event: Any) -> None:
        """供内部与 --self-test 统一调用的拖拽移动入口。"""
        if not self._is_dragging:
            return
        x_root = int(getattr(event, "x_root", 0))
        y_root = int(getattr(event, "y_root", 0))
        dx = abs(x_root - self._drag_start_x)
        dy = abs(y_root - self._drag_start_y)
        if not self._drag_moved and dx <= 4 and dy <= 4:
            return

        self._drag_moved = True
        self._user_moved_window = True
        new_x = x_root - self._drag_x
        new_y = y_root - self._drag_y
        self._move_window_fast(new_x, new_y)

    def _on_header_release(self, event: Any) -> None:
        """供内部与 --self-test 统一调用的释放入口。"""
        if not self._is_dragging:
            return
        was_header = self._drag_in_header
        was_moved = self._drag_moved
        self._is_dragging = False
        self._drag_in_header = False
        if was_moved:
            x_root = int(getattr(event, "x_root", 0))
            y_root = int(getattr(event, "y_root", 0))
            self._move_window_fast(x_root - self._drag_x, y_root - self._drag_y)
        elif was_header:
            self.cycle_card()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            local_pos = event.position().toPoint()
            g_pos = event.globalPosition().toPoint()
            # 右下角 18x18 区域直接触发窗口缩放
            if local_pos.x() >= self.width() - 18 and local_pos.y() >= self.height() - 18:
                self._on_resize_press(g_pos.x(), g_pos.y())
                event.accept()
                return

            # 位于左侧列表区域内部时不启动整窗拖拽
            if self._sidebar_qframe.geometry().contains(self.body_frame.mapFrom(self, local_pos)):
                super().mousePressEvent(event)
                return

            win_x, win_y = self._get_window_screen_origin()
            self._drag_start_x = g_pos.x()
            self._drag_start_y = g_pos.y()
            self._drag_x = g_pos.x() - win_x
            self._drag_y = g_pos.y() - win_y
            self._drag_moved = False
            self._drag_in_header = self.header.geometry().contains(local_pos)
            self._is_dragging = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._is_resizing and (event.buttons() & Qt.MouseButton.LeftButton):
            g_pos = event.globalPosition().toPoint()
            self._on_resize_motion(g_pos.x(), g_pos.y())
            event.accept()
            return
        if self._is_dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            g_pos = event.globalPosition().toPoint()

            class _Pt:
                x_root = g_pos.x()
                y_root = g_pos.y()

            self._on_drag_motion(_Pt())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._is_resizing:
            g_pos = event.globalPosition().toPoint()
            self._on_resize_release(g_pos.x(), g_pos.y())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._is_dragging:
            g_pos = event.globalPosition().toPoint()

            class _Pt:
                x_root = g_pos.x()
                y_root = g_pos.y()

            self._on_header_release(_Pt())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event: Any) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            f"""
            QMenu {{
                background-color: {self.BG_CARD};
                color: {self.FG_PRIMARY};
                border: 1px solid {self.BORDER_COLOR};
                font-family: "Microsoft YaHei UI";
                font-size: 9pt;
            }}
            QMenu::item {{
                padding: 5px 18px;
            }}
            QMenu::item:selected {{
                background-color: {self.FG_ACCENT};
                color: {self.BG_MAIN};
            }}
            """
        )
        act_reset = QAction("返回最新活跃会话", self)
        act_reset.triggered.connect(self._reset_pinned_session)
        menu.addAction(act_reset)

        act_reset_geo = QAction("重置窗口大小与位置", self)
        act_reset_geo.triggered.connect(self._reset_window_geometry)
        menu.addAction(act_reset_geo)

        menu.addSeparator()
        act_export_csv = QAction("导出历史为 CSV...", self)
        act_export_csv.triggered.connect(lambda: self.export_history_to_csv(None))
        menu.addAction(act_export_csv)

        act_settings = QAction("⚙ 设置...", self)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)

        act_refresh = QAction("立即刷新", self)
        act_refresh.triggered.connect(self.rescan_now)
        menu.addAction(act_refresh)

        act_hide = QAction("显示 / 隐藏悬浮窗 (Ctrl+Alt+T)", self)
        act_hide.triggered.connect(self.toggle_window_visibility)
        menu.addAction(act_hide)

        act_close = QAction("关闭悬浮窗 (Esc)", self)
        act_close.triggered.connect(self.close)
        menu.addAction(act_close)

        menu.exec(event.globalPos())

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # 数据渲染与后台异步线程更新
    # ------------------------------------------------------------------
    def _populate_agent_view(
        self,
        view: dict[str, Any],
        session_data: dict[str, Any] | None,
        now_ts: float,
        id_key: str,
        default_model: str,
        empty_text: str,
        is_pinned: bool = False,
    ) -> str:
        if "back_active_lbl" in view:
            view["back_active_lbl"].setVisible(is_pinned)

        if not session_data:
            view["_raw_session_name"] = "无"
            view["status_lbl"].setText(f"[{empty_text}]")
            view["status_lbl"].setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")
            view["session_lbl"].setText("会话: 无")
            return "idle"

        status = str(session_data.get("status") or "idle")
        raw_id = str(session_data.get(id_key) or "-")
        short_id = raw_id[:8] if len(raw_id) > 8 else raw_id
        session_name = str(session_data.get("session_name") or short_id or "无")
        raw_model = str(session_data.get("model") or default_model)

        st = float(session_data.get("start_time") or session_data.get("last_updated") or now_ts)
        end_ref = now_ts if status in ("running", "active") else float(session_data.get("last_updated") or now_ts)
        dur_str = format_duration(end_ref - st)

        steps = int(session_data.get("steps", 0))
        t_calls = int(session_data.get("tool_calls_count", 0))
        last_tool = str(session_data.get("last_tool") or "无")
        stream_est = int(session_data.get("stream_est_tokens", 0))

        in_tok = int(session_data.get("input_tokens", 0))
        cache_tok = int(session_data.get("cached_tokens", 0))
        out_tok = int(session_data.get("output_tokens", 0)) + stream_est
        thought_tok = int(session_data.get("thought_tokens", 0))
        tot_tok = int(session_data.get("total_tokens", 0)) + stream_est
        ctx_tok = int(session_data.get("context_tokens", 0))

        if is_pinned:
            view["status_lbl"].setText("[历史回看]")
            view["status_lbl"].setStyleSheet(f"color: {self.FG_YELLOW}; background: transparent; border: none;")
        elif status == "running":
            est_tag = f" (+~{stream_est})" if stream_est > 0 else ""
            view["status_lbl"].setText(f"[执行中{est_tag}]")
            view["status_lbl"].setStyleSheet(f"color: {self.FG_GREEN}; background: transparent; border: none;")
        elif status == "active":
            view["status_lbl"].setText("[刚更新]")
            view["status_lbl"].setStyleSheet(f"color: {self.FG_YELLOW}; background: transparent; border: none;")
        else:
            view["status_lbl"].setText("[已完成]")
            view["status_lbl"].setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")

        view["_raw_session_name"] = session_name
        view["_raw_short_id"] = short_id
        view["_raw_model"] = raw_model
        view["_raw_dur_str"] = dur_str
        view["_raw_steps"] = steps
        view["_raw_t_calls"] = t_calls
        view["_raw_last_tool"] = last_tool

        view["total_val"].setText(format_num(tot_tok))
        view["ctx_val"].setText(format_num(ctx_tok))

        cache_pct = int(round(cache_tok * 100.0 / in_tok)) if in_tok > 0 else 0
        view["lbl_in"].setText(format_num(in_tok))
        view["lbl_cache"].setText(f"{format_num(cache_tok)} ({cache_pct}%)")
        view["lbl_out"].setText(format_num(out_tok))
        view["lbl_thought"].setText(format_num(thought_tok))

        return status

    def is_render_paused(self) -> bool:
        """判断当前窗口是否处于最小化或隐藏状态，需暂停界面重绘。"""
        return bool(self.isMinimized() or not self.isVisible())

    def _evaluate_adaptive_polling(
        self,
        now_ts: float | None = None,
        snap: dict[str, Any] | None = None,
    ) -> int:
        """
        自适应待机（Adaptive Polling Backoff）：
        - 若检测到任何文件有新写入，立即更新 last_activity_time 并切回配置的原始高速周期（如 1000ms）；
        - 若连续超过 30 秒所有被监控会话均无 mtime / 大小变动，定时器周期自动平滑退避至 3000ms 待机状态。
        """
        cur_now = float(now_ts) if now_ts is not None else time.time()
        if isinstance(snap, dict):
            if bool(snap.get("disk_modified", False)):
                act_ts = float(snap.get("last_activity_time", cur_now) or cur_now)
                self.last_activity_time = act_ts
                self.monitor.last_activity_time = act_ts
            else:
                mon_lat = float(getattr(self.monitor, "last_activity_time", self.last_activity_time))
                eff_lat = min(float(self.last_activity_time), mon_lat)
                self.last_activity_time = eff_lat
                self.monitor.last_activity_time = eff_lat

        self.standby_refresh_ms = max(self.STANDBY_REFRESH_MS, int(self.refresh_ms))
        idle_sec = max(0.0, cur_now - float(self.last_activity_time))
        if idle_sec >= float(self.idle_backoff_sec):
            self._is_polling_backoff = True
            target_interval = int(self.standby_refresh_ms)
        else:
            self._is_polling_backoff = False
            target_interval = int(self.refresh_ms)

        if hasattr(self, "_timer") and self._timer.interval() != target_interval:
            self._timer.setInterval(target_interval)
        return target_interval

    def _resume_render_if_needed(self) -> None:
        """当窗口从最小化或隐藏状态恢复显示时，立即执行一次全量重绘呈现最新状态。"""
        if self.is_render_paused():
            return
        if self._render_paused or self._pending_render_snap is not None:
            self._render_paused = False
            snap_to_render = (
                self._pending_render_snap
                if self._pending_render_snap is not None
                else getattr(self, "_last_snap", None)
            )
            self._pending_render_snap = None
            if isinstance(snap_to_render, dict):
                self._render_snapshot(snap_to_render)

    def show(self) -> None:
        super().show()
        self._resume_render_if_needed()

    def showNormal(self) -> None:
        super().showNormal()
        self._resume_render_if_needed()

    def hide(self) -> None:
        self._render_paused = True
        super().hide()

    def showMinimized(self) -> None:
        self._render_paused = True
        super().showMinimized()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._resume_render_if_needed()

    def hideEvent(self, event: Any) -> None:
        self._render_paused = True
        super().hideEvent(event)

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if event is not None and event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._render_paused = True
            else:
                self._resume_render_if_needed()

    def _refresh_displayed_cards(self) -> None:
        """使用已缓存的会话数据立即刷新右侧卡片（供点击侧边栏切换时零延迟调用，不触发磁盘读取）。"""
        if not hasattr(self, "_last_snap") or not self._last_snap:
            return
        self._apply_snapshot(self._last_snap)

    def _apply_snapshot(self, snap: dict[str, Any]) -> None:
        """接收采集快照：更新自适应轮询退避状态，并在窗口可见且未最小化时调用 _render_snapshot。"""
        self._last_snap = snap
        now_ts = float(snap.get("timestamp", time.time()) or time.time())
        self._evaluate_adaptive_polling(now_ts=now_ts, snap=snap)

        if self.is_render_paused():
            self._render_paused = True
            self._pending_render_snap = snap
            has_sub = self._has_enabled_sub_agents()
            main_sess = snap.get("main_session")
            active_sub_sess = snap.get("active_session") if has_sub else None
            main_status = str(main_sess.get("status") or "idle") if main_sess else "idle"
            sub_status = str(active_sub_sess.get("status") or "idle") if active_sub_sess else "idle"
            if "running" in (main_status, sub_status) or "active" in (main_status, sub_status):
                self._seen_running = True
                self._idle_since = None
            elif self._seen_running and self._idle_since is None:
                self._idle_since = now_ts
            if self.auto_close_idle_sec > 0 and self._idle_since is not None:
                if (now_ts - self._idle_since) >= self.auto_close_idle_sec:
                    self.close()
            return

        self._render_paused = False
        self._pending_render_snap = None
        self._render_snapshot(snap)

    def _render_snapshot(self, snap: dict[str, Any]) -> None:
        """执行全量 Qt 控件重绘更新（当窗口最小化或隐藏时暂停调用）。"""
        self._render_count += 1
        now_ts = float(snap.get("timestamp", time.time()) or time.time())
        has_sub = self._has_enabled_sub_agents()
        prev_has_sub = getattr(self, "_had_enabled_sub_agents", None)
        if prev_has_sub != has_sub:
            self._had_enabled_sub_agents = has_sub
            if has_sub:
                if self.sidebar_filter not in ("all", "main", "sub") or prev_has_sub is False:
                    self.sidebar_filter = "all"
            else:
                self.sidebar_filter = "main"
                self.pinned_sub_file = None
            self._update_filter_btn_styles()

        self._last_main_sessions = snap.get("all_main_sessions") or []
        self._last_sub_sessions = (snap.get("all_sub_sessions") or []) if has_sub else []
        raw_sess_by_agent = snap.get("sessions_by_agent")
        if isinstance(raw_sess_by_agent, dict):
            if has_sub:
                self._last_sessions_by_agent = dict(raw_sess_by_agent)
            else:
                self._last_sessions_by_agent = {"main": list(raw_sess_by_agent.get("main") or self._last_main_sessions)}
        else:
            self._last_sessions_by_agent = {"main": list(self._last_main_sessions)}

        raw_today_by_agent = snap.get("today_by_agent")
        if isinstance(raw_today_by_agent, dict):
            if has_sub:
                self._last_today_by_agent = dict(raw_today_by_agent)
            else:
                self._last_today_by_agent = {"main": dict(raw_today_by_agent.get("main") or {})}
        else:
            self._last_today_by_agent = {}

        main_sess = snap.get("main_session")
        active_sub_sess = snap.get("active_session") if has_sub else None
        today = snap.get("today_stats", {})

        if has_sub:
            header_tot_tok = int(today.get("total_tokens", 0))
            header_today_cost = float(
                today.get("cost_usd", snap.get("today_cost_usd", 0.0)) or 0.0
            )
        else:
            header_tot_tok = int(today.get("main_total_tokens", today.get("total_tokens", 0)))
            header_today_cost = float(
                today.get("main_cost_usd", today.get("cost_usd", snap.get("today_cost_usd", 0.0))) or 0.0
            )
        header_cost_str = format_cost_usd(header_today_cost)
        self._grand_total_compact_text = f"今日累计: {format_compact_tokens(header_tot_tok)} (≈ {header_cost_str})"
        self._grand_total_hover_text = f"今日累计总 Token: {format_num(header_tot_tok)} | 今日折合: ≈ {header_cost_str}"

        self._grand_total_qlabel.setText(
            self._grand_total_hover_text if self._is_hovering_grand_total else self._grand_total_compact_text
        )

        pinned_sess: dict[str, Any] | None = None
        pinned_eng: str | None = None
        if self.pinned_main_file:
            for ms in self._last_main_sessions:
                if str(ms.get("file_path") or "") == self.pinned_main_file:
                    pinned_sess = ms
                    pinned_eng = "main"
                    break
        elif has_sub and self.pinned_sub_file:
            for ss in self._last_sub_sessions:
                if str(ss.get("file_path") or "") == self.pinned_sub_file:
                    pinned_sess = ss
                    pinned_eng = "sub"
                    break

        if not has_sub:
            self.pinned_sub_file = None

        is_any_pinned = pinned_sess is not None
        if self.current_card_idx != 0:
            self.current_card_idx = 0
            self._deck_qwidget.setCurrentIndex(0)

        self.reset_active_btn.setVisible(is_any_pinned)

        # 同步左侧常驻历史会话列表
        self._refresh_sidebar_items()

        # 更新卡片 0：默认显示今日总用量；选中主/子 Agent 对话时显示该条对话的 Token 总量与各项细分指标
        v0 = self.card_views[0]
        main_valid = bool(snap.get("main_agent_valid", False))
        has_any_sessions = bool(self._last_main_sessions or self._last_sub_sessions)

        if is_any_pinned and pinned_sess:
            raw_cid = str(pinned_sess.get("cid") or pinned_sess.get("session_id") or "-")
            short_cid = raw_cid[:8] if len(raw_cid) > 8 else raw_cid
            sess_title = _truncate_session_name(str(pinned_sess.get("session_name") or short_cid), 26)
            if pinned_eng == "sub":
                sub_tag = str(pinned_sess.get("sub_agent_name") or pinned_sess.get("agent_name") or "SubAgent")
                sess_model = str(pinned_sess.get("model") or sub_tag)
            else:
                sess_model = str(pinned_sess.get("model") or "Antigravity")
            sess_steps = int(pinned_sess.get("steps", 0))
            sess_tools = int(pinned_sess.get("tool_calls_count", 0))
            sess_last_tool = str(pinned_sess.get("last_tool") or "无")
            st_ts = float(pinned_sess.get("start_time") or pinned_sess.get("last_updated") or now_ts)
            lu_ts = float(pinned_sess.get("last_updated") or now_ts)
            dur_s = format_duration(max(0.0, lu_ts - st_ts))

            s_in = int(pinned_sess.get("input_tokens", 0))
            s_cache = int(pinned_sess.get("cached_tokens", 0))
            s_out = int(pinned_sess.get("output_tokens", 0))
            s_thought = int(pinned_sess.get("thought_tokens", 0))
            s_tot = int(pinned_sess.get("total_tokens", 0))
            s_ctx = int(pinned_sess.get("context_tokens", 0))
            s_cache_rate = (s_cache * 100.0 / s_in) if s_in > 0 else 0.0
            s_cost_usd = float(
                pinned_sess.get("total_cost_usd", pinned_sess.get("cost_usd", pinned_sess.get("today_cost_usd", 0.0))) or 0.0
            )
            s_today_cost_usd = float(pinned_sess.get("today_cost_usd", s_cost_usd) or 0.0)
            s_cost_str = format_cost_usd(s_cost_usd)
            s_today_cost_str = format_cost_usd(s_today_cost_usd)

            v0["t_lbl"].setText(f"当前选中对话: {sess_title}")
            v0["date_lbl"].setText(format_short_time(lu_ts))
            v0["counts_lbl"].setText(
                f"ID: {short_cid} | 轮数: {sess_steps} 步 ({sess_tools} 次工具) | 耗时: {dur_s}"
            )
            v0["split_lbl"].setText(
                f"模型: {sess_model} | 上下文水位: {format_num(s_ctx)} | 最近工具: {sess_last_tool}"
            )
            v0["lbl_t1"].setText("本对话累计 Token")
            v0["lbl_t2"].setText("对话缓存命中率")
            v0["total_val"].setText(format_num(s_tot))
            if "cost_val" in v0:
                v0["cost_val"].setText(f"本会话折合: ≈ {s_cost_str} (今日: ≈ {s_today_cost_str})")
            v0["cache_rate_val"].setText(f"{s_cache_rate:.1f}%")
            v0["lbl_in"].setText(format_num(s_in))
            v0["lbl_cache"].setText(f"{format_num(s_cache)} ({int(round(s_cache_rate))}%)")
            v0["lbl_out"].setText(format_num(s_out))
            v0["lbl_thought"].setText(format_num(s_thought))
            if len(v0.get("grid_cells", [])) == 4:
                v0["grid_cells"][0]["title_lbl"].setText("输入 (Input)")
                v0["grid_cells"][1]["title_lbl"].setText("缓存 (Cached)")
                v0["grid_cells"][2]["title_lbl"].setText("输出 (Output)")
                v0["grid_cells"][3]["title_lbl"].setText("思考 (Thought)")
        else:
            m_sess_c = int(today.get("main_sessions_count", today.get("sessions_count", 0)))
            m_step_c = int(today.get("main_steps_count", today.get("steps_count", 0)))
            m_tot_tok = int(today.get("main_total_tokens", today.get("total_tokens", 0)))
            sub_sess_c = int(today.get("sub_sessions_count", 0))
            sub_step_c = int(today.get("sub_steps_count", 0))
            sub_tot_tok = int(today.get("sub_total_tokens", 0))
            tot_sess_c = int(today.get("sessions_count", m_sess_c))
            tot_step_c = int(today.get("steps_count", m_step_c))

            t_in = int(today.get("input_tokens", 0))
            t_cache = int(today.get("cached_tokens", 0))
            t_out = int(today.get("output_tokens", 0))
            t_thought = int(today.get("thought_tokens", 0))
            t_tot = int(today.get("total_tokens", 0)) if has_sub else m_tot_tok
            t_cache_rate = (t_cache * 100.0 / t_in) if t_in > 0 else 0.0
            if has_sub:
                t_cost_usd = float(today.get("cost_usd", snap.get("today_cost_usd", 0.0)) or 0.0)
            else:
                t_cost_usd = float(
                    today.get("main_cost_usd", today.get("cost_usd", snap.get("today_cost_usd", 0.0))) or 0.0
                )
            t_cost_str = format_cost_usd(t_cost_usd)

            v0["t_lbl"].setText("Token 总用量")
            v0["date_lbl"].setText(datetime.date.today().strftime("%Y-%m-%d"))
            v0["lbl_t1"].setText("今日累计总 Token")
            v0["lbl_t2"].setText("综合缓存命中率")
            if len(v0.get("grid_cells", [])) == 4:
                v0["grid_cells"][0]["title_lbl"].setText("总输入 (Input)")
                v0["grid_cells"][1]["title_lbl"].setText("总缓存 (Cached)")
                v0["grid_cells"][2]["title_lbl"].setText("总输出 (Output)")
                v0["grid_cells"][3]["title_lbl"].setText("总思考 (Thought)")

            if not main_valid and not has_any_sessions:
                v0["counts_lbl"].setText("未检测到会话日志，请点击右上角【⚙ 设置】配置路径")
                v0["split_lbl"].setText("Antigravity: 未检测到有效路径")
            elif has_sub:
                v0["counts_lbl"].setText(
                    f"今日会话: {tot_sess_c} 次 (主 {m_sess_c} / 子 {sub_sess_c}) | 今日步数: {tot_step_c} 步 (主 {m_step_c} / 子 {sub_step_c})"
                )
                v0["split_lbl"].setText(
                    f"Antigravity 今日: {format_num(m_tot_tok)} | 子 Agent 今日: {format_num(sub_tot_tok)} | 历史会话: {len(self._last_main_sessions) + len(self._last_sub_sessions)}"
                )
            else:
                v0["counts_lbl"].setText(
                    f"今日会话: {m_sess_c} 次 | 今日总步数: {m_step_c} 步"
                )
                v0["split_lbl"].setText(
                    f"Antigravity 今日累计: {format_num(m_tot_tok)} | 历史会话总数: {len(self._last_main_sessions)}"
                )
            v0["total_val"].setText(format_num(t_tot))
            if "cost_val" in v0:
                v0["cost_val"].setText(f"今日折合: ≈ {t_cost_str}")
            v0["cache_rate_val"].setText(f"{t_cache_rate:.1f}%")
            v0["lbl_in"].setText(format_num(t_in))
            v0["lbl_cache"].setText(format_num(t_cache))
            v0["lbl_out"].setText(format_num(t_out))
            v0["lbl_thought"].setText(format_num(t_thought))

        if is_any_pinned:
            self._title_qlabel.setText("[当前选中对话]")
        else:
            self._title_qlabel.setText("[Token 总用量]")

        main_status = str(main_sess.get("status") or "idle") if main_sess else "idle"
        sub_status = str(active_sub_sess.get("status") or "idle") if active_sub_sess else "idle"
        combined_running = "running" in (main_status, sub_status)
        combined_active = "active" in (main_status, sub_status)

        # 更新顶部指示灯与空闲自动关闭计时
        if combined_running:
            self.status_dot.setStyleSheet(f"color: {self.FG_GREEN}; background: transparent; border: none;")
        elif combined_active:
            self.status_dot.setStyleSheet(f"color: {self.FG_YELLOW}; background: transparent; border: none;")
        else:
            self.status_dot.setStyleSheet(f"color: {self.FG_MUTED}; background: transparent; border: none;")

        if combined_running or combined_active:
            self._seen_running = True
            self._idle_since = None
        elif self._seen_running and self._idle_since is None:
            self._idle_since = now_ts

        self._adjust_window_geometry()

        if self.auto_close_idle_sec > 0 and self._idle_since is not None:
            if (now_ts - self._idle_since) >= self.auto_close_idle_sec:
                self.close()

    def _update_once(self) -> None:
        """同步采集并更新一次界面（供托盘手动立即刷新、短时运行与 --self-test 同步断言使用）。"""
        snap = self.monitor.collect_snapshot(
            target_session=self.target_session,
            target_project=self.target_project,
        )
        self._apply_snapshot(snap)

    def _schedule_tick(self) -> None:
        """后台工作线程采集快照，主线程零阻塞。"""
        if not self._in_mainloop or self._worker_busy:
            return
        self._worker_busy = True

        def _bg_collect() -> None:
            try:
                snap = self.monitor.collect_snapshot(
                    target_session=self.target_session,
                    target_project=self.target_project,
                )
                if self._in_mainloop:
                    self._bridge.snapshot_ready.emit(snap)
            except Exception:
                pass
            finally:
                self._worker_busy = False

        threading.Thread(target=_bg_collect, daemon=True).start()

    def run(self, duration_sec: float = 0.0) -> None:
        self._in_mainloop = True
        self._timer.start()
        if duration_sec > 0:
            self._update_once()
            QTimer.singleShot(int(duration_sec * 1000), self.close)
        else:
            QTimer.singleShot(0, self._schedule_tick)
        self._app.exec()

    def closeEvent(self, event: Any) -> None:
        self._cleanup_on_exit()
        super().closeEvent(event)

    def _cleanup_on_exit(self) -> None:
        self._in_mainloop = False
        if hasattr(self, "_timer"):
            self._timer.stop()
        try:
            if hasattr(self, "monitor") and self.monitor is not None:
                self.monitor.save_history()
        except Exception:
            pass
        self._unregister_global_hotkey()
        if getattr(self, "tray_icon", None) is not None:
            try:
                self.tray_icon.hide()
            except Exception:
                pass

    def close(self) -> bool:
        self._cleanup_on_exit()
        res = super().close()
        if self._app:
            self._app.quit()
        return res
