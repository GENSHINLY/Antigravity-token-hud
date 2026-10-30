# -*- coding: utf-8 -*-
"""
hud_core.settings_dialog - SettingsDialog、实时主题同步与子 Agent 状态探针。
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import (
    DEFAULT_CONFIG,
    GEMINI_TMP_ROOT,
    THEME_PRESETS,
    ConfigManager,
    _HEX_COLOR_RE,
    _probe_antigravity_brain_dir,
    auto_detect_antigravity_path,
)
from .monitor import TokenMonitorCache
from .parsers import PARSER_REGISTRY
from .pricing import (
    DEFAULT_MODEL_PRICING_PER_1M,
    DEFAULT_USD_TO_CNY_RATE,
    MODEL_PRICING_PRESETS,
    TokenPricingEngine,
    fetch_online_usd_rate,
    fetch_remote_pricing_table,
    get_default_pricing_config,
    merge_remote_pricing_into_config,
)
from .ui_widgets import _HoverLabel, _derive_theme_palette
from .win32_dwm import apply_win11_mica_and_rounded_corners


class _OnlinePricingBridge(QObject):
    """用于将后台线程拉取到的汇率与远程价格表结果安全投递回设置面板主线程。"""

    fetch_finished = Signal(bool, str, float, int)


def _hex_to_rgba_css(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    if not c.isValid():
        return hex_color
    a_clamped = max(0.0, min(1.0, float(alpha)))
    if a_clamped >= 0.999:
        return hex_color
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {a_clamped:.2f})"


def _probe_sub_agent_status(
    path_str: str, parser_type: str = "gemini_cli_session"
) -> tuple[str, str]:
    """探测子 Agent 路径的会话文件状态，返回 (状态文本, 颜色HEX)。"""
    clean_path = str(path_str or "").strip()
    if not clean_path:
        return ("🔴 路径无效", "#EA6962")
    try:
        raw_p = Path(clean_path)
        expanded_p = raw_p.expanduser()
        if not expanded_p.exists():
            return ("🔴 路径无效", "#EA6962")
        files = TokenMonitorCache._discover_sub_agent_files(raw_p, parser_type)
        if files:
            return (f"🟢 {len(files)} 个会话", "#A9B665")
        return ("🟡 0 个会话", "#D8A657")
    except Exception:
        return ("🔴 路径无效", "#EA6962")


class SettingsDialog(QDialog):
    """
    Token HUD 设置面板：
    1. Agents 监控源管理（主 Agent 路径配置/浏览/自动检测/状态指示 + 子 Agent 列表增删改）
    2. Appearance 外观与配色（预设主题切换 + 5 项核心 HEX 颜色与拾色器）
    3. Performance 性能与频次（刷新间隔 250~5000ms + 目录扫描 TTL 1.0~10.0s）
    """

    COLOR_FIELD_SPECS: list[tuple[str, str]] = [
        ("bg_color", "主背景色 (bg_color)"),
        ("fg_color", "主文字色 (fg_color)"),
        ("accent_color", "强调高亮色 (accent_color)"),
        ("card_bg_color", "卡片背景色 (card_bg_color)"),
        ("border_color", "边框颜色 (border_color)"),
    ]

    BACKDROP_OPTIONS: list[tuple[str, str]] = [
        ("Acrylic (强高斯模糊·透射窗口)", "acrylic"),
        ("Mica (云母·柔和壁纸模糊)", "mica"),
        ("Mica Alt (深邃云母·高对比度)", "mica_alt"),
        ("关闭 (无模糊·纯半透明/实色)", "none"),
    ]

    def __init__(self, parent: QWidget | None = None, config_mgr: ConfigManager | None = None):
        super().__init__(parent)
        self._hud_parent = parent
        self.hud_window = parent
        if config_mgr is not None:
            self.config_mgr = config_mgr
        elif parent is not None and hasattr(parent, "config_mgr") and parent.config_mgr is not None:
            self.config_mgr = parent.config_mgr
        else:
            self.config_mgr = ConfigManager()

        self.setObjectName("settingsDialogRoot")
        self.setWindowTitle("Token HUD 设置")
        self.setMinimumSize(620, 450)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self._is_dragging = False
        self._drag_start = QPoint(0, 0)
        self.dwm_info: dict[str, Any] = {}
        theme_cfg = self.config_mgr.get("theme", {})
        if not isinstance(theme_cfg, dict):
            theme_cfg = {}
        try:
            self.alpha = float(theme_cfg.get("opacity", 0.94) or 0.94)
            self.alpha = max(0.2, min(1.0, self.alpha))
        except Exception:
            self.alpha = 0.94
        try:
            self.bg_opacity = float(theme_cfg.get("bg_opacity", 0.65) or 0.65)
            self.bg_opacity = max(0.05, min(1.0, self.bg_opacity))
        except Exception:
            self.bg_opacity = 0.65
        raw_bd = str(theme_cfg.get("backdrop") or "mica").strip().lower()
        self.backdrop_type = raw_bd if raw_bd in ("mica", "acrylic", "mica_alt", "none") else "mica"

        self._loading_config = False
        self._updating_preset_fields = False
        self._updating_sub_table = False
        self._updating_pricing_table = False
        self._fetching_online_pricing = False
        self._online_bridge = _OnlinePricingBridge(self)
        self._online_bridge.fetch_finished.connect(self._on_online_fetch_finished)
        self._last_opacity_render_time: float = 0.0
        self._pending_opacity_val: float | None = None
        self._opacity_throttle_timer = QTimer(self)
        self._opacity_throttle_timer.setSingleShot(True)
        self._opacity_throttle_timer.timeout.connect(self._flush_pending_opacity_render)
        self._theme_debounce_timer = QTimer(self)
        self._theme_debounce_timer.setSingleShot(True)
        self._theme_debounce_timer.timeout.connect(self._persist_theme_debounced)
        self._dialog_qss_dirty_from_opacity: bool = False
        self._build_dialog_ui()
        self._apply_dialog_stylesheet()
        self._load_from_config()

    @staticmethod
    def _probe_sub_agent_status(
        path_str: str, parser_type: str = "gemini_cli_session"
    ) -> tuple[str, str]:
        """探测子 Agent 路径的会话文件状态，返回 (状态文本, 颜色HEX)。"""
        return _probe_sub_agent_status(path_str, parser_type)

    def _update_bg_opacity_colors(
        self, opacity: float | None = None, pal: dict[str, str] | None = None
    ) -> tuple[str, str, str, str]:
        if opacity is not None:
            try:
                self.bg_opacity = max(0.05, min(1.0, float(opacity)))
            except Exception:
                self.bg_opacity = 0.65

        if pal is None:
            theme_cfg = self.config_mgr.get("theme", {})
            if not isinstance(theme_cfg, dict):
                theme_cfg = {}
            pal = _derive_theme_palette(theme_cfg)

        bg_main = pal["BG_MAIN"]
        bg_card = pal["BG_CARD"]
        bg_inner = pal["BG_INNER"]
        border_col = pal["BORDER_COLOR"]

        alpha_card = self.bg_opacity
        alpha_main = max(0.02, self.bg_opacity * 0.55)

        if self.backdrop_type != "none" or self.bg_opacity < 1.0:
            bg_main_css = _hex_to_rgba_css(bg_main, alpha_main)
            bg_header_css = _hex_to_rgba_css(bg_main, alpha_card)
            bg_card_css = _hex_to_rgba_css(bg_card, alpha_card)
            bg_inner_css = _hex_to_rgba_css(bg_inner, alpha_card)
            eff_alpha_main = int(round(alpha_main * 255))
            eff_alpha_card = int(round(alpha_card * 255))
        else:
            bg_main_css = bg_main
            bg_header_css = bg_main
            bg_card_css = bg_card
            bg_inner_css = bg_inner
            eff_alpha_main = 255
            eff_alpha_card = 255

        c_main = QColor(bg_main)
        c_main.setAlpha(eff_alpha_main)
        self._bg_main_qcolor = c_main

        c_card = QColor(bg_card)
        c_card.setAlpha(eff_alpha_card)
        self._bg_card_qcolor = c_card

        self._border_qcolor = QColor(border_col)
        return bg_main_css, bg_header_css, bg_card_css, bg_inner_css

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bg_main_color = getattr(self, "_bg_main_qcolor", QColor(20, 22, 23, 91))
        bg_card_color = getattr(self, "_bg_card_qcolor", QColor(29, 32, 33, 166))
        border_color = getattr(self, "_border_qcolor", QColor("#282828"))

        outer_rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setBrush(bg_main_color)
        painter.setPen(QPen(border_color, 1))
        painter.drawRoundedRect(outer_rect, 6, 6)

        if hasattr(self, "tabs") and self.tabs.isVisible():
            tb_h = self.tabs.tabBar().height()
            y_off = max(0, tb_h - 1)
            pane_top_left = self.tabs.mapTo(self, QPoint(0, y_off))
            pane_rect = QRect(
                pane_top_left.x(),
                pane_top_left.y(),
                max(0, self.tabs.width() - 1),
                max(0, self.tabs.height() - y_off - 1),
            )
            painter.setBrush(bg_card_color)
            painter.setPen(QPen(border_color, 1))
            painter.drawRoundedRect(pane_rect, 4, 4)

    def apply_bg_opacity(self, opacity: float | None = None) -> None:
        """轻量热更新对话框背景透明度：绝不调用 DWM API，不重解析全量 QSS，仅更新 QPainter 画布颜色与顶栏。"""
        _, bg_header_css, _, _ = self._update_bg_opacity_colors(opacity)
        if hasattr(self, "header"):
            self.header.setStyleSheet(
                f"background-color: {bg_header_css}; border: none; border-top-left-radius: 5px; border-top-right-radius: 5px;"
            )
        self._dialog_qss_dirty_from_opacity = True
        self.update()

    def _apply_dialog_stylesheet(self, apply_dwm: bool = True) -> None:
        theme_cfg = self.config_mgr.get("theme", {})
        if not isinstance(theme_cfg, dict):
            theme_cfg = {}
        try:
            self.alpha = float(theme_cfg.get("opacity", 0.94) or 0.94)
            self.alpha = max(0.2, min(1.0, self.alpha))
        except Exception:
            self.alpha = 0.94
        try:
            self.bg_opacity = float(theme_cfg.get("bg_opacity", 0.65) or 0.65)
            self.bg_opacity = max(0.05, min(1.0, self.bg_opacity))
        except Exception:
            self.bg_opacity = 0.65
        raw_bd = str(theme_cfg.get("backdrop") or "mica").strip().lower()
        self.backdrop_type = raw_bd if raw_bd in ("mica", "acrylic", "mica_alt", "none") else "mica"

        pal = _derive_theme_palette(theme_cfg)
        bg_main = pal["BG_MAIN"]
        bg_hover = pal["BG_HOVER"]
        fg_primary = pal["FG_PRIMARY"]
        fg_muted = pal["FG_MUTED"]
        fg_accent = pal["FG_ACCENT"]
        border_col = pal["BORDER_COLOR"]

        _, bg_header_css, bg_card_css, bg_inner_css = self._update_bg_opacity_colors(
            self.bg_opacity, pal=pal
        )
        self._dialog_qss_dirty_from_opacity = False

        self.setWindowOpacity(self.alpha)
        if apply_dwm and (
            getattr(self, "_applied_dwm_backdrop", None) != self.backdrop_type
            or not getattr(self, "dwm_info", None)
        ):
            self.dwm_info = apply_win11_mica_and_rounded_corners(
                self, backdrop_type=self.backdrop_type
            )
            self._applied_dwm_backdrop = self.backdrop_type

        if hasattr(self, "header"):
            self.header.setStyleSheet(
                f"background-color: {bg_header_css}; border: none; border-top-left-radius: 5px; border-top-right-radius: 5px;"
            )
        if hasattr(self, "dialog_title_lbl"):
            self.dialog_title_lbl.setStyleSheet(
                f"color: {fg_primary}; background: transparent; border: none;"
            )
        if hasattr(self, "header_close_btn"):
            self.header_close_btn.setStyleSheet(
                f"""
                QLabel {{
                    color: {fg_muted};
                    background: transparent;
                    border: none;
                    padding: 2px 8px;
                }}
                QLabel:hover {{
                    color: {fg_accent};
                }}
                """
            )

        self.setStyleSheet(
            f"""
            QDialog#settingsDialogRoot, SettingsDialog {{
                background-color: transparent;
                color: {fg_primary};
                font-family: "Microsoft YaHei UI";
                font-size: 9pt;
                border: none;
            }}
            QLabel {{
                color: {fg_primary};
                background: transparent;
            }}
            QTabWidget::pane {{
                background-color: transparent;
                border: 1px solid {border_col};
                border-radius: 4px;
            }}
            QTabBar::tab {{
                background-color: {bg_inner_css};
                color: {fg_muted};
                border: 1px solid {border_col};
                border-bottom: none;
                padding: 6px 14px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-weight: bold;
            }}
            QTabBar::tab:selected {{
                background-color: {bg_card_css};
                color: {fg_accent};
            }}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
                background-color: {bg_inner_css};
                color: {fg_primary};
                border: 1px solid {border_col};
                border-radius: 3px;
                padding: 4px 6px;
                selection-background-color: {fg_accent};
                selection-color: {bg_main};
            }}
            QComboBox QAbstractItemView {{
                background-color: {bg_card_css};
                color: {fg_primary};
                border: 1px solid {border_col};
                selection-background-color: {fg_accent};
                selection-color: {bg_main};
            }}
            QPushButton {{
                background-color: {bg_inner_css};
                color: {fg_primary};
                border: 1px solid {border_col};
                border-radius: 3px;
                padding: 5px 12px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {bg_hover};
                border-color: {fg_accent};
                color: {fg_accent};
            }}
            QPushButton#primarySaveBtn {{
                background-color: {fg_accent};
                color: {bg_main};
                border: 1px solid {fg_accent};
            }}
            QPushButton#primarySaveBtn:hover {{
                background-color: {fg_primary};
                border-color: {fg_primary};
                color: {bg_main};
            }}
            QTableWidget {{
                background-color: {bg_inner_css};
                color: {fg_primary};
                gridline-color: {border_col};
                border: 1px solid {border_col};
                border-radius: 3px;
                selection-background-color: {bg_hover};
                selection-color: {fg_accent};
            }}
            QHeaderView::section {{
                background-color: {bg_card_css};
                color: {fg_muted};
                border: 1px solid {border_col};
                padding: 4px 6px;
                font-weight: bold;
            }}
            """
        )
        self.update()

    def _build_dialog_ui(self) -> None:
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(1, 1, 1, 1)
        root_lay.setSpacing(0)

        self.header = QFrame(self)
        self.header.setFixedHeight(34)
        header_lay = QHBoxLayout(self.header)
        header_lay.setContentsMargins(12, 0, 8, 0)
        self.dialog_title_lbl = QLabel("Token HUD 设置", self.header)
        self.dialog_title_lbl.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Bold))
        header_lay.addWidget(self.dialog_title_lbl, 0)
        header_lay.addStretch(1)
        self.header_close_btn = _HoverLabel("✕", self.header)
        self.header_close_btn.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.header_close_btn.clicked.connect(self.accept)
        header_lay.addWidget(self.header_close_btn, 0)
        root_lay.addWidget(self.header, 0)

        self.dialog_body = QWidget(self)
        body_lay = QVBoxLayout(self.dialog_body)
        body_lay.setContentsMargins(12, 10, 12, 12)
        body_lay.setSpacing(10)

        self.tabs = QTabWidget(self.dialog_body)
        body_lay.addWidget(self.tabs, 1)

        # ------------------------------------------------------------------
        # Tab 1: Agents 监控源管理
        # ------------------------------------------------------------------
        tab_agents = QWidget(self.dialog_body)
        agents_lay = QVBoxLayout(tab_agents)
        agents_lay.setContentsMargins(12, 12, 12, 12)
        agents_lay.setSpacing(10)

        main_title = QLabel("主 Agent (Antigravity) 监控路径：", tab_agents)
        main_title.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Bold))
        agents_lay.addWidget(main_title, 0)

        main_row = QHBoxLayout()
        main_row.setSpacing(6)
        self.main_path_edit = QLineEdit(tab_agents)
        self.main_path_edit.setPlaceholderText("留空将自动探测 ~/.gemini/antigravity/brain")
        self.main_path_edit.textChanged.connect(self._on_main_path_changed)
        main_row.addWidget(self.main_path_edit, 1)

        self.browse_main_btn = QPushButton("浏览", tab_agents)
        self.browse_main_btn.clicked.connect(self._on_browse_main_path)
        main_row.addWidget(self.browse_main_btn, 0)

        self.detect_main_btn = QPushButton("自动检测", tab_agents)
        self.detect_main_btn.clicked.connect(self._on_auto_detect_main_path)
        main_row.addWidget(self.detect_main_btn, 0)
        agents_lay.addLayout(main_row, 0)

        self.main_status_lbl = QLabel("🟢 已找到 0 个会话", tab_agents)
        self.main_status_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        agents_lay.addWidget(self.main_status_lbl, 0)

        self.sub_section_widget = QWidget(tab_agents)
        sub_sec_lay = QVBoxLayout(self.sub_section_widget)
        sub_sec_lay.setContentsMargins(0, 0, 0, 0)
        sub_sec_lay.setSpacing(6)

        sub_head_row = QHBoxLayout()
        sub_title = QLabel("子 Agent 监控源列表（无启用项时主界面自动保持单 Agent 极简模式）：", self.sub_section_widget)
        sub_title.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Bold))
        sub_head_row.addWidget(sub_title, 1)

        self.add_sub_btn = QPushButton("+ 添加", self.sub_section_widget)
        self.add_sub_btn.clicked.connect(lambda: self.add_sub_agent_row())
        sub_head_row.addWidget(self.add_sub_btn, 0)

        self.auto_scan_sub_btn = QPushButton("自动扫描", self.sub_section_widget)
        self.auto_scan_sub_btn.clicked.connect(self._on_auto_scan_sub_agents)
        sub_head_row.addWidget(self.auto_scan_sub_btn, 0)

        self.browse_sub_btn = QPushButton("浏览路径", self.sub_section_widget)
        self.browse_sub_btn.clicked.connect(self._on_browse_sub_path)
        sub_head_row.addWidget(self.browse_sub_btn, 0)

        self.remove_sub_btn = QPushButton("删除", self.sub_section_widget)
        self.remove_sub_btn.clicked.connect(self.remove_selected_sub_agent_row)
        sub_head_row.addWidget(self.remove_sub_btn, 0)
        sub_sec_lay.addLayout(sub_head_row, 0)

        self.sub_table = QTableWidget(0, 5, self.sub_section_widget)
        self.sub_table.setHorizontalHeaderLabels(
            ["启用", "名称", "监控路径", "解析器 (parser_type)", "探测状态 / 实时状态"]
        )
        self.sub_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sub_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sub_table.verticalHeader().setVisible(False)
        hdr = self.sub_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.sub_table.setColumnWidth(1, 105)
        self.sub_table.itemChanged.connect(self._on_sub_table_item_changed)
        sub_sec_lay.addWidget(self.sub_table, 1)

        agents_lay.addWidget(self.sub_section_widget, 1)

        self.tabs.addTab(tab_agents, "Agents 监控源")

        # ------------------------------------------------------------------
        # Tab 2: Appearance 外观与配色
        # ------------------------------------------------------------------
        tab_appear = QWidget(self.dialog_body)
        appear_lay = QVBoxLayout(tab_appear)
        appear_lay.setContentsMargins(14, 14, 14, 14)
        appear_lay.setSpacing(10)

        form_appear = QFormLayout()
        form_appear.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form_appear.setHorizontalSpacing(12)
        form_appear.setVerticalSpacing(10)

        self.preset_combo = QComboBox(tab_appear)
        for preset_name in THEME_PRESETS.keys():
            self.preset_combo.addItem(preset_name)
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        form_appear.addRow("预设主题：", self.preset_combo)

        self.backdrop_combo = QComboBox(tab_appear)
        for label_text, backdrop_key in self.BACKDROP_OPTIONS:
            self.backdrop_combo.addItem(label_text, backdrop_key)
        self.backdrop_combo.currentIndexChanged.connect(self._on_backdrop_changed)
        form_appear.addRow("毛玻璃效果：", self.backdrop_combo)

        bg_op_container = QWidget(tab_appear)
        bg_op_vbox = QVBoxLayout(bg_op_container)
        bg_op_vbox.setContentsMargins(0, 0, 0, 0)
        bg_op_vbox.setSpacing(3)

        bg_op_row = QHBoxLayout()
        bg_op_row.setContentsMargins(0, 0, 0, 0)
        bg_op_row.setSpacing(8)

        self.bg_opacity_slider = QSlider(Qt.Orientation.Horizontal, bg_op_container)
        self.bg_opacity_slider.setRange(5, 100)
        self.bg_opacity_slider.setSingleStep(5)
        self.bg_opacity_slider.setPageStep(10)
        self.bg_opacity_slider.setValue(int(round(self.bg_opacity * 100)))
        self.bg_opacity_slider.valueChanged.connect(self._on_bg_opacity_changed)
        bg_op_row.addWidget(self.bg_opacity_slider, 1)

        self.bg_opacity_val_label = QLabel(
            f"{int(round(self.bg_opacity * 100))}%", bg_op_container
        )
        self.bg_opacity_val_label.setFixedWidth(42)
        self.bg_opacity_val_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.bg_opacity_val_label.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        bg_op_row.addWidget(self.bg_opacity_val_label, 0)

        bg_op_vbox.addLayout(bg_op_row)

        self.bg_opacity_hint_label = QLabel(
            "仅调节背景透光度与毛玻璃深浅，前景文字保持清晰", bg_op_container
        )
        self.bg_opacity_hint_label.setFont(QFont("Microsoft YaHei UI", 8))
        bg_op_vbox.addWidget(self.bg_opacity_hint_label)

        form_appear.addRow("背景不透明度：", bg_op_container)

        self.color_edits: dict[str, QLineEdit] = {}
        self.color_pick_btns: dict[str, QPushButton] = {}
        self.color_swatches: dict[str, QLabel] = {}

        for key, label_text in self.COLOR_FIELD_SPECS:
            row_w = QWidget(tab_appear)
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.setSpacing(6)

            swatch = QLabel("", row_w)
            swatch.setFixedSize(20, 20)
            swatch.setStyleSheet("background-color: #141617; border: 1px solid #504945; border-radius: 3px;")
            row_l.addWidget(swatch, 0)

            edit = QLineEdit(row_w)
            edit.setPlaceholderText("#RRGGBB")
            edit.textChanged.connect(lambda txt, k=key: self._on_color_edit_changed(k, txt))
            row_l.addWidget(edit, 1)

            btn = QPushButton("拾色", row_w)
            btn.clicked.connect(lambda _checked=False, k=key: self._on_pick_color(k))
            row_l.addWidget(btn, 0)

            self.color_edits[key] = edit
            self.color_pick_btns[key] = btn
            self.color_swatches[key] = swatch
            form_appear.addRow(f"{label_text}：", row_w)

        appear_lay.addLayout(form_appear, 0)
        appear_lay.addStretch(1)
        self.tabs.addTab(tab_appear, "Appearance 外观")

        # ------------------------------------------------------------------
        # Tab 3: Performance 性能与频次
        # ------------------------------------------------------------------
        tab_perf = QWidget(self.dialog_body)
        perf_lay = QVBoxLayout(tab_perf)
        perf_lay.setContentsMargins(14, 14, 14, 14)
        perf_lay.setSpacing(12)

        form_perf = QFormLayout()
        form_perf.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form_perf.setHorizontalSpacing(12)
        form_perf.setVerticalSpacing(12)

        self.poll_spin = QSpinBox(tab_perf)
        self.poll_spin.setRange(250, 5000)
        self.poll_spin.setSingleStep(250)
        self.poll_spin.setSuffix(" ms")
        self.poll_spin.valueChanged.connect(self._on_perf_changed)
        form_perf.addRow("界面刷新频次 (250 ~ 5000ms)：", self.poll_spin)

        self.ttl_spin = QDoubleSpinBox(tab_perf)
        self.ttl_spin.setRange(1.0, 10.0)
        self.ttl_spin.setSingleStep(0.5)
        self.ttl_spin.setDecimals(1)
        self.ttl_spin.setSuffix(" s")
        self.ttl_spin.valueChanged.connect(self._on_perf_changed)
        form_perf.addRow("目录扫描缓存 TTL (1.0 ~ 10.0s)：", self.ttl_spin)

        perf_lay.addLayout(form_perf, 0)
        perf_hint = QLabel(
            "说明：后台采用独立工作线程增量解析 JSONL 日志，调低刷新间隔可提升实时性，调高扫描 TTL 可减少磁盘目录遍历次数。",
            tab_perf,
        )
        perf_hint.setWordWrap(True)
        perf_hint.setStyleSheet("color: #928374; background: transparent; border: none;")
        perf_lay.addWidget(perf_hint, 0)
        perf_lay.addStretch(1)
        self.tabs.addTab(tab_perf, "Performance 性能")

        # ------------------------------------------------------------------
        # Tab 4: Model Pricing 模型定价与汇率设置
        # ------------------------------------------------------------------
        tab_pricing = QWidget(self.dialog_body)
        pricing_tab_lay = QVBoxLayout(tab_pricing)
        pricing_tab_lay.setContentsMargins(0, 0, 0, 0)
        pricing_tab_lay.setSpacing(0)

        self.pricing_scroll = QScrollArea(tab_pricing)
        self.pricing_scroll.setWidgetResizable(True)
        self.pricing_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.pricing_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pricing_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pricing_scroll.setStyleSheet("background: transparent; border: none;")

        self.pricing_section_widget = QWidget(self.pricing_scroll)
        self.pricing_section_widget.setStyleSheet("background: transparent; border: none;")
        pricing_lay = QVBoxLayout(self.pricing_section_widget)
        pricing_lay.setContentsMargins(12, 12, 12, 12)
        pricing_lay.setSpacing(10)

        pricing_head_row = QHBoxLayout()
        pricing_head_row.setSpacing(8)
        self.pricing_title_lbl = QLabel(
            "模型定价与汇率设置（Model Pricing）：", self.pricing_section_widget
        )
        self.pricing_title_lbl.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Bold))
        pricing_head_row.addWidget(self.pricing_title_lbl, 0)
        pricing_head_row.addStretch(1)

        rate_lbl = QLabel("USD -> CNY 汇率：", self.pricing_section_widget)
        rate_lbl.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Bold))
        pricing_head_row.addWidget(rate_lbl, 0)

        self.usd_to_cny_spin = QDoubleSpinBox(self.pricing_section_widget)
        self.usd_to_cny_spin.setRange(0.01, 1000.0)
        self.usd_to_cny_spin.setSingleStep(0.05)
        self.usd_to_cny_spin.setDecimals(2)
        self.usd_to_cny_spin.setValue(DEFAULT_USD_TO_CNY_RATE)
        self.usd_to_cny_spin.valueChanged.connect(self._on_pricing_changed)
        self.rate_spin = self.usd_to_cny_spin
        pricing_head_row.addWidget(self.usd_to_cny_spin, 0)

        self.reset_pricing_btn = QPushButton("重置为默认费率", self.pricing_section_widget)
        self.reset_pricing_btn.setToolTip("恢复所有官方预设模型的默认单价与默认汇率")
        self.reset_pricing_btn.clicked.connect(self.reset_pricing_to_defaults)
        pricing_head_row.addWidget(self.reset_pricing_btn, 0)

        pricing_lay.addLayout(pricing_head_row, 0)

        pricing_net_row = QHBoxLayout()
        pricing_net_row.setSpacing(8)
        self.auto_update_pricing_chk = QCheckBox(
            "启动时自动联网同步汇率与最新模型费率", self.pricing_section_widget
        )
        self.auto_update_pricing_chk.setChecked(True)
        self.auto_update_pricing_chk.stateChanged.connect(self._on_auto_update_pricing_changed)
        self.auto_update_pricing = self.auto_update_pricing_chk
        pricing_net_row.addWidget(self.auto_update_pricing_chk, 0)
        pricing_net_row.addStretch(1)

        proxy_lbl = QLabel("网络代理 (留空使用系统代理)：", self.pricing_section_widget)
        pricing_net_row.addWidget(proxy_lbl, 0)

        self.proxy_edit = QLineEdit(self.pricing_section_widget)
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:7890 或 socks5://...")
        self.proxy_edit.setMinimumWidth(190)
        self.proxy_edit.textChanged.connect(self._on_proxy_changed)
        self.proxy = self.proxy_edit
        pricing_net_row.addWidget(self.proxy_edit, 0)
        pricing_lay.addLayout(pricing_net_row, 0)

        pricing_sync_row = QHBoxLayout()
        pricing_sync_row.setSpacing(8)
        self.fetch_now_btn = QPushButton("立即联网更新费率与汇率", self.pricing_section_widget)
        self.fetch_now_btn.setToolTip("从在线汇率源与 GitHub / jsDelivr 镜像同步最新 USD->CNY 汇率与官方模型单价")
        self.fetch_now_btn.clicked.connect(self._on_fetch_online_pricing_clicked)
        pricing_sync_row.addWidget(self.fetch_now_btn, 0)

        self.fetch_status_lbl = QLabel("", self.pricing_section_widget)
        self.fetch_status_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        self.fetch_status_lbl.setStyleSheet("color: #928374; background: transparent; border: none;")
        pricing_sync_row.addWidget(self.fetch_status_lbl, 1)
        pricing_lay.addLayout(pricing_sync_row, 0)

        self.pricing_table = QTableWidget(0, 5, self.pricing_section_widget)
        self.pricing_table.setHorizontalHeaderLabels(
            [
                "模型名称",
                "输入单价 ($/1M)",
                "缓存单价 ($/1M)",
                "输出单价 ($/1M)",
                "思考单价 ($/1M)",
            ]
        )
        self.pricing_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pricing_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.pricing_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pricing_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pricing_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.pricing_table.verticalHeader().setVisible(False)
        self.pricing_table.setMinimumHeight(230)
        p_hdr = self.pricing_table.horizontalHeader()
        p_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        p_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        p_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        p_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        p_hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.pricing_table.itemChanged.connect(self._on_pricing_table_item_changed)
        pricing_lay.addWidget(self.pricing_table, 1)

        pricing_hint = QLabel(
            "说明：直接双击单元格修改每百万 Token 美元单价，修改后自动实时保存并刷新主界面与侧边栏折算费用。",
            self.pricing_section_widget,
        )
        pricing_hint.setWordWrap(True)
        pricing_hint.setStyleSheet("color: #928374; background: transparent; border: none;")
        pricing_lay.addWidget(pricing_hint, 0)

        self.pricing_scroll.setWidget(self.pricing_section_widget)
        pricing_tab_lay.addWidget(self.pricing_scroll, 1)
        self.tabs.addTab(tab_pricing, "Model Pricing 模型定价")

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        self.export_csv_btn = QPushButton("导出历史为 CSV...", self.dialog_body)
        self.export_csv_btn.setToolTip("将本地归档的每日 Token 与折算成本导出为 CSV 报表")
        self.export_csv_btn.clicked.connect(self._on_export_csv_clicked)
        btn_row.addWidget(self.export_csv_btn, 0)
        btn_row.addStretch(1)
        self.close_btn = QPushButton("关闭", self.dialog_body)
        self.close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.close_btn, 0)
        body_lay.addLayout(btn_row, 0)

        root_lay.addWidget(self.dialog_body, 1)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if hasattr(self, "header") and self.header.geometry().contains(event.pos()):
                self._is_dragging = True
                self._drag_start = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if getattr(self, "_is_dragging", False) and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_start)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        self._is_dragging = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.accept()
            event.accept()
            return
        super().keyPressEvent(event)

    def _load_from_config(self) -> None:
        self._loading_config = True
        try:
            main_path = str(self.config_mgr.get("main_agent_path", "") or "").strip()
            self.main_path_edit.setText(main_path)
            self._update_main_path_status()

            self.sub_table.setRowCount(0)
            sub_agents = self.config_mgr.get("sub_agents", [])
            if isinstance(sub_agents, list):
                for item in sub_agents:
                    if not isinstance(item, dict):
                        continue
                    self.add_sub_agent_row(
                        name=str(item.get("name") or "SubAgent"),
                        path=str(item.get("path") or ""),
                        parser_type=str(item.get("parser_type") or "gemini_cli_session"),
                        live_state_path=str(item.get("live_state_path") or ""),
                        enabled=bool(item.get("enabled", True)),
                    )

            theme_cfg = self.config_mgr.get("theme", {})
            if not isinstance(theme_cfg, dict):
                theme_cfg = dict(DEFAULT_CONFIG["theme"])
            preset_name = str(theme_cfg.get("preset") or "Gruvbox Material").strip()
            self._updating_preset_fields = True
            try:
                idx = self.preset_combo.findText(preset_name)
                if idx >= 0:
                    self.preset_combo.setCurrentIndex(idx)
                base_preset = THEME_PRESETS.get(preset_name, THEME_PRESETS["Gruvbox Material"])
                for key, _ in self.COLOR_FIELD_SPECS:
                    val = str(theme_cfg.get(key) or base_preset[key]).strip()
                    self.color_edits[key].setText(val)
                    self._update_color_swatch(key, val)

                backdrop_key = str(theme_cfg.get("backdrop") or "mica").strip().lower()
                bd_idx = self.backdrop_combo.findData(backdrop_key)
                if bd_idx < 0:
                    bd_idx = self.backdrop_combo.findData("mica")
                if bd_idx >= 0:
                    self.backdrop_combo.setCurrentIndex(bd_idx)

                try:
                    bg_op_val = float(theme_cfg.get("bg_opacity", 0.65))
                    bg_op_val = max(0.05, min(1.0, bg_op_val))
                except Exception:
                    bg_op_val = 0.65
                self.bg_opacity = round(bg_op_val, 2)
                pct_val = max(5, min(100, int(round(self.bg_opacity * 100))))
                self.bg_opacity_slider.setValue(pct_val)
                self.bg_opacity_val_label.setText(f"{pct_val}%")
            finally:
                self._updating_preset_fields = False

            poll_ms = int(self.config_mgr.get("poll_interval_ms", 1000) or 1000)
            self.poll_spin.setValue(max(250, min(5000, poll_ms)))

            ttl_sec = float(self.config_mgr.get("scan_ttl_sec", 2.5) or 2.5)
            self.ttl_spin.setValue(max(1.0, min(10.0, ttl_sec)))

            auto_upd = bool(self.config_mgr.get("auto_update_pricing", True))
            self.auto_update_pricing_chk.setChecked(auto_upd)

            proxy_val = str(self.config_mgr.get("proxy", "") or "").strip()
            self.proxy_edit.setText(proxy_val)

            rate_val = float(
                self.config_mgr.get("usd_to_cny_rate", DEFAULT_USD_TO_CNY_RATE)
                or DEFAULT_USD_TO_CNY_RATE
            )
            self.usd_to_cny_spin.setValue(max(0.01, min(1000.0, rate_val)))
            pricing_cfg = self.config_mgr.get("pricing", {})
            self._populate_pricing_table(
                pricing_cfg if isinstance(pricing_cfg, dict) else get_default_pricing_config()
            )
        finally:
            self._loading_config = False

    def _count_antigravity_sessions(self, candidate_dir: Path | None) -> int:
        if candidate_dir is None or not candidate_dir.is_dir():
            return 0
        try:
            if (candidate_dir / ".system_generated" / "logs" / "transcript.jsonl").is_file():
                return 1
            return len(list(candidate_dir.glob("*/.system_generated/logs/transcript.jsonl")))
        except Exception:
            return 0

    def _update_main_path_status(self) -> None:
        raw_txt = self.main_path_edit.text().strip()
        if raw_txt:
            resolved_dir, is_valid = _probe_antigravity_brain_dir(raw_txt)
        else:
            resolved_dir, is_valid = auto_detect_antigravity_path(custom_path="", config_mgr=None)

        count = self._count_antigravity_sessions(resolved_dir)
        if is_valid and count > 0:
            self.main_status_lbl.setText(f"🟢 已找到 {count} 个会话")
            self.main_status_lbl.setStyleSheet("color: #A9B665; font-weight: bold; background: transparent; border: none;")
        else:
            self.main_status_lbl.setText("🔴 路径无效")
            self.main_status_lbl.setStyleSheet("color: #EA6962; font-weight: bold; background: transparent; border: none;")

    def _on_main_path_changed(self, _txt: str = "") -> None:
        self._update_main_path_status()
        if getattr(self, "_loading_config", False):
            return
        self.config_mgr.set("main_agent_path", self.main_path_edit.text().strip())
        if self._hud_parent is not None and hasattr(self._hud_parent, "apply_settings"):
            self._hud_parent.apply_settings()

    def _on_browse_main_path(self) -> None:
        start_dir = self.main_path_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择 Antigravity Brain 监控目录", start_dir)
        if chosen:
            self.main_path_edit.setText(chosen)
            self._on_main_path_changed(chosen)

    def _on_auto_detect_main_path(self) -> None:
        custom_txt = self.main_path_edit.text().strip()
        detected, is_valid = auto_detect_antigravity_path(
            custom_path=custom_txt,
            config_mgr=self.config_mgr,
        )
        if not is_valid and custom_txt:
            detected_default, is_valid_default = auto_detect_antigravity_path(
                custom_path="",
                config_mgr=None,
            )
            if detected_default is not None:
                detected, is_valid = detected_default, is_valid_default
        if detected is not None:
            self.main_path_edit.setText(str(detected))
        self._on_main_path_changed(self.main_path_edit.text())

    def _sync_sub_agents_immediately(self) -> None:
        if getattr(self, "_loading_config", False) or getattr(self, "_updating_sub_table", False):
            return
        sub_agents = self._collect_sub_agents()
        self.config_mgr.set("sub_agents", sub_agents)
        if self._hud_parent is not None and hasattr(self._hud_parent, "apply_settings"):
            self._hud_parent.apply_settings()

    def add_sub_agent_row(
        self,
        name: str = "",
        path: str = "",
        parser_type: str = "gemini_cli_session",
        live_state_path: str = "",
        enabled: bool = True,
    ) -> int:
        prev_flag = getattr(self, "_updating_sub_table", False)
        self._updating_sub_table = True
        try:
            row_idx = self.sub_table.rowCount()
            self.sub_table.insertRow(row_idx)

            enabled_item = QTableWidgetItem("")
            enabled_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            enabled_item.setCheckState(
                Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
            )
            self.sub_table.setItem(row_idx, 0, enabled_item)

            resolved_name = (
                ConfigManager._resolve_sub_agent_display_name(name, path) if path else name
            )
            name_item = QTableWidgetItem(resolved_name)
            path_item = QTableWidgetItem(path)
            live_item = QTableWidgetItem(live_state_path)
            live_item.setData(Qt.ItemDataRole.UserRole, str(live_state_path or "").strip())
            self.sub_table.setItem(row_idx, 1, name_item)
            self.sub_table.setItem(row_idx, 2, path_item)

            parser_combo = QComboBox(self.sub_table)
            parser_combo.addItems(list(PARSER_REGISTRY.keys()))
            parser_combo.setCurrentText(
                parser_type if parser_type in PARSER_REGISTRY else "gemini_cli_session"
            )
            self.sub_table.setCellWidget(row_idx, 3, parser_combo)
            self.sub_table.setItem(row_idx, 3, QTableWidgetItem(parser_combo.currentText()))
            parser_combo.currentTextChanged.connect(
                lambda txt, cb=parser_combo: self._on_sub_parser_changed(cb, txt)
            )

            self.sub_table.setItem(row_idx, 4, live_item)
            self.sub_table.selectRow(row_idx)
        finally:
            self._updating_sub_table = prev_flag

        self._update_sub_agent_row_status(row_idx)
        self._sync_sub_agents_immediately()
        return row_idx

    def _update_sub_agent_row_status(self, row_idx: int) -> None:
        if not (0 <= row_idx < self.sub_table.rowCount()):
            return
        path_item = self.sub_table.item(row_idx, 2)
        path_str = path_item.text().strip() if path_item is not None else ""

        parser_widget = self.sub_table.cellWidget(row_idx, 3)
        parser_item = self.sub_table.item(row_idx, 3)
        if isinstance(parser_widget, QComboBox):
            parser_type = parser_widget.currentText().strip()
        elif parser_item is not None:
            parser_type = parser_item.text().strip()
        else:
            parser_type = "gemini_cli_session"

        status_text, status_color = self._probe_sub_agent_status(path_str, parser_type)

        prev_flag = getattr(self, "_updating_sub_table", False)
        self._updating_sub_table = True
        try:
            status_item = self.sub_table.item(row_idx, 4)
            if status_item is None:
                status_item = QTableWidgetItem()
                self.sub_table.setItem(row_idx, 4, status_item)
            status_item.setText(status_text)
            status_item.setForeground(QColor(status_color))
            status_item.setToolTip(
                f"探测状态: {status_text}\n监控路径: {path_str or '(空)'}\n解析器: {parser_type}"
            )
        finally:
            self._updating_sub_table = prev_flag

    def _on_sub_table_item_changed(self, item: QTableWidgetItem | None) -> None:
        if item is None or getattr(self, "_updating_sub_table", False):
            return
        row_idx = item.row()
        col_idx = item.column()
        if col_idx in (2, 3):
            self._update_sub_agent_row_status(row_idx)
        self._sync_sub_agents_immediately()

    def _on_sub_parser_changed(self, combo: QComboBox, txt: str) -> None:
        for r in range(self.sub_table.rowCount()):
            if self.sub_table.cellWidget(r, 3) is combo:
                prev_flag = getattr(self, "_updating_sub_table", False)
                self._updating_sub_table = True
                try:
                    item = self.sub_table.item(r, 3)
                    if item is not None:
                        item.setText(txt)
                finally:
                    self._updating_sub_table = prev_flag
                self._update_sub_agent_row_status(r)
                self._sync_sub_agents_immediately()
                break

    @staticmethod
    def _norm_sub_path_key(raw_path: str | Path) -> str:
        try:
            return os.path.normcase(
                os.path.normpath(str(Path(raw_path).expanduser().resolve()))
            )
        except Exception:
            return os.path.normcase(os.path.normpath(str(raw_path).strip()))

    def _on_auto_scan_sub_agents(self, _checked: bool = False) -> int:
        existing_keys: set[str] = set()
        for r in range(self.sub_table.rowCount()):
            p_item = self.sub_table.item(r, 2)
            if p_item is not None and p_item.text().strip():
                existing_keys.add(self._norm_sub_path_key(p_item.text().strip()))

        candidates: list[tuple[Path, str, str]] = []
        gemini_home_env = os.environ.get("GEMINI_HOME", "").strip()
        if gemini_home_env:
            gh = Path(gemini_home_env).expanduser()
            candidates.append((gh / "tmp", "Gemini CLI", "gemini_cli_session"))
            candidates.append((gh / "chats", "Gemini CLI", "gemini_cli_session"))

        home_gemini = Path.home() / ".gemini"
        candidates.append((home_gemini / "tmp", "Gemini CLI", "gemini_cli_session"))
        candidates.append((home_gemini / "chats", "Gemini CLI", "gemini_cli_session"))

        home_claude = Path.home() / ".claude"
        candidates.append((home_claude, "Claude Code", "claude_code_session"))

        added_count = 0
        for cand_dir, default_name, parser_type in candidates:
            try:
                p = cand_dir.expanduser()
                if not p.is_dir():
                    continue
                norm_k = self._norm_sub_path_key(p)
                if norm_k in existing_keys:
                    continue
                if parser_type == "gemini_cli_session":
                    session_files = list(p.rglob("session-*.jsonl"))
                    if not session_files:
                        session_files = TokenMonitorCache._discover_sub_agent_files(
                            p, parser_type
                        )
                else:
                    session_files = TokenMonitorCache._discover_sub_agent_files(
                        p, parser_type
                    )
                if session_files:
                    existing_keys.add(norm_k)
                    self.add_sub_agent_row(
                        name=default_name,
                        path=str(p),
                        parser_type=parser_type,
                        enabled=True,
                    )
                    added_count += 1
            except Exception:
                continue
        if added_count > 0:
            self._sync_sub_agents_immediately()
        return added_count

    def set_sub_agent_enabled(self, row_idx: int, enabled: bool) -> None:
        if 0 <= row_idx < self.sub_table.rowCount():
            item = self.sub_table.item(row_idx, 0)
            if item is not None:
                item.setCheckState(
                    Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
                )
                self._sync_sub_agents_immediately()

    def _on_browse_sub_path(self) -> None:
        row_idx = self.sub_table.currentRow()
        if row_idx < 0:
            if self.sub_table.rowCount() == 0:
                row_idx = self.add_sub_agent_row()
            else:
                row_idx = self.sub_table.rowCount() - 1
        cur_item = self.sub_table.item(row_idx, 2)
        start_dir = (cur_item.text().strip() if cur_item else "") or str(GEMINI_TMP_ROOT)
        chosen = QFileDialog.getExistingDirectory(self, "选择子 Agent 会话日志目录", start_dir)
        if chosen:
            prev_flag = getattr(self, "_updating_sub_table", False)
            self._updating_sub_table = True
            try:
                if cur_item is None:
                    cur_item = QTableWidgetItem(chosen)
                    self.sub_table.setItem(row_idx, 2, cur_item)
                else:
                    cur_item.setText(chosen)
                name_item = self.sub_table.item(row_idx, 1)
                cur_name = name_item.text().strip() if name_item else ""
                if not cur_name:
                    auto_name = ConfigManager._resolve_sub_agent_display_name("", chosen)
                    if name_item is None:
                        self.sub_table.setItem(row_idx, 1, QTableWidgetItem(auto_name))
                    else:
                        name_item.setText(auto_name)
            finally:
                self._updating_sub_table = prev_flag
            self._update_sub_agent_row_status(row_idx)
            self._sync_sub_agents_immediately()

    def remove_selected_sub_agent_row(self) -> None:
        row_idx = self.sub_table.currentRow()
        if row_idx < 0 and self.sub_table.rowCount() > 0:
            row_idx = self.sub_table.rowCount() - 1
        if row_idx >= 0:
            self.sub_table.removeRow(row_idx)
            self._sync_sub_agents_immediately()

    def _collect_sub_agents(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for r in range(self.sub_table.rowCount()):
            enabled_item = self.sub_table.item(r, 0)
            name_item = self.sub_table.item(r, 1)
            path_item = self.sub_table.item(r, 2)
            parser_widget = self.sub_table.cellWidget(r, 3)
            parser_item = self.sub_table.item(r, 3)
            live_item = self.sub_table.item(r, 4)

            enabled_val = (
                enabled_item.checkState() == Qt.CheckState.Checked
                if enabled_item is not None
                else True
            )
            path_val = path_item.text().strip() if path_item else ""
            raw_name = name_item.text().strip() if name_item else ""
            name_val = ConfigManager._resolve_sub_agent_display_name(raw_name, path_val)
            if isinstance(parser_widget, QComboBox):
                parser_val = parser_widget.currentText().strip()
            elif parser_item is not None:
                parser_val = parser_item.text().strip()
            else:
                parser_val = "gemini_cli_session"
            if parser_val not in PARSER_REGISTRY:
                parser_val = "gemini_cli_session"
            live_val = ""
            if live_item is not None:
                raw_live_txt = live_item.text().strip()
                user_live_txt = str(live_item.data(Qt.ItemDataRole.UserRole) or "").strip()
                if raw_live_txt and not raw_live_txt.startswith(("🟢", "🟡", "🔴")):
                    live_val = raw_live_txt
                else:
                    live_val = user_live_txt

            if path_val:
                result.append(
                    {
                        "enabled": enabled_val,
                        "name": name_val,
                        "path": path_val,
                        "parser_type": parser_val,
                        "live_state_path": live_val,
                    }
                )
        return result

    def _update_color_swatch(self, key: str, hex_str: str) -> None:
        swatch = self.color_swatches.get(key)
        if swatch is None:
            return
        clean = hex_str.strip()
        if _HEX_COLOR_RE.match(clean):
            swatch.setStyleSheet(
                f"background-color: {clean}; border: 1px solid #928374; border-radius: 3px;"
            )

    def _collect_theme_data(self) -> dict[str, Any]:
        preset_name = self.preset_combo.currentText().strip() or "Gruvbox Material"
        base_preset = THEME_PRESETS.get(preset_name, THEME_PRESETS["Gruvbox Material"])
        theme_data: dict[str, Any] = {"preset": preset_name}
        for key, _ in self.COLOR_FIELD_SPECS:
            raw_hex = self.color_edits[key].text().strip()
            theme_data[key] = raw_hex if _HEX_COLOR_RE.match(raw_hex) else base_preset[key]

        backdrop_val = str(self.backdrop_combo.currentData() or "").strip().lower()
        if backdrop_val not in ("mica", "acrylic", "mica_alt", "none"):
            text_to_bd = {lbl: k for lbl, k in self.BACKDROP_OPTIONS}
            backdrop_val = text_to_bd.get(self.backdrop_combo.currentText().strip(), "mica")
        if hasattr(self, "bg_opacity_slider"):
            self.bg_opacity = round(max(0.05, min(1.0, self.bg_opacity_slider.value() / 100.0)), 2)
        theme_data["bg_opacity"] = self.bg_opacity
        theme_data["backdrop"] = backdrop_val
        return theme_data

    def _flush_pending_opacity_render(self) -> None:
        if self._pending_opacity_val is None:
            return
        op_val = float(self._pending_opacity_val)
        self._pending_opacity_val = None
        self._last_opacity_render_time = time.perf_counter()
        self.apply_bg_opacity(op_val)
        target_hud = self.hud_window if self.hud_window is not None else self._hud_parent
        if target_hud is not None:
            if hasattr(target_hud, "apply_bg_opacity"):
                target_hud.apply_bg_opacity(op_val)
            elif hasattr(target_hud, "apply_theme"):
                target_hud.apply_theme()

    def _persist_theme_debounced(self) -> None:
        if hasattr(self, "_opacity_throttle_timer") and self._opacity_throttle_timer.isActive():
            self._opacity_throttle_timer.stop()
            self._flush_pending_opacity_render()
        if getattr(self, "_dialog_qss_dirty_from_opacity", False):
            self._apply_dialog_stylesheet(apply_dwm=False)
        theme_data = self._collect_theme_data()
        self.config_mgr.set("theme", theme_data)

    def _sync_theme_immediately(self) -> None:
        if getattr(self, "_loading_config", False) or getattr(self, "_updating_preset_fields", False):
            return
        theme_data = self._collect_theme_data()
        self.config_mgr.set("theme", theme_data, persist=False)
        self._apply_dialog_stylesheet()
        if self.hud_window is not None and hasattr(self.hud_window, "apply_theme"):
            self.hud_window.apply_theme()
        elif self._hud_parent is not None and hasattr(self._hud_parent, "apply_theme"):
            self._hud_parent.apply_theme()
        if hasattr(self, "_theme_debounce_timer"):
            self._theme_debounce_timer.start(200)

    def _on_bg_opacity_changed(self, val: int) -> None:
        pct = max(5, min(100, int(val)))
        self.bg_opacity = round(pct / 100.0, 2)
        if hasattr(self, "bg_opacity_val_label"):
            self.bg_opacity_val_label.setText(f"{pct}%")
        if getattr(self, "_loading_config", False) or getattr(self, "_updating_preset_fields", False):
            return
        # 1. 立即同步纯内存配置与目标 HUD 的属性值（零 I/O，零重排）
        theme_data = self._collect_theme_data()
        self.config_mgr.set("theme", theme_data, persist=False)
        target_hud = self.hud_window if self.hud_window is not None else self._hud_parent
        if target_hud is not None:
            target_hud.bg_opacity = self.bg_opacity

        # 2. 30ms (~33fps) 节流器控制界面视觉重绘，杜绝高频信号阻塞消息队列
        self._pending_opacity_val = self.bg_opacity
        now_ts = time.perf_counter()
        elapsed_ms = (now_ts - self._last_opacity_render_time) * 1000.0
        if elapsed_ms >= 30.0:
            if self._opacity_throttle_timer.isActive():
                self._opacity_throttle_timer.stop()
            self._flush_pending_opacity_render()
        else:
            if not self._opacity_throttle_timer.isActive():
                remain_ms = max(1, int(round(30.0 - elapsed_ms)))
                self._opacity_throttle_timer.start(remain_ms)

        # 3. 200ms 防抖持久化至磁盘
        if hasattr(self, "_theme_debounce_timer"):
            self._theme_debounce_timer.start(200)

    def closeEvent(self, event: Any) -> None:
        if hasattr(self, "_opacity_throttle_timer") and self._opacity_throttle_timer.isActive():
            self._opacity_throttle_timer.stop()
            self._flush_pending_opacity_render()
        if hasattr(self, "_theme_debounce_timer") and self._theme_debounce_timer.isActive():
            self._theme_debounce_timer.stop()
            self._persist_theme_debounced()
        super().closeEvent(event)

    def _on_color_edit_changed(self, key: str, hex_str: str) -> None:
        self._update_color_swatch(key, hex_str)
        if _HEX_COLOR_RE.match(hex_str.strip()):
            self._sync_theme_immediately()

    def _on_backdrop_changed(self, _idx: int = 0) -> None:
        self._sync_theme_immediately()

    def _on_preset_changed(self, preset_name: str) -> None:
        if self._updating_preset_fields:
            return
        preset = THEME_PRESETS.get(preset_name)
        if not preset:
            return
        self._updating_preset_fields = True
        try:
            for key, _ in self.COLOR_FIELD_SPECS:
                val = preset.get(key, "")
                if val and key in self.color_edits:
                    self.color_edits[key].setText(val)
                    self._update_color_swatch(key, val)
        finally:
            self._updating_preset_fields = False
        self._sync_theme_immediately()

    def _on_pick_color(self, key: str) -> None:
        edit = self.color_edits.get(key)
        if edit is None:
            return
        cur_hex = edit.text().strip()
        init_color = QColor(cur_hex) if _HEX_COLOR_RE.match(cur_hex) else QColor("#141617")
        chosen = QColorDialog.getColor(init_color, self, f"选择颜色 - {key}")
        if chosen.isValid():
            hex_val = chosen.name()
            edit.setText(hex_val)
            self._update_color_swatch(key, hex_val)
            self._sync_theme_immediately()

    @staticmethod
    def _format_rate_value(val: float) -> str:
        f_val = max(0.0, float(val or 0.0))
        if abs(f_val - round(f_val, 2)) < 1e-6:
            return f"{f_val:.2f}"
        return f"{f_val:.6f}".rstrip("0").rstrip(".")

    def _populate_pricing_table(self, pricing_cfg: dict[str, Any]) -> None:
        prev_flag = getattr(self, "_updating_pricing_table", False)
        self._updating_pricing_table = True
        try:
            default_pricing = get_default_pricing_config()
            normalized_cfg: dict[str, dict[str, float]] = {
                k: dict(v) for k, v in default_pricing.items()
            }
            if isinstance(pricing_cfg, dict):
                for raw_k, raw_v in pricing_cfg.items():
                    if not isinstance(raw_v, dict):
                        continue
                    canon_k = TokenPricingEngine.canonicalize_model_key(str(raw_k))
                    base_entry = dict(
                        normalized_cfg.get(
                            canon_k,
                            DEFAULT_MODEL_PRICING_PER_1M.get(
                                canon_k, DEFAULT_MODEL_PRICING_PER_1M["default"]
                            ),
                        )
                    )
                    for field in ("input", "cached", "output", "thought"):
                        if field in raw_v and not isinstance(raw_v[field], bool):
                            try:
                                f_val = float(raw_v[field])
                                if (
                                    f_val == f_val
                                    and f_val not in (float("inf"), float("-inf"))
                                    and f_val >= 0.0
                                ):
                                    base_entry[field] = round(f_val, 6)
                            except (TypeError, ValueError):
                                pass
                    normalized_cfg[canon_k] = base_entry

            rows_spec: list[tuple[str, str]] = list(MODEL_PRICING_PRESETS)
            seen_keys = {k for k, _ in rows_spec}
            for extra_k in normalized_cfg.keys():
                if extra_k not in seen_keys:
                    rows_spec.append((extra_k, extra_k))
                    seen_keys.add(extra_k)

            self.pricing_table.setRowCount(0)
            for row_idx, (model_key, display_name) in enumerate(rows_spec):
                self.pricing_table.insertRow(row_idx)
                rates = normalized_cfg.get(
                    model_key,
                    DEFAULT_MODEL_PRICING_PER_1M.get(
                        model_key, DEFAULT_MODEL_PRICING_PER_1M["default"]
                    ),
                )
                name_item = QTableWidgetItem(display_name)
                name_item.setData(Qt.ItemDataRole.UserRole, model_key)
                name_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
                name_item.setToolTip(f"模型显示名: {display_name}\n配置键名: {model_key}")
                self.pricing_table.setItem(row_idx, 0, name_item)

                for col_idx, field in enumerate(("input", "cached", "output", "thought"), start=1):
                    f_val = float(rates.get(field, 0.0) or 0.0)
                    rate_item = QTableWidgetItem(self._format_rate_value(f_val))
                    rate_item.setData(Qt.ItemDataRole.UserRole, round(f_val, 6))
                    rate_item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                    rate_item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsEditable
                    )
                    self.pricing_table.setItem(row_idx, col_idx, rate_item)
        finally:
            self._updating_pricing_table = prev_flag

    def _collect_pricing_data(self) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = get_default_pricing_config()
        field_cols = ((1, "input"), (2, "cached"), (3, "output"), (4, "thought"))
        for r in range(self.pricing_table.rowCount()):
            name_item = self.pricing_table.item(r, 0)
            if name_item is None:
                continue
            raw_key = str(name_item.data(Qt.ItemDataRole.UserRole) or name_item.text() or "").strip()
            if not raw_key:
                continue
            model_key = TokenPricingEngine.canonicalize_model_key(raw_key)
            default_rates = DEFAULT_MODEL_PRICING_PER_1M.get(
                model_key, DEFAULT_MODEL_PRICING_PER_1M["default"]
            )
            entry: dict[str, float] = dict(default_rates)
            for col_idx, field in field_cols:
                cell_item = self.pricing_table.item(r, col_idx)
                if cell_item is None:
                    continue
                raw_txt = cell_item.text().strip().lstrip("$¥")
                try:
                    val = float(raw_txt)
                    if val == val and val not in (float("inf"), float("-inf")) and val >= 0.0:
                        entry[field] = round(min(100000.0, val), 6)
                except (TypeError, ValueError):
                    cached_num = cell_item.data(Qt.ItemDataRole.UserRole)
                    if isinstance(cached_num, (int, float)):
                        entry[field] = round(max(0.0, float(cached_num)), 6)
            result[model_key] = entry
        return result

    def _sync_pricing_immediately(self) -> None:
        if getattr(self, "_loading_config", False) or getattr(
            self, "_updating_pricing_table", False
        ):
            return
        rate_val = round(max(0.01, min(1000.0, float(self.usd_to_cny_spin.value()))), 4)
        pricing_data = self._collect_pricing_data()
        self.config_mgr.set("usd_to_cny_rate", rate_val)
        self.config_mgr.set("pricing", pricing_data)
        if self._hud_parent is not None and hasattr(self._hud_parent, "apply_settings"):
            self._hud_parent.apply_settings()

    def _save_pricing_to_config(self) -> None:
        self._sync_pricing_immediately()

    def _on_auto_update_pricing_changed(self, _state: Any = None) -> None:
        if getattr(self, "_loading_config", False):
            return
        self.config_mgr.set("auto_update_pricing", bool(self.auto_update_pricing_chk.isChecked()))

    def _on_proxy_changed(self, _txt: str = "") -> None:
        if getattr(self, "_loading_config", False):
            return
        self.config_mgr.set("proxy", self.proxy_edit.text().strip())

    def _on_fetch_online_pricing_clicked(self, _checked: bool = False) -> None:
        if getattr(self, "_fetching_online_pricing", False):
            return
        self._fetching_online_pricing = True
        proxy_val = self.proxy_edit.text().strip()
        self.config_mgr.set("proxy", proxy_val)
        self.fetch_now_btn.setEnabled(False)
        self.fetch_now_btn.setText("正在联网更新...")
        self.fetch_status_lbl.setText("⏳ 正在联网更新...")
        self.fetch_status_lbl.setStyleSheet(
            "color: #D8A657; font-weight: bold; background: transparent; border: none;"
        )

        def _bg_fetch() -> None:
            try:
                online_rate = fetch_online_usd_rate(proxy=proxy_val, timeout=3.0)
                remote_table = fetch_remote_pricing_table(proxy=proxy_val, timeout=3.0)
                self._last_remote_pricing_table = (
                    dict(remote_table) if isinstance(remote_table, dict) else None
                )
                rate_num = float(online_rate) if online_rate is not None else 0.0
                upd_count = len(remote_table) if isinstance(remote_table, dict) else 0
                ok = (online_rate is not None) or (upd_count > 0)
                if online_rate is not None and upd_count > 0:
                    msg = f"✓ 汇率已更新为 {online_rate:.2f}（已同步 {upd_count} 个模型价格）"
                elif online_rate is not None:
                    msg = f"✓ 汇率已更新为 {online_rate:.2f}"
                elif upd_count > 0:
                    msg = f"✓ 模型价格已同步（{upd_count} 项），汇率保留本地配置"
                else:
                    msg = "✗ 汇率更新失败，已保留本地配置"
                self._online_bridge.fetch_finished.emit(ok, msg, rate_num, upd_count)
            except Exception:
                self._last_remote_pricing_table = None
                self._online_bridge.fetch_finished.emit(
                    False, "✗ 汇率更新失败，已保留本地配置", 0.0, 0
                )

        threading.Thread(target=_bg_fetch, daemon=True).start()

    def _on_online_fetch_finished(
        self,
        success: bool,
        message: str,
        new_rate: float = 0.0,
        updated_count: int = 0,
    ) -> None:
        self._fetching_online_pricing = False
        self.fetch_now_btn.setEnabled(True)
        self.fetch_now_btn.setText("立即联网更新费率与汇率")
        if not message:
            if success and new_rate > 0:
                message = f"✓ 汇率已更新为 {new_rate:.2f}"
            elif success:
                message = "✓ 费率同步完成"
            else:
                message = "✗ 汇率更新失败，已保留本地配置"

        color_hex = "#A9B665" if success else "#EA6962"
        self.fetch_status_lbl.setText(message)
        self.fetch_status_lbl.setStyleSheet(
            f"color: {color_hex}; font-weight: bold; background: transparent; border: none;"
        )

        changed = False
        prev_loading = getattr(self, "_loading_config", False)
        self._loading_config = True
        try:
            if new_rate and new_rate > 0.0:
                self.usd_to_cny_spin.setValue(float(new_rate))
                changed = True

            remote_table = getattr(self, "_last_remote_pricing_table", None)
            if updated_count > 0 and isinstance(remote_table, dict) and remote_table:
                current_local = self._collect_pricing_data()
                merged_pricing = merge_remote_pricing_into_config(
                    local_pricing=current_local,
                    remote_pricing=remote_table,
                )
                self._populate_pricing_table(merged_pricing)
                changed = True
            elif updated_count > 0:
                changed = True
        finally:
            self._loading_config = prev_loading

        if changed or success:
            self._save_pricing_to_config()

    def _on_pricing_changed(self, _val: Any = None) -> None:
        self._sync_pricing_immediately()

    def _on_pricing_table_item_changed(self, item: QTableWidgetItem | None) -> None:
        if item is None or getattr(self, "_updating_pricing_table", False):
            return
        col_idx = item.column()
        if col_idx in (1, 2, 3, 4):
            raw_txt = item.text().strip().lstrip("$¥")
            prev_flag = getattr(self, "_updating_pricing_table", False)
            self._updating_pricing_table = True
            try:
                try:
                    val = float(raw_txt)
                    if val != val or val in (float("inf"), float("-inf")) or val < 0.0:
                        raise ValueError("invalid rate")
                    val = round(min(100000.0, val), 6)
                except (TypeError, ValueError):
                    fallback = item.data(Qt.ItemDataRole.UserRole)
                    val = float(fallback) if isinstance(fallback, (int, float)) else 0.0
                item.setData(Qt.ItemDataRole.UserRole, val)
                item.setText(self._format_rate_value(val))
            finally:
                self._updating_pricing_table = prev_flag
        self._sync_pricing_immediately()

    def _find_pricing_row(self, model_name: str) -> int:
        target_canon = TokenPricingEngine.canonicalize_model_key(model_name)
        target_norm = TokenPricingEngine._normalize_model_key(model_name)
        for r in range(self.pricing_table.rowCount()):
            name_item = self.pricing_table.item(r, 0)
            if name_item is None:
                continue
            row_key = str(name_item.data(Qt.ItemDataRole.UserRole) or "").strip()
            row_txt = name_item.text().strip()
            if (
                row_key == target_canon
                or TokenPricingEngine._normalize_model_key(row_txt) == target_norm
                or TokenPricingEngine.canonicalize_model_key(row_txt) == target_canon
            ):
                return r
        return -1

    def set_model_pricing_rate(self, model_name: str, field: str, value: float) -> bool:
        """按模型名与字段名（input/cached/output/thought）修改费率单元格并触发实时热重载。"""
        col_map = {"input": 1, "cached": 2, "output": 3, "thought": 4}
        col_idx = col_map.get(str(field or "").strip().lower())
        if col_idx is None:
            return False
        row_idx = self._find_pricing_row(model_name)
        if row_idx < 0:
            return False
        cell_item = self.pricing_table.item(row_idx, col_idx)
        if cell_item is None:
            return False
        cell_item.setText(self._format_rate_value(float(value)))
        return True

    def get_model_pricing_rates(self, model_name: str) -> dict[str, float]:
        """读取设置表格中指定模型的四维单价字典。"""
        row_idx = self._find_pricing_row(model_name)
        if row_idx < 0:
            return {}
        col_map = {1: "input", 2: "cached", 3: "output", 4: "thought"}
        rates: dict[str, float] = {}
        for col_idx, field in col_map.items():
            item = self.pricing_table.item(row_idx, col_idx)
            if item is not None:
                try:
                    rates[field] = float(item.text().strip().lstrip("$¥"))
                except (TypeError, ValueError):
                    rates[field] = 0.0
        return rates

    def reset_pricing_to_defaults(self, _checked: bool = False) -> None:
        """一键恢复所有官方默认单价与默认 USD->CNY 汇率，并立即热重载生效。"""
        default_pricing = get_default_pricing_config()
        prev_loading = getattr(self, "_loading_config", False)
        self._loading_config = True
        try:
            self.usd_to_cny_spin.setValue(DEFAULT_USD_TO_CNY_RATE)
            self._populate_pricing_table(default_pricing)
        finally:
            self._loading_config = prev_loading
        self._sync_pricing_immediately()

    def _on_perf_changed(self, _val: Any = None) -> None:
        if getattr(self, "_loading_config", False):
            return
        poll_ms = int(self.poll_spin.value())
        ttl_sec = round(float(self.ttl_spin.value()), 2)
        self.config_mgr.set("poll_interval_ms", poll_ms)
        self.config_mgr.set("scan_ttl_sec", ttl_sec)
        if self._hud_parent is not None:
            self._hud_parent.refresh_ms = max(250, min(5000, poll_ms))
            if hasattr(self._hud_parent, "_timer"):
                self._hud_parent._timer.setInterval(self._hud_parent.refresh_ms)
            if hasattr(self._hud_parent, "monitor") and self._hud_parent.monitor is not None:
                self._hud_parent.monitor.scan_ttl_sec = max(1.0, min(10.0, ttl_sec))

    def save_and_apply(self) -> None:
        main_path = self.main_path_edit.text().strip()
        sub_agents = self._collect_sub_agents()
        poll_ms = int(self.poll_spin.value())
        ttl_sec = round(float(self.ttl_spin.value()), 2)
        rate_val = round(max(0.01, min(1000.0, float(self.usd_to_cny_spin.value()))), 4)
        pricing_data = self._collect_pricing_data()
        theme_data = self._collect_theme_data()

        self.config_mgr.set("main_agent_path", main_path)
        self.config_mgr.set("sub_agents", sub_agents)
        self.config_mgr.set("poll_interval_ms", poll_ms)
        self.config_mgr.set("scan_ttl_sec", ttl_sec)
        self.config_mgr.set("usd_to_cny_rate", rate_val)
        self.config_mgr.set("pricing", pricing_data)
        self.config_mgr.set("theme", theme_data)
        self.config_mgr.save()

        if self._hud_parent is not None and hasattr(self._hud_parent, "apply_settings"):
            self._hud_parent.apply_settings()

    def export_history_to_csv(self, output_path: Path | str | None = None) -> Path | None:
        """从设置面板触发导出历史 CSV（可传入指定路径供自动化测试，未传则调起保存对话框或父窗口导出方法）。"""
        if self._hud_parent is not None and hasattr(self._hud_parent, "export_history_to_csv"):
            return self._hud_parent.export_history_to_csv(output_path)
        from .monitor import export_history_to_csv as _export_csv

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
        return _export_csv(output_path)

    def _on_export_csv_clicked(self) -> None:
        self.export_history_to_csv(None)

