# -*- coding: utf-8 -*-
"""
hud_core.selftest - Token HUD 非阻塞自动化自检套件。
"""
from __future__ import annotations

import csv
import ctypes
from ctypes import wintypes
import datetime
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QColorDialog

from .config import DEFAULT_CONFIG, THEME_PRESETS, ConfigManager, auto_detect_antigravity_path
from .hud_window import TokenHUDWindow
from .monitor import CSV_FIELDNAMES, TokenMonitorCache, export_history_to_csv
from .parsers import (
    PARSER_REGISTRY,
    AntigravityBrainParser,
    ClaudeCodeSessionParser,
    GeminiCliSessionParser,
    _discover_claude_code_files,
    parse_antigravity_transcript,
    parse_claude_code_session,
    parse_session_jsonl,
    reset_session_today_stats,
)
from .pricing import (
    DEFAULT_USD_TO_CNY_RATE,
    MAX_VALID_USD_TO_CNY_RATE,
    MIN_VALID_USD_TO_CNY_RATE,
    TokenPricingEngine,
    fetch_online_usd_rate,
    fetch_remote_pricing_table,
    format_cost_cny,
    format_cost_usd,
    merge_remote_pricing_into_config,
    normalize_proxy_url,
    normalize_remote_pricing_payload,
    parse_usd_to_cny_payload,
    resolve_effective_proxy,
    resolve_proxy_settings,
    safe_fetch_json,
)
from .settings_dialog import SettingsDialog
from .win32_dwm import (
    apply_win11_mica_and_rounded_corners,
    bind_interactive_desktop,
    enable_dpi_awareness,
)


def run_self_test(
    target_session: str | None = None,
    target_project: str | None = None,
    interval: int = 1000,
    config_path: str | Path | None = None,
) -> int:
    """运行非阻塞自检：验证 JSONL 解析、界面组件隔离、DWM 属性与设置联动，成功返回 0。"""
    args = SimpleNamespace(
        session=target_session,
        project=target_project,
        interval=interval,
    )
    config_mgr = ConfigManager(config_path=config_path)
    monitor = TokenMonitorCache(config_mgr=config_mgr)
    snap = monitor.collect_snapshot(target_session=args.session, target_project=args.project)
    bind_interactive_desktop()
    enable_dpi_awareness()
    hud = TokenHUDWindow(
        monitor=monitor,
        config_mgr=config_mgr,
        refresh_ms=args.interval,
        target_session=args.session,
        target_project=args.project,
    )
    hud._update_once()
    card_geometries = {}
    for i in range(len(hud.card_views)):
        hud._show_card(i)
        hud.root.update_idletasks()
        card_geometries[f"card_{i}"] = {
            "req_w": int(hud.root.winfo_reqwidth()),
            "req_h": int(hud.root.winfo_reqheight()),
            "win_w": int(hud.root.winfo_width()),
            "win_h": int(hud.root.winfo_height()),
            "x": int(hud.root.winfo_x()),
            "y": int(hud.root.winfo_y()),
            "sidebar_w": int(hud.sidebar_frame.winfo_width()),
            "deck_w": int(hud.deck.winfo_width()),
        }

    class _DummyWheelEvent:
        delta = -120

    hud._on_mousewheel(_DummyWheelEvent())
    sidebar_geom = {
        "ui_engine": "PySide6 (Qt 6)",
        "permanent_sidebar": True,
        "has_scrollbar": False,
        "win_w": int(hud.root.winfo_width()),
        "win_h": int(hud.root.winfo_height()),
        "x": int(hud.root.winfo_x()),
        "y": int(hud.root.winfo_y()),
        "sidebar_w": int(hud.sidebar_frame.winfo_width()),
        "deck_w": int(hud.deck.winfo_width()),
        "rendered_items": len(hud._active_row_keys),
    }
    hud._set_sidebar_filter("main")
    hud._reset_pinned_session()
    hud._show_card(0)
    hud.root.update_idletasks()

    class _DummyMouseEvent:
        def __init__(self, x_root: int, y_root: int):
            self.x_root = x_root
            self.y_root = y_root

    orig_x, orig_y = hud._get_window_screen_origin()
    hud._on_header_press(_DummyMouseEvent(orig_x + 50, orig_y + 15))
    for step in range(1, 15):
        hud._on_drag_motion(_DummyMouseEvent(orig_x + 50 - step * 10, orig_y + 15 + step * 5))
    hud._on_header_release(_DummyMouseEvent(orig_x + 50 - 140, orig_y + 15 + 70))
    hud.root.update_idletasks()
    moved_x, moved_y = hud._get_window_screen_origin()
    drag_test = {
        "window_alive": bool(hud.root.winfo_exists()),
        "user_moved_window": hud._user_moved_window,
        "orig_pos": [orig_x, orig_y],
        "moved_pos": [moved_x, moved_y],
        "card_unchanged_after_drag": hud.current_card_idx == 0,
    }

    # 测试右下角缩放手柄拖拽调整窗口大小及内部控件/数字动态自适应
    base_size = [int(hud.width()), int(hud.height())]
    base_sidebar_w = int(hud.sidebar_frame.winfo_width())
    base_big_pt = int(hud.card_views[0]["total_val"].font().pointSize())
    base_grid_pt = int(hud.card_views[0]["lbl_in"].font().pointSize())

    rx0 = moved_x + base_size[0] - 6
    ry0 = moved_y + base_size[1] - 6
    hud._on_resize_press(rx0, ry0)
    hud._on_resize_motion(rx0 + 220, ry0 + 130)
    hud._on_resize_release(rx0 + 220, ry0 + 130)
    hud.root.update_idletasks()
    enlarged_size = [int(hud.width()), int(hud.height())]
    enlarged_sidebar_w = int(hud.sidebar_frame.winfo_width())
    enlarged_big_pt = int(hud.card_views[0]["total_val"].font().pointSize())
    enlarged_grid_pt = int(hud.card_views[0]["lbl_in"].font().pointSize())

    rx1 = moved_x + enlarged_size[0] - 6
    ry1 = moved_y + enlarged_size[1] - 6
    hud._on_resize_press(rx1, ry1)
    hud._on_resize_motion(rx1 - 380, ry1 - 210)
    hud._on_resize_release(rx1 - 380, ry1 - 210)
    hud.root.update_idletasks()
    compact_size = [int(hud.width()), int(hud.height())]
    compact_sidebar_w = int(hud.sidebar_frame.winfo_width())
    compact_big_pt = int(hud.card_views[0]["total_val"].font().pointSize())
    compact_grid_pt = int(hud.card_views[0]["lbl_in"].font().pointSize())

    resize_test = {
        "has_resize_grip": hasattr(hud, "resize_grip") and hud.resize_grip.isVisible(),
        "user_resized_window": hud._user_resized_window,
        "base": {
            "size": base_size,
            "sidebar_w": base_sidebar_w,
            "big_num_pt": base_big_pt,
            "grid_num_pt": base_grid_pt,
        },
        "enlarged": {
            "size": enlarged_size,
            "sidebar_w": enlarged_sidebar_w,
            "big_num_pt": enlarged_big_pt,
            "grid_num_pt": enlarged_grid_pt,
        },
        "compact": {
            "size": compact_size,
            "sidebar_w": compact_sidebar_w,
            "big_num_pt": compact_big_pt,
            "grid_num_pt": compact_grid_pt,
        },
    }
    hud._reset_window_geometry()
    hud.root.update_idletasks()

    # 测试子菜单与右侧统计页之间的横向调整手柄拖拽占比
    sx_before = int(hud.sidebar_frame.winfo_width())
    dx_before = int(hud.deck.winfo_width())
    hud._on_splitter_press(moved_x + sx_before + 10)
    hud._on_splitter_motion(moved_x + sx_before + 10 - 65)
    hud._on_splitter_release(moved_x + sx_before + 10 - 65)
    hud.root.update_idletasks()
    sx_narrow = int(hud.sidebar_frame.winfo_width())
    dx_wide = int(hud.deck.winfo_width())

    hud._on_splitter_press(moved_x + sx_narrow + 10)
    hud._on_splitter_motion(moved_x + sx_narrow + 10 + 130)
    hud._on_splitter_release(moved_x + sx_narrow + 10 + 130)
    hud.root.update_idletasks()
    sx_wide = int(hud.sidebar_frame.winfo_width())
    dx_narrow = int(hud.deck.winfo_width())

    splitter_test = {
        "has_splitter_handle": hasattr(hud, "splitter_handle") and hud.splitter_handle.isVisible(),
        "grid_layout_mode": "2x2",
        "default_split": {"sidebar_w": sx_before, "deck_w": dx_before},
        "drag_left_expand_deck": {"sidebar_w": sx_narrow, "deck_w": dx_wide},
        "drag_right_expand_sidebar": {"sidebar_w": sx_wide, "deck_w": dx_narrow},
    }

    # 测试手柄单击折叠与展开功能
    hud.toggle_sidebar()
    hud.root.update_idletasks()
    collapsed_ok = bool(hud._sidebar_collapsed) and not hud._sidebar_qframe.isVisible()

    hud.toggle_sidebar()
    hud.root.update_idletasks()
    expanded_ok = (not hud._sidebar_collapsed) and hud._sidebar_qframe.isVisible()

    splitter_test["toggle_collapse_expand"] = {
        "collapse_ok": collapsed_ok,
        "expanded_ok": expanded_ok,
    }
    assert collapsed_ok, "splitter_test: 侧边栏折叠断言失败"
    assert expanded_ok, "splitter_test: 侧边栏展开断言失败"

    hud._reset_window_geometry()
    hud.root.update_idletasks()

    # 测试 ConfigManager、auto_detect_antigravity_path、SettingsDialog、预设配色联动、极简模式与热重载（使用临时目录，不污染用户配置）
    import tempfile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmp_cfg_dir:
        tmp_dir_path = Path(tmp_cfg_dir)

        # 1. config_test：测试 ConfigManager 对默认值加载、自定义路径保存、脏数据过滤与热重载的断言
        tmp_cfg_path = tmp_dir_path / "test_config.json"
        tmp_cfg = ConfigManager(config_path=tmp_cfg_path)
        default_loaded_ok = (
            tmp_cfg.get("main_agent_path") == ""
            and tmp_cfg.get("sub_agents") == []
            and tmp_cfg.get("poll_interval_ms") == 1000
            and tmp_cfg.get("scan_ttl_sec") == 2.5
            and tmp_cfg.get("theme", {}).get("preset") == "Gruvbox Material"
            and abs(float(tmp_cfg.get("theme", {}).get("opacity", 0.0)) - 0.94) < 1e-6
            and tmp_cfg.get("theme", {}).get("backdrop") == "mica"
        )
        assert default_loaded_ok, "config_test: 默认值加载断言失败"

        tmp_cfg.set("main_agent_path", "  C:/custom/brain/path  ")
        tmp_cfg.set("poll_interval_ms", 1500)
        custom_save_ok = (
            tmp_cfg_path.is_file()
            and tmp_cfg.get("main_agent_path") == "C:/custom/brain/path"
            and tmp_cfg.get("poll_interval_ms") == 1500
        )
        assert custom_save_ok, "config_test: 自定义路径保存断言失败"

        dirty_payload = {
            "main_agent_path": 12345,
            "sub_agents": [
                "not_a_dict",
                {"name": "", "path": " /tmp/sub1 ", "parser_type": "invalid_parser", "live_state_path": " "},
            ],
            "poll_interval_ms": -50,
            "scan_ttl_sec": 9999.0,
            "theme": {
                "preset": "Cyberpunk Dark",
                "bg_color": "not-a-hex",
                "accent_color": "#123456",
                "opacity": 0.12,
                "backdrop": "invalid_glass",
            },
        }
        tmp_cfg_path.write_text(json.dumps(dirty_payload), encoding="utf-8")
        reloaded_data = tmp_cfg.load()
        dirty_filtered_ok = (
            reloaded_data["main_agent_path"] == ""
            and len(reloaded_data["sub_agents"]) == 1
            and reloaded_data["sub_agents"][0]["name"] == "sub1"
            and reloaded_data["sub_agents"][0]["path"] == "/tmp/sub1"
            and reloaded_data["sub_agents"][0]["parser_type"] == "gemini_cli_session"
            and reloaded_data["poll_interval_ms"] == 100
            and reloaded_data["scan_ttl_sec"] == 300.0
            and reloaded_data["theme"]["bg_color"] == THEME_PRESETS["Cyberpunk Dark"]["bg_color"]
            and reloaded_data["theme"]["accent_color"] == "#123456"
            and abs(float(reloaded_data["theme"]["opacity"]) - 0.50) < 1e-6
            and reloaded_data["theme"]["backdrop"] == "mica"
        )
        assert dirty_filtered_ok, "config_test: 脏数据过滤与热重载断言失败"

        tmp_cfg_path.write_text("{broken json", encoding="utf-8")
        corrupted_fallback_ok = tmp_cfg.load()["sub_agents"] == []
        assert corrupted_fallback_ok, "config_test: 损坏 JSON 回退默认配置断言失败"

        norm_pricing_res = ConfigManager.normalize_config(
            {
                "usd_to_cny_rate": 7.45,
                "pricing": {
                    "Gemini 3.8 Pro": {"input": 0.625, "cached": 0.15, "output": 5.0, "thought": 5.0},
                    "invalid_entry": "not_a_dict",
                },
            }
        )
        pricing_normalize_ok = (
            isinstance(DEFAULT_CONFIG.get("pricing"), dict)
            and abs(float(DEFAULT_CONFIG.get("usd_to_cny_rate", 0.0)) - 7.25) < 1e-6
            and abs(float(norm_pricing_res.get("usd_to_cny_rate", 0.0)) - 7.45) < 1e-6
            and abs(float(norm_pricing_res["pricing"]["gemini-3.8-pro"]["input"]) - 0.625) < 1e-6
            and abs(float(norm_pricing_res["pricing"]["gemini-3.8-pro"]["output"]) - 5.0) < 1e-6
            and "default" in norm_pricing_res["pricing"]
        )
        assert pricing_normalize_ok, "config_test: ConfigManager.normalize_config 定价与汇率校验断言失败"

        config_test = {
            "passed": True,
            "default_loaded_ok": default_loaded_ok,
            "custom_save_ok": custom_save_ok,
            "dirty_filtered_ok": dirty_filtered_ok,
            "corrupted_fallback_ok": corrupted_fallback_ok,
            "pricing_normalize_ok": pricing_normalize_ok,
        }

        # 2. path_detection_test：测试 auto_detect_antigravity_path() 在不同环境变量及路径配置下的探测与有效性断言
        fake_home = tmp_dir_path / "fake_gemini_home"
        fake_brain = fake_home / "antigravity" / "brain"
        fake_cid_logs = fake_brain / "cid-001" / ".system_generated" / "logs"
        fake_cid_logs.mkdir(parents=True, exist_ok=True)
        fake_transcript = fake_cid_logs / "transcript.jsonl"

        empty_brain = tmp_dir_path / "empty_brain"
        empty_brain.mkdir(parents=True, exist_ok=True)

        # 仅存在目录但无 transcript.jsonl 时（隔离宿主机默认 home 目录），返回 (resolved_dir, False)
        with patch.object(Path, "home", return_value=tmp_dir_path):
            det_empty_dir, det_empty_valid = auto_detect_antigravity_path(
                custom_path=str(empty_brain), config_mgr=tmp_cfg
            )
        empty_dir_probed_ok = det_empty_dir == empty_brain and det_empty_valid is False
        assert empty_dir_probed_ok, "path_detection_test: 空目录有效性断言失败"

        # 写入 transcript.jsonl 后，测试 custom_path、config.main_agent_path 与环境变量 GEMINI_HOME 探测
        fake_transcript.write_text('{"type": "user"}\n', encoding="utf-8")

        det_custom_dir, det_custom_valid = auto_detect_antigravity_path(
            custom_path=str(fake_brain), config_mgr=tmp_cfg
        )
        custom_path_ok = det_custom_dir == fake_brain and det_custom_valid is True
        assert custom_path_ok, "path_detection_test: custom_path 探测断言失败"

        det_file_dir, det_file_valid = auto_detect_antigravity_path(
            custom_path=str(fake_transcript), config_mgr=tmp_cfg
        )
        direct_file_ok = det_file_dir == fake_brain and det_file_valid is True
        assert direct_file_ok, "path_detection_test: 直接指向 transcript.jsonl 探测断言失败"

        cfg_for_path = ConfigManager(config_path=tmp_dir_path / "path_cfg.json")
        cfg_for_path.set("main_agent_path", str(fake_home))
        det_cfg_dir, det_cfg_valid = auto_detect_antigravity_path(custom_path="", config_mgr=cfg_for_path)
        config_path_ok = det_cfg_dir == fake_brain and det_cfg_valid is True
        assert config_path_ok, "path_detection_test: config.main_agent_path 探测断言失败"

        empty_cfg = ConfigManager(config_path=tmp_dir_path / "empty_cfg.json")
        old_gemini_home = os.environ.get("GEMINI_HOME")
        try:
            os.environ["GEMINI_HOME"] = str(fake_home)
            det_env_dir, det_env_valid = auto_detect_antigravity_path(custom_path="", config_mgr=empty_cfg)
            env_gemini_home_ok = det_env_dir == fake_brain and det_env_valid is True
        finally:
            if old_gemini_home is None:
                os.environ.pop("GEMINI_HOME", None)
            else:
                os.environ["GEMINI_HOME"] = old_gemini_home
        assert env_gemini_home_ok, "path_detection_test: GEMINI_HOME 环境变量探测断言失败"

        path_detection_test = {
            "passed": True,
            "empty_dir_probed_ok": empty_dir_probed_ok,
            "custom_path_ok": custom_path_ok,
            "direct_file_ok": direct_file_ok,
            "config_path_ok": config_path_ok,
            "env_gemini_home_ok": env_gemini_home_ok,
        }

        # 3. theme_and_settings_test：测试 SettingsDialog 子 Agent 区域常驻展示、无子 Agent 时过滤栏隐藏、启用子 Agent 时动态激活多 Agent 视图、主题切换与拾色器应用
        ui_cfg = ConfigManager(config_path=tmp_dir_path / "ui_test_config.json")
        hud.config_mgr = ui_cfg
        hud.monitor.config_mgr = ui_cfg
        hud.apply_settings()
        filter_hidden_when_no_sub = not hud.filter_bar.isVisible()
        single_card_only_ok = len(hud.card_views) == 1
        assert filter_hidden_when_no_sub, "theme_and_settings_test: 无子 Agent 时侧边栏过滤栏隐藏断言失败"
        assert single_card_only_ok, "theme_and_settings_test: 仅保留第 0 张主卡片断言失败"

        # 验证单卡片架构下，点击标题栏翻页保持在卡片 0，且不显示切换提示
        hud.cycle_card()
        no_sub_flip_disabled_ok = (
            hud.current_card_idx == 0
            and hud._title_qlabel.text() == "[Token 总用量]"
            and not hud.switch_hint_lbl.isVisible()
            and "切换" not in hud.switch_hint_lbl.text()
        )
        assert no_sub_flip_disabled_ok, "theme_and_settings_test: 禁止切换子卡片断言失败"

        dlg = SettingsDialog(hud, ui_cfg)
        dlg.show()
        QApplication.processEvents()
        no_buttons_and_no_opacity_ok = (
            not hasattr(dlg, "save_btn")
            and not hasattr(dlg, "cancel_btn")
            and hasattr(dlg, "close_btn")
            and dlg.close_btn.isVisible()
            and hasattr(dlg, "header")
            and hasattr(dlg, "dialog_title_lbl")
            and hasattr(dlg, "header_close_btn")
            and bool(dlg.windowFlags() & Qt.WindowType.FramelessWindowHint)
            and not hasattr(dlg, "opacity_slider")
            and not hasattr(dlg, "opacity_val_lbl")
            and abs(hud.alpha - 0.94) < 1e-6
        )
        assert no_buttons_and_no_opacity_ok, "theme_and_settings_test: 移除确认/取消按钮与透明度滑块及保留关闭按钮断言失败"
        sub_section_visible_ok = (
            hasattr(dlg, "sub_section_widget")
            and dlg.sub_section_widget.isVisible()
            and dlg.sub_table.isVisible()
            and dlg.add_sub_btn.isVisible()
            and dlg.remove_sub_btn.isVisible()
            and dlg.sub_table.columnCount() == 5
        )
        assert sub_section_visible_ok, "theme_and_settings_test: SettingsDialog 显示子 Agent 管理区域断言失败"
        dlg.close_btn.click()
        QApplication.processEvents()
        cancel_keeps_main_alive_ok = (
            not dlg.isVisible()
            and hud.isVisible()
            and hud._in_mainloop is False
            and not hud._app.quitOnLastWindowClosed()
        )
        assert cancel_keeps_main_alive_ok, "theme_and_settings_test: 关闭设置弹窗后主窗口保持运行断言失败"

        # 创建模拟子 Agent 日志文件，验证添加/启用/禁用子 Agent 与主界面的动态实时联动
        mock_sub_dir = tmp_dir_path / "mock_sub_agent" / "chats"
        mock_sub_dir.mkdir(parents=True, exist_ok=True)
        mock_sub_jsonl = mock_sub_dir / "session-test-sub.jsonl"
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        mock_sub_jsonl.write_text(
            "\n".join(
                [
                    json.dumps({"sessionId": "sub-sess-99887766", "startTime": now_iso}),
                    json.dumps(
                        {
                            "type": "user",
                            "timestamp": now_iso,
                            "content": [{"text": "执行子 Agent 多模态验证任务"}],
                        }
                    ),
                    json.dumps(
                        {
                            "type": "gemini",
                            "id": "turn-1",
                            "timestamp": now_iso,
                            "model": "gemini-4-argon",
                            "tokens": {
                                "input": 4000,
                                "cached": 2500,
                                "output": 600,
                                "thoughts": 200,
                                "tool": 0,
                                "total": 4800,
                            },
                            "toolCalls": [{"name": "grep_search", "args": {}}],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        dlg = SettingsDialog(hud, ui_cfg)
        dlg.show()
        QApplication.processEvents()
        presets_checked: dict[str, bool] = {}
        for preset_name, preset_colors in THEME_PRESETS.items():
            dlg.preset_combo.setCurrentText(preset_name)
            transparent_ok = (
                "background: transparent; border: none;" in hud._grand_total_qlabel.styleSheet()
                and all(
                    "transparent" in view["info_frame"].styleSheet()
                    and "transparent" in view["tot_frame"].styleSheet()
                    and "background: transparent; border: none;" in view["total_val"].styleSheet()
                    and "background: transparent; border: none;" in view["lbl_t1"].styleSheet()
                    and "background: transparent; border: none;" in view["lbl_t2"].styleSheet()
                    and "background: transparent; border: none;" in view["lbl_in"].styleSheet()
                    and "background: transparent; border: none;" in view["lbl_cache"].styleSheet()
                    and "background: transparent; border: none;" in view["lbl_out"].styleSheet()
                    and "background: transparent; border: none;" in view["lbl_thought"].styleSheet()
                    and (
                        "background: transparent; border: none;" in view["cache_rate_val"].styleSheet()
                        if view.get("kind") == "summary"
                        else "background: transparent; border: none;" in view["ctx_val"].styleSheet()
                    )
                    and all("transparent" in c["cell"].styleSheet() for c in view.get("grid_cells", []))
                    for view in hud.card_views
                )
            )
            matched = (
                transparent_ok
                and hud.BG_MAIN.lower() == preset_colors["bg_color"].lower()
                and hud.FG_ACCENT.lower() == preset_colors["accent_color"].lower()
                and all(
                    dlg.color_edits[k].text().lower() == preset_colors[k].lower()
                    for k, _ in dlg.COLOR_FIELD_SPECS
                )
            )
            presets_checked[preset_name] = matched
            assert matched, f"theme_and_settings_test: 预设 {preset_name} 实时联动断言失败"
        all_4_presets_ok = len(presets_checked) == 4 and all(presets_checked.values())
        assert all_4_presets_ok, "theme_and_settings_test: 4 套预设切换实时生效断言失败"

        # 切回 Cyberpunk Dark 并测试 QColorDialog 拾色器色值实时应用
        dlg.preset_combo.setCurrentText("Cyberpunk Dark")
        expected_bg = THEME_PRESETS["Cyberpunk Dark"]["bg_color"]
        picked_accent = "#ff8800"
        with patch.object(QColorDialog, "getColor", return_value=QColor(picked_accent)):
            dlg._on_pick_color("accent_color")
        color_picker_ok = (
            dlg.color_edits["accent_color"].text().lower() == picked_accent
            and picked_accent in dlg.color_swatches["accent_color"].styleSheet().lower()
            and hud.FG_ACCENT.lower() == picked_accent
            and ui_cfg.get("theme", {}).get("accent_color", "").lower() == picked_accent
        )
        assert color_picker_ok, "theme_and_settings_test: 拾色器色值实时应用断言失败"

        # 验证毛玻璃下拉框与面板背景不透明度滑块的回显与实时 DWM 属性动态应用
        opacity_and_backdrop_loaded_ok = (
            not hasattr(dlg, "opacity_slider")
            and hasattr(dlg, "backdrop_combo")
            and dlg.backdrop_combo.currentData() == "mica"
            and dlg.backdrop_combo.count() == 4
            and hasattr(dlg, "bg_opacity_slider")
            and hasattr(dlg, "bg_opacity_val_label")
            and dlg.bg_opacity_slider.value() == 65
            and dlg.bg_opacity_val_label.text() == "65%"
            and abs(hud.bg_opacity - 0.65) < 1e-6
        )
        assert opacity_and_backdrop_loaded_ok, "theme_and_settings_test: 毛玻璃与背景不透明度初始回显断言失败"

        dlg.bg_opacity_slider.setValue(35)
        dlg.bg_opacity_slider.setValue(45)
        if hasattr(dlg, "_opacity_throttle_timer") and dlg._opacity_throttle_timer.isActive():
            dlg._opacity_throttle_timer.stop()
            dlg._flush_pending_opacity_render()
        bg_opacity_slider_updated_ok = (
            hasattr(hud, "apply_bg_opacity")
            and hasattr(dlg, "apply_bg_opacity")
            and dlg.bg_opacity_val_label.text() == "45%"
            and abs(dlg.bg_opacity - 0.45) < 1e-6
            and abs(hud.bg_opacity - 0.45) < 1e-6
            and "0.45)" in hud.BG_CARD_CSS
            and abs(float(ui_cfg.get("theme", {}).get("bg_opacity", 0.0)) - 0.45) < 1e-6
        )
        assert bg_opacity_slider_updated_ok, "theme_and_settings_test: 背景不透明度滑块实时联动断言失败"

        acrylic_idx = dlg.backdrop_combo.findData("acrylic")
        assert acrylic_idx >= 0, "theme_and_settings_test: 未找到 acrylic 下拉项"
        dlg.backdrop_combo.setCurrentIndex(acrylic_idx)

        # 验证 _probe_sub_agent_status 三种状态返回及表格行即时探测与自动扫描按钮
        empty_sub_dir = tmp_dir_path / "empty_sub_dir"
        empty_sub_dir.mkdir(parents=True, exist_ok=True)
        probe_ok_txt, probe_ok_col = dlg._probe_sub_agent_status(
            str(mock_sub_dir), "gemini_cli_session"
        )
        probe_zero_txt, probe_zero_col = dlg._probe_sub_agent_status(
            str(empty_sub_dir), "gemini_cli_session"
        )
        probe_bad_txt, probe_bad_col = dlg._probe_sub_agent_status(
            str(tmp_dir_path / "non_existent_sub_dir"), "gemini_cli_session"
        )
        sub_probe_helper_ok = (
            probe_ok_txt == "🟢 1 个会话"
            and probe_ok_col == "#A9B665"
            and probe_zero_txt == "🟡 0 个会话"
            and probe_zero_col == "#D8A657"
            and probe_bad_txt == "🔴 路径无效"
            and probe_bad_col == "#EA6962"
        )
        assert sub_probe_helper_ok, "theme_and_settings_test: _probe_sub_agent_status 三种状态探测断言失败"

        # 测试自动扫描按钮发现有效子 Agent 目录（含 Gemini CLI 与 Claude Code）并去重
        scan_home = tmp_dir_path / "scan_user_home"
        scan_tmp_dir = scan_home / ".gemini" / "tmp"
        scan_tmp_dir.mkdir(parents=True, exist_ok=True)
        (scan_tmp_dir / "session-scan-01.jsonl").write_text(
            mock_sub_jsonl.read_text(encoding="utf-8"), encoding="utf-8"
        )
        scan_claude_proj = scan_home / ".claude" / "projects" / "demo-proj"
        scan_claude_proj.mkdir(parents=True, exist_ok=True)
        mock_claude_jsonl = scan_claude_proj / "claude-sess-01.jsonl"
        mock_claude_jsonl.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "sessionId": "claude-sid-11223344",
                            "type": "user",
                            "timestamp": now_iso,
                            "message": {"role": "user", "content": "Claude Code 任务测试"},
                        }
                    ),
                    json.dumps(
                        {
                            "sessionId": "claude-sid-11223344",
                            "type": "assistant",
                            "timestamp": now_iso,
                            "message": {
                                "id": "msg_cc_01",
                                "role": "assistant",
                                "model": "claude-3-7-sonnet-20250219",
                                "content": [{"type": "tool_use", "name": "Read"}],
                                "usage": {
                                    "input_tokens": 1200,
                                    "cache_read_input_tokens": 800,
                                    "output_tokens": 300,
                                },
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        discovered_claude = _discover_claude_code_files(scan_home / ".claude")
        parsed_claude = parse_claude_code_session(mock_claude_jsonl)
        claude_parser_ok = (
            "claude_code_session" in PARSER_REGISTRY
            and len(discovered_claude) == 1
            and _discover_claude_code_files(mock_claude_jsonl) == [mock_claude_jsonl]
            and parsed_claude["session_id"] == "claude-sid-11223344"
            and parsed_claude["session_name"] == "Claude Code 任务测试"
            and parsed_claude["model"] == "claude-3-7-sonnet-20250219"
            and parsed_claude["steps"] == 1
            and parsed_claude["input_tokens"] == 2000
            and parsed_claude["cached_tokens"] == 800
            and parsed_claude["output_tokens"] == 300
            and parsed_claude["total_tokens"] == 2300
            and parsed_claude["today_steps"] == 1
            and parsed_claude["today_input_tokens"] == 2000
            and parsed_claude["today_cached_tokens"] == 800
            and parsed_claude["today_output_tokens"] == 300
            and parsed_claude["today_total_tokens"] == 2300
        )
        assert claude_parser_ok, "theme_and_settings_test: Claude Code 发现与解析断言失败"
        with patch.dict(os.environ, {"GEMINI_HOME": ""}), patch.object(
            Path, "home", return_value=scan_home
        ):
            added_first = dlg._on_auto_scan_sub_agents()
            added_second = dlg._on_auto_scan_sub_agents()
        auto_scan_sub_ok = (
            hasattr(dlg, "auto_scan_sub_btn")
            and dlg.auto_scan_sub_btn.isVisible()
            and added_first == 2
            and added_second == 0
            and dlg.sub_table.rowCount() == 2
            and dlg.sub_table.item(0, 1).text() == "Gemini CLI"
            and dlg.sub_table.item(0, 4).text() == "🟢 1 个会话"
            and dlg.sub_table.item(1, 1).text() == "Claude Code"
            and dlg.sub_table.cellWidget(1, 3).currentText() == "claude_code_session"
            and dlg.sub_table.item(1, 4).text() == "🟢 1 个会话"
            and len(ui_cfg.get("sub_agents", [])) == 2
        )
        assert auto_scan_sub_ok, "theme_and_settings_test: 自动扫描按钮实时保存与去重断言失败"
        dlg.remove_selected_sub_agent_row()
        dlg.remove_selected_sub_agent_row()

        row_idx = dlg.add_sub_agent_row(
            name="ArgonSub",
            path=str(mock_sub_dir),
            parser_type="gemini_cli_session",
            enabled=True,
        )
        initial_row_status_ok = dlg.sub_table.item(row_idx, 4).text() == "🟢 1 个会话"
        # 切换解析器下拉框触发即时状态刷新与实时生效（mock_sub_dir 下无 transcript.jsonl，应变为 🟡 0 个会话）
        row_combo = dlg.sub_table.cellWidget(row_idx, 3)
        row_combo.setCurrentText("antigravity_transcript")
        combo_switch_status_ok = dlg.sub_table.item(row_idx, 4).text() == "🟡 0 个会话"
        row_combo.setCurrentText("gemini_cli_session")
        # 修改路径单元格触发 itemChanged 即时刷新状态与实时保存
        dlg.sub_table.item(row_idx, 2).setText(str(tmp_dir_path / "bad_path_xyz"))
        item_changed_bad_ok = dlg.sub_table.item(row_idx, 4).text() == "🔴 路径无效"
        dlg.sub_table.item(row_idx, 2).setText(str(mock_sub_dir))
        item_changed_restore_ok = dlg.sub_table.item(row_idx, 4).text() == "🟢 1 个会话"
        sub_row_live_probe_ok = (
            initial_row_status_ok
            and combo_switch_status_ok
            and item_changed_bad_ok
            and item_changed_restore_ok
        )
        assert sub_row_live_probe_ok, "theme_and_settings_test: 表格行路径与解析器联动状态刷新断言失败"
        dlg.poll_spin.setValue(750)
        dlg.ttl_spin.setValue(4.0)
        QApplication.processEvents()
        dlg.close()
        QApplication.processEvents()

        save_keeps_main_alive_ok = hud.isVisible() and not hud._app.quitOnLastWindowClosed()
        assert save_keeps_main_alive_ok, "theme_and_settings_test: 实时设置并关闭弹窗后主窗口保持运行断言失败"

        timer_updated_ok = (
            hud._timer.interval() == 750
            and abs(hud.monitor.scan_ttl_sec - 4.0) < 1e-6
            and ui_cfg.get("poll_interval_ms") == 750
            and abs(float(ui_cfg.get("scan_ttl_sec", 0.0)) - 4.0) < 1e-6
        )
        theme_updated_ok = (
            hud.BG_MAIN.lower() == expected_bg.lower()
            and hud.FG_ACCENT.lower() == picked_accent.lower()
        )
        saved_theme_cfg = ui_cfg.get("theme", {})
        dwm_none_res = apply_win11_mica_and_rounded_corners(hud, backdrop_type="none")
        dwm_mica_alt_res = apply_win11_mica_and_rounded_corners(hud, backdrop_type="mica_alt")
        dwm_acrylic_res = apply_win11_mica_and_rounded_corners(hud, backdrop_type="acrylic")
        opacity_and_backdrop_applied_ok = (
            opacity_and_backdrop_loaded_ok
            and bg_opacity_slider_updated_ok
            and no_buttons_and_no_opacity_ok
            and abs(float(saved_theme_cfg.get("opacity", 0.0)) - 0.94) < 1e-6
            and abs(float(saved_theme_cfg.get("bg_opacity", 0.0)) - 0.45) < 1e-6
            and saved_theme_cfg.get("backdrop") == "acrylic"
            and abs(hud.alpha - 0.94) < 1e-6
            and abs(hud.bg_opacity - 0.45) < 1e-6
            and abs(hud.windowOpacity() - 0.94) < 1e-2
            and hud.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is True
            and "rgba(" in hud.BG_MAIN_CSS
            and "rgba(" in hud.BG_CARD_CSS
            and "transparent" in hud.body_frame.styleSheet()
            and "transparent" in hud._deck_qwidget.styleSheet()
            and "transparent" in hud.sidebar_scroll.styleSheet()
            and "transparent" in hud.sidebar_list_inner.styleSheet()
            and hud.backdrop_type == "acrylic"
            and hud.dwm_info.get("backdrop") == "acrylic"
            and hud.dwm_info.get("extended_frame") is True
            and dwm_none_res.get("backdrop") == "none"
            and dwm_mica_alt_res.get("backdrop") == "mica_alt"
            and dwm_acrylic_res.get("backdrop") == "acrylic"
        )
        assert timer_updated_ok, "theme_and_settings_test: QTimer 周期与 TTL 实时动态生效断言失败"
        assert theme_updated_ok, "theme_and_settings_test: 主题与拾色器颜色实时热重载断言失败"
        assert opacity_and_backdrop_applied_ok, "theme_and_settings_test: 固定透明度与毛玻璃效果实时保存及动态生效断言失败"

        # 验证启用子 Agent 后，主界面动态激活 filter_bar、加载子会话并支持筛选与选中详情展示
        sub_dynamic_activated_ok = (
            hud._has_enabled_sub_agents() is True
            and hud.filter_bar.isVisible() is True
            and len(hud._last_sub_sessions) == 1
            and hud._last_sub_sessions[0]["total_tokens"] == 4800
            and "main" in hud._last_sessions_by_agent
            and "sub_0" in hud._last_sessions_by_agent
            and len(hud._last_sessions_by_agent["sub_0"]) == 1
            and "main" in hud._last_today_by_agent
            and "sub_0" in hud._last_today_by_agent
            and hud._last_today_by_agent["sub_0"]["total_tokens"] == 4800
            and hud._last_today_by_agent["sub_0"]["sessions_count"] == 1
            and hud._last_today_by_agent["sub_0"]["steps_count"] == 1
        )
        assert sub_dynamic_activated_ok, "theme_and_settings_test: 启用子 Agent 后主界面动态激活断言失败"

        hud._set_sidebar_filter("sub")
        sub_filter_ok = hud.sidebar_filter == "sub" and "共 1 条会话" in hud.sidebar_count_lbl.text()
        assert sub_filter_ok, "theme_and_settings_test: 子 Agent 侧边栏过滤断言失败"

        hud._select_history_session("sub", str(mock_sub_jsonl))
        selected_sub_display_ok = (
            hud.pinned_sub_file == str(mock_sub_jsonl)
            and hud.card_views[0]["total_val"].text() == "4,800"
            and hud.card_views[0]["lbl_in"].text() == "4,000"
            and hud.card_views[0]["lbl_out"].text() == "600"
            and hud.card_views[0]["lbl_thought"].text() == "200"
        )
        assert selected_sub_display_ok, "theme_and_settings_test: 选中子 Agent 对话详情展示断言失败"
        hud._reset_pinned_session()

        # 在设置中取消勾选该子 Agent 的 enabled 复选框，验证无需点击确认即实时恢复单 Agent 极简模式
        dlg_disable = SettingsDialog(hud, ui_cfg)
        dlg_disable.show()
        QApplication.processEvents()
        dlg_disable.set_sub_agent_enabled(0, False)
        QApplication.processEvents()
        dlg_disable.close()
        QApplication.processEvents()
        sub_disable_restores_pure_main_ok = (
            hud._has_enabled_sub_agents() is False
            and not hud.filter_bar.isVisible()
            and len(hud._last_sub_sessions) == 0
        )
        assert sub_disable_restores_pure_main_ok, "theme_and_settings_test: 禁用子 Agent 后实时恢复纯净主模式断言失败"

        # 测试选中某条主 Agent 对话时主卡片区域显示该对话的 Token 总量与细分指标，且标题栏右侧常驻显示今日累计总 Token
        mock_main_sess = {
            "file_path": "C:/mock/session_1.jsonl",
            "cid": "cid-mock-123456",
            "session_name": "测试选中对话统计",
            "model": "claude-opus-4-6",
            "start_time": time.time() - 60.0,
            "last_updated": time.time(),
            "steps": 5,
            "tool_calls_count": 3,
            "last_tool": "read_file",
            "status": "completed",
            "context_tokens": 12000,
            "input_tokens": 10000,
            "cached_tokens": 8000,
            "output_tokens": 1500,
            "thought_tokens": 500,
            "total_tokens": 12000,
        }
        hud._apply_snapshot(
            {
                "timestamp": time.time(),
                "main_agent_valid": True,
                "main_session": mock_main_sess,
                "active_session": None,
                "all_main_sessions": [mock_main_sess],
                "all_sub_sessions": [],
                "today_stats": {
                    "main_sessions_count": 2,
                    "sub_sessions_count": 0,
                    "sessions_count": 2,
                    "main_steps_count": 10,
                    "sub_steps_count": 0,
                    "steps_count": 10,
                    "main_total_tokens": 50000,
                    "sub_total_tokens": 0,
                    "input_tokens": 40000,
                    "cached_tokens": 30000,
                    "output_tokens": 7000,
                    "thought_tokens": 3000,
                    "total_tokens": 50000,
                },
            }
        )
        hud._select_history_session("main", "C:/mock/session_1.jsonl")
        selected_session_display_ok = (
            hud.card_views[0]["total_val"].text() == "12,000"
            and hud.card_views[0]["lbl_in"].text() == "10,000"
            and "8,000" in hud.card_views[0]["lbl_cache"].text()
            and hud.card_views[0]["lbl_out"].text() == "1,500"
            and hud.card_views[0]["lbl_thought"].text() == "500"
            and "今日累计" in hud._grand_total_qlabel.text()
            and "50.0k" in hud._grand_total_compact_text
        )
        assert selected_session_display_ok, "theme_and_settings_test: 选中对话 Token 展示与标题栏今日累计断言失败"
        hud._reset_pinned_session()

        hud._apply_snapshot(
            {
                "timestamp": time.time(),
                "main_agent_valid": False,
                "main_session": None,
                "active_session": None,
                "all_main_sessions": [],
                "all_sub_sessions": [],
                "today_stats": {},
            }
        )
        empty_prompt_ok = "未检测到会话日志，请点击右上角【⚙ 设置】配置路径" in hud.card_views[0]["counts_lbl"].text()
        assert empty_prompt_ok, "theme_and_settings_test: 未检测到日志友好提示断言失败"

        # 恢复原配置管理器
        hud.config_mgr = config_mgr
        hud.monitor.config_mgr = config_mgr
        hud.apply_settings()

    theme_and_settings_test = {
        "passed": True,
        "has_settings_btn": hasattr(hud, "settings_btn") and hud.settings_btn.isVisible(),
        "sub_section_visible_ok": sub_section_visible_ok,
        "sub_probe_helper_ok": sub_probe_helper_ok,
        "claude_parser_ok": claude_parser_ok,
        "auto_scan_sub_ok": auto_scan_sub_ok,
        "sub_row_live_probe_ok": sub_row_live_probe_ok,
        "filter_hidden_when_no_sub": filter_hidden_when_no_sub,
        "sub_dynamic_activated_ok": sub_dynamic_activated_ok,
        "sub_filter_ok": sub_filter_ok,
        "selected_sub_display_ok": selected_sub_display_ok,
        "sub_disable_restores_pure_main_ok": sub_disable_restores_pure_main_ok,
        "single_card_only_ok": single_card_only_ok,
        "no_sub_flip_disabled_ok": no_sub_flip_disabled_ok,
        "selected_session_display_ok": selected_session_display_ok,
        "cancel_keeps_main_alive_ok": cancel_keeps_main_alive_ok,
        "save_keeps_main_alive_ok": save_keeps_main_alive_ok,
        "all_4_presets_ok": all_4_presets_ok,
        "presets_checked": presets_checked,
        "color_picker_ok": color_picker_ok,
        "opacity_and_backdrop_ok": opacity_and_backdrop_applied_ok,
        "timer_updated_ok": timer_updated_ok,
        "theme_updated_ok": theme_updated_ok,
        "empty_prompt_ok": empty_prompt_ok,
    }

    # ------------------------------------------------------------------
    # 9. v1.2 第二阶段自检：增量字节偏移读取、跨天零 I/O 归零、自适应待机与渲染暂停
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="token_hud_phase2_test_") as p2_tmp_str:
        p2_root = Path(p2_tmp_str)
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_iso_1 = now_dt.isoformat()
        now_iso_2 = (now_dt + datetime.timedelta(seconds=2)).isoformat()
        now_iso_3 = (now_dt + datetime.timedelta(seconds=4)).isoformat()

        # 9.1 准备三类解析器的测试目录与初始 JSONL 文件
        p2_brain_root = p2_root / "brain"
        p2_main_logs = p2_brain_root / "conv-inc-main-001" / ".system_generated" / "logs"
        p2_main_logs.mkdir(parents=True, exist_ok=True)
        p2_main_jsonl = p2_main_logs / "transcript.jsonl"

        p2_gemini_dir = p2_root / "gemini_sub" / "demo_proj" / "chats"
        p2_gemini_dir.mkdir(parents=True, exist_ok=True)
        p2_gemini_jsonl = p2_gemini_dir / "session-inc-01.jsonl"

        p2_claude_dir = p2_root / "claude_sub" / "projects" / "claude_proj"
        p2_claude_dir.mkdir(parents=True, exist_ok=True)
        p2_claude_jsonl = p2_claude_dir / "claude-inc-01.jsonl"

        # 写入初始第 1 步
        main_lines_1 = [
            json.dumps(
                {
                    "type": "USER_INPUT",
                    "status": "RUNNING",
                    "created_at": now_iso_1,
                    "content": "<USER_REQUEST>\nPhase2 增量读取主会话验证\n</USER_REQUEST>\nModel Selection: set to claude-opus-4-6.",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": now_iso_1,
                    "input_tokens": 1200,
                    "cache_read_tokens": 300,
                    "output_tokens": 250,
                    "thinking_tokens": 80,
                    "tool_calls": [{"name": "read_file"}],
                },
                ensure_ascii=False,
            ),
        ]
        p2_main_jsonl.write_text("\n".join(main_lines_1) + "\n", encoding="utf-8")
        main_size_1 = p2_main_jsonl.stat().st_size

        gemini_lines_1 = [
            json.dumps(
                {"sessionId": "gem-inc-11112222", "startTime": now_iso_1},
                ensure_ascii=False,
            ),
            json.dumps(
                {"type": "user", "timestamp": now_iso_1, "content": "Gemini 增量测试首行"},
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "gemini",
                    "id": "turn-1",
                    "timestamp": now_iso_1,
                    "model": "gemini-3.1-pro",
                    "tokens": {
                        "input": 2000,
                        "cached": 500,
                        "output": 300,
                        "thoughts": 100,
                        "tool": 50,
                        "total": 2450,
                    },
                    "toolCalls": [{"name": "grep_search", "args": {}}],
                },
                ensure_ascii=False,
            ),
        ]
        p2_gemini_jsonl.write_text("\n".join(gemini_lines_1) + "\n", encoding="utf-8")
        gemini_size_1 = p2_gemini_jsonl.stat().st_size

        claude_lines_1 = [
            json.dumps(
                {
                    "sessionId": "claude-inc-99887766",
                    "type": "user",
                    "timestamp": now_iso_1,
                    "message": {"role": "user", "content": "Claude 增量读取测试"},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "sessionId": "claude-inc-99887766",
                    "type": "assistant",
                    "timestamp": now_iso_1,
                    "message": {
                        "id": "cmsg-1",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "content": [{"type": "tool_use", "name": "Edit"}],
                        "usage": {
                            "input_tokens": 1000,
                            "cache_read_input_tokens": 400,
                            "cache_creation_input_tokens": 100,
                            "output_tokens": 200,
                            "thinking_tokens": 60,
                        },
                    },
                },
                ensure_ascii=False,
            ),
        ]
        p2_claude_jsonl.write_text("\n".join(claude_lines_1) + "\n", encoding="utf-8")
        claude_size_1 = p2_claude_jsonl.stat().st_size

        p2_cfg = ConfigManager(config_path=p2_root / "p2_config.json")
        p2_cfg.save(
            {
                "main_agent_path": str(p2_brain_root),
                "sub_agents": [
                    {
                        "id": "sub_gem",
                        "name": "GemInc",
                        "path": str(p2_root / "gemini_sub"),
                        "parser_type": "gemini_cli_session",
                        "enabled": True,
                    },
                    {
                        "id": "sub_claude",
                        "name": "ClaudeInc",
                        "path": str(p2_root / "claude_sub"),
                        "parser_type": "claude_code_session",
                        "enabled": True,
                    },
                ],
                "poll_interval_ms": 1000,
                "scan_ttl_sec": 2.5,
            }
        )
        p2_mon = TokenMonitorCache(config_mgr=p2_cfg)
        base_now = time.time()
        today_d1 = datetime.date.today()

        # 第一次采集：全量解析
        snap_step1 = p2_mon.collect_snapshot(
            force_scan=True, now_ts=base_now, today_date=today_d1
        )
        main_state = p2_mon.get_file_cache_state(p2_main_jsonl)
        gem_state = p2_mon.get_file_cache_state(p2_gemini_jsonl, "gemini_cli_session")
        claude_state = p2_mon.get_file_cache_state(p2_claude_jsonl, "claude_code_session")

        assert (
            main_state is not None
            and gem_state is not None
            and claude_state is not None
            and main_state["last_byte_offset"] == main_size_1
            and gem_state["last_byte_offset"] == gemini_size_1
            and claude_state["last_byte_offset"] == claude_size_1
            and main_state["full_reads"] == 1
            and main_state["incremental_reads"] == 0
        ), "phase2_test: 初始全量解析与 last_byte_offset 记录断言失败"

        # 9.2 追加写入 JSONL 第 2、3 步，验证增量字节偏移读取与全量解析结果完全一致
        main_append = (
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "PLANNER_RESPONSE",
                            "status": "RUNNING",
                            "created_at": now_iso_2,
                            "input_tokens": 1800,
                            "cache_read_tokens": 700,
                            "output_tokens": 420,
                            "thinking_tokens": 150,
                            "tool_calls": [{"name": "replace"}, {"name": "run_shell_command"}],
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "PLANNER_RESPONSE",
                            "status": "DONE",
                            "created_at": now_iso_3,
                            "input_tokens": 2200,
                            "cache_read_tokens": 1100,
                            "output_tokens": 310,
                            "thinking_tokens": 90,
                            "tool_calls": [],
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n"
        )
        with p2_main_jsonl.open("a", encoding="utf-8") as f:
            f.write(main_append)
        main_size_2 = p2_main_jsonl.stat().st_size

        gem_append = (
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "gemini",
                            "id": "turn-2",
                            "timestamp": now_iso_2,
                            "model": "gemini-3.1-pro",
                            "tokens": {
                                "input": 3200,
                                "cached": 1200,
                                "output": 480,
                                "thoughts": 160,
                                "tool": 80,
                                "total": 3920,
                            },
                            "toolCalls": [
                                {
                                    "name": "update_topic",
                                    "args": {"title": "Gemini 增量更新主题"},
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"$set": {"lastUpdated": now_iso_3}},
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n"
        )
        with p2_gemini_jsonl.open("a", encoding="utf-8") as f:
            f.write(gem_append)
        gemini_size_2 = p2_gemini_jsonl.stat().st_size

        claude_append = (
            json.dumps(
                {
                    "sessionId": "claude-inc-99887766",
                    "type": "assistant",
                    "timestamp": now_iso_2,
                    "message": {
                        "id": "cmsg-2",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "content": [{"type": "tool_use", "name": "Bash"}],
                        "usage": {
                            "input_tokens": 1500,
                            "cache_read_input_tokens": 900,
                            "cache_creation_input_tokens": 200,
                            "output_tokens": 350,
                            "thinking_tokens": 110,
                        },
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        with p2_claude_jsonl.open("a", encoding="utf-8") as f:
            f.write(claude_append)
        claude_size_2 = p2_claude_jsonl.stat().st_size

        snap_step2 = p2_mon.collect_snapshot(
            force_scan=False, now_ts=base_now + 2.0, today_date=today_d1
        )

        # 对照全量重解析结果
        full_main_expected = parse_antigravity_transcript(
            p2_main_jsonl, today_date=today_d1, now_ts=base_now + 2.0
        )
        full_gem_expected = parse_session_jsonl(
            p2_gemini_jsonl, today_date=today_d1, now_ts=base_now + 2.0
        )
        full_claude_expected = parse_claude_code_session(
            p2_claude_jsonl, today_date=today_d1, now_ts=base_now + 2.0
        )

        compare_keys = (
            "session_name",
            "model",
            "steps",
            "tool_calls_count",
            "last_tool",
            "status",
            "context_tokens",
            "input_tokens",
            "cached_tokens",
            "output_tokens",
            "thought_tokens",
            "total_tokens",
            "today_steps",
            "today_input_tokens",
            "today_cached_tokens",
            "today_output_tokens",
            "today_thought_tokens",
            "today_total_tokens",
            "cost_usd",
            "cost_cny",
            "today_cost_usd",
            "today_cost_cny",
            "last_byte_offset",
        )
        inc_main_info = main_state["info"]
        inc_gem_info = gem_state["info"]
        inc_claude_info = claude_state["info"]

        main_match_full = all(
            inc_main_info.get(k) == full_main_expected.get(k) for k in compare_keys
        )
        gem_match_full = all(
            inc_gem_info.get(k) == full_gem_expected.get(k) for k in compare_keys
        )
        claude_match_full = all(
            inc_claude_info.get(k) == full_claude_expected.get(k) for k in compare_keys
        )
        tail_offset_ok = (
            main_state["full_reads"] == 1
            and main_state["incremental_reads"] == 1
            and main_state["bytes_read_last"] == (main_size_2 - main_size_1)
            and main_state["last_byte_offset"] == main_size_2
            and gem_state["full_reads"] == 1
            and gem_state["incremental_reads"] == 1
            and gem_state["bytes_read_last"] == (gemini_size_2 - gemini_size_1)
            and gem_state["last_byte_offset"] == gemini_size_2
            and claude_state["full_reads"] == 1
            and claude_state["incremental_reads"] == 1
            and claude_state["bytes_read_last"] == (claude_size_2 - claude_size_1)
            and claude_state["last_byte_offset"] == claude_size_2
        )
        assert (
            main_match_full and gem_match_full and claude_match_full and tail_offset_ok
        ), "phase2_test: 增量字节偏移读取或与全量聚合一致性断言失败"

        # 9.3 验证跨天（00:00）零 I/O 内存归零
        tomorrow_date = today_d1 + datetime.timedelta(days=1)
        tomorrow_noon_ts = datetime.datetime.combine(
            tomorrow_date, datetime.time(12, 0, 0)
        ).timestamp()
        pre_midnight_full_reads = (
            main_state["full_reads"] + gem_state["full_reads"] + claude_state["full_reads"]
        )
        pre_midnight_inc_reads = (
            main_state["incremental_reads"]
            + gem_state["incremental_reads"]
            + claude_state["incremental_reads"]
        )
        snap_midnight = p2_mon.collect_snapshot(
            force_scan=False, now_ts=tomorrow_noon_ts, today_date=tomorrow_date
        )
        post_midnight_full_reads = (
            main_state["full_reads"] + gem_state["full_reads"] + claude_state["full_reads"]
        )
        post_midnight_inc_reads = (
            main_state["incremental_reads"]
            + gem_state["incremental_reads"]
            + claude_state["incremental_reads"]
        )
        midnight_zero_io_ok = (
            p2_mon.midnight_reset_count == 3
            and post_midnight_full_reads == pre_midnight_full_reads
            and post_midnight_inc_reads == pre_midnight_inc_reads
            and main_state["bytes_read_last"] == 0
            and gem_state["bytes_read_last"] == 0
            and claude_state["bytes_read_last"] == 0
            and snap_midnight["today_stats"]["total_tokens"] == 0
            and snap_midnight["today_stats"]["steps_count"] == 0
            and snap_midnight["today_stats"]["cost_usd"] == 0.0
            and snap_midnight["today_stats"]["cost_cny"] == 0.0
            and inc_main_info["today_total_tokens"] == 0
            and inc_main_info["today_input_tokens"] == 0
            and inc_main_info["today_cached_tokens"] == 0
            and inc_main_info["today_output_tokens"] == 0
            and inc_main_info["today_thought_tokens"] == 0
            and inc_main_info["today_cost_usd"] == 0.0
            and inc_main_info["today_cost_cny"] == 0.0
            and inc_main_info["total_tokens"] == full_main_expected["total_tokens"]
            and inc_gem_info["today_total_tokens"] == 0
            and inc_gem_info["total_tokens"] == full_gem_expected["total_tokens"]
            and inc_claude_info["today_total_tokens"] == 0
            and inc_claude_info["total_tokens"] == full_claude_expected["total_tokens"]
        )
        assert midnight_zero_io_ok, "phase2_test: 跨天（00:00）零 I/O 内存归零断言失败"

        # 9.4 验证文件截断/变小时自动重置 last_byte_offset = 0 并回退全量重新解析
        p2_main_jsonl.write_text("\n".join(main_lines_1) + "\n", encoding="utf-8")
        snap_trunc = p2_mon.collect_snapshot(
            force_scan=False, now_ts=base_now + 5.0, today_date=today_d1
        )
        trunc_fallback_ok = (
            main_state["full_reads"] == 2
            and main_state["last_byte_offset"] == main_size_1
            and main_state["info"]["steps"] == 1
            and main_state["info"]["total_tokens"] == 1750
        )
        assert trunc_fallback_ok, "phase2_test: 文件截断回退全量重解析断言失败"

        # 9.5 验证自适应待机（Adaptive Polling Backoff：30 秒无修改退避至 3000ms，新写入立即切回高速周期）
        prev_refresh_ms = hud.refresh_ms
        hud.refresh_ms = 1000
        hud._timer.setInterval(1000)
        hud.last_activity_time = base_now + 5.0
        p2_mon.last_activity_time = base_now + 5.0

        # 连续 35 秒无任何磁盘文件修改 -> 退避至 3000ms
        idle_35s_ts = base_now + 40.0
        snap_idle = p2_mon.collect_snapshot(
            force_scan=False, now_ts=idle_35s_ts, today_date=today_d1
        )
        hud._apply_snapshot(snap_idle)
        backoff_triggered_ok = (
            snap_idle["is_standby_backoff"] is True
            and hud._is_polling_backoff is True
            and hud._timer.interval() == 3000
        )
        assert backoff_triggered_ok, "phase2_test: 连续 30 秒空闲自动退避至 3000ms 断言失败"

        # 触发新写入 -> 立即恢复至 1000ms 高速周期
        with p2_main_jsonl.open("a", encoding="utf-8") as f:
            f.write(main_append)
        active_ts = idle_35s_ts + 1.0
        snap_rewritten = p2_mon.collect_snapshot(
            force_scan=False, now_ts=active_ts, today_date=today_d1
        )
        hud._apply_snapshot(snap_rewritten)
        backoff_recovered_ok = (
            snap_rewritten["disk_modified"] is True
            and snap_rewritten["is_standby_backoff"] is False
            and hud._is_polling_backoff is False
            and hud._timer.interval() == 1000
        )
        assert backoff_recovered_ok, "phase2_test: 检测到新写入立即恢复高速轮询周期断言失败"

        # 9.6 验证窗口隐藏 / 最小化时暂停渲染（_render_snapshot），恢复显示时立即全量重绘
        hud.showNormal()
        hud.show()
        QApplication.processEvents()
        renders_before_pause = hud._render_count

        # 测试隐藏状态暂停渲染
        hud.hide()
        QApplication.processEvents()
        paused_when_hidden = hud.is_render_paused()
        paused_snap_1 = dict(snap_rewritten)
        paused_today_1 = dict(snap_rewritten["today_stats"])
        paused_today_1["main_total_tokens"] = 777777
        paused_today_1["total_tokens"] = 777777
        paused_snap_1["today_stats"] = paused_today_1
        hud._apply_snapshot(paused_snap_1)
        render_skipped_on_hide = (
            hud._render_paused is True
            and hud._render_count == renders_before_pause
            and hud.card_views[0]["total_val"].text() != "777,777"
        )
        assert paused_when_hidden and render_skipped_on_hide, "phase2_test: 窗口隐藏时暂停 _render_snapshot 断言失败"

        # 恢复显示 -> 立即触发一次全量重绘
        hud.show()
        QApplication.processEvents()
        render_resumed_on_show = (
            hud.is_render_paused() is False
            and hud._render_paused is False
            and hud._render_count == renders_before_pause + 1
            and hud.card_views[0]["total_val"].text() == "777,777"
        )
        assert render_resumed_on_show, "phase2_test: 窗口恢复显示时立即全量重绘断言失败"

        # 测试最小化状态暂停渲染与恢复
        renders_before_min = hud._render_count
        hud.showMinimized()
        QApplication.processEvents()
        paused_when_minimized = hud.is_render_paused()
        paused_snap_2 = dict(snap_rewritten)
        paused_today_2 = dict(snap_rewritten["today_stats"])
        paused_today_2["main_total_tokens"] = 888888
        paused_today_2["total_tokens"] = 888888
        paused_snap_2["today_stats"] = paused_today_2
        hud._apply_snapshot(paused_snap_2)
        render_skipped_on_min = (
            hud._render_paused is True
            and hud._render_count == renders_before_min
        )
        hud.showNormal()
        hud.show()
        QApplication.processEvents()
        render_resumed_on_normal = (
            hud.is_render_paused() is False
            and hud._render_paused is False
            and hud._render_count == renders_before_min + 1
            and hud.card_views[0]["total_val"].text() == "888,888"
        )
        assert (
            paused_when_minimized and render_skipped_on_min and render_resumed_on_normal
        ), "phase2_test: 窗口最小化暂停渲染与恢复重绘断言失败"

        # 恢复原窗口快照与轮询周期
        hud.refresh_ms = prev_refresh_ms
        hud._timer.setInterval(prev_refresh_ms)
        hud._apply_snapshot(snap)

    phase2_io_and_perf_test = {
        "passed": True,
        "incremental_tailing_ok": bool(
            main_match_full and gem_match_full and claude_match_full and tail_offset_ok
        ),
        "truncation_fallback_ok": trunc_fallback_ok,
        "midnight_zero_io_ok": midnight_zero_io_ok,
        "backoff_triggered_ok": backoff_triggered_ok,
        "backoff_recovered_ok": backoff_recovered_ok,
        "render_pause_and_resume_ok": bool(
            render_skipped_on_hide
            and render_resumed_on_show
            and render_skipped_on_min
            and render_resumed_on_normal
        ),
    }

    # 10. 第三阶段综合功能测试（UI 费用呈现、系统托盘与全局热键、历史数据归档与 CSV 导出）
    with tempfile.TemporaryDirectory(prefix="token_hud_p3_test_") as p3_tmp_str:
        p3_root = Path(p3_tmp_str)
        p3_main_dir = p3_root / "antigravity" / "brain"
        p3_sub_dir = p3_root / "gemini_sub"
        p3_sess_dir = p3_main_dir / "p3-session-001" / ".system_generated" / "logs"
        p3_sess_dir.mkdir(parents=True, exist_ok=True)
        (p3_sub_dir / "chats").mkdir(parents=True, exist_ok=True)

        p3_cfg_path = p3_root / "p3_config.json"
        p3_hist_path = p3_root / "token_hud_history.json"
        p3_csv_path = p3_root / "exported_history.csv"
        p3_dlg_csv_path = p3_root / "dlg_exported_history.csv"

        p3_cfg = ConfigManager(config_path=p3_cfg_path)
        p3_cfg.set("main_agent_path", str(p3_main_dir))
        p3_cfg.set(
            "sub_agents",
            [
                {
                    "name": "Argon",
                    "path": str(p3_sub_dir),
                    "parser_type": "gemini_cli_session",
                    "enabled": True,
                }
            ],
        )
        p3_cfg.save()

        p3_day1 = datetime.date(2026, 10, 28)
        p3_day2 = datetime.date(2026, 10, 29)
        p3_ts_d1 = datetime.datetime(2026, 10, 28, 15, 0, 0).timestamp()
        p3_iso_d1 = datetime.datetime.fromtimestamp(p3_ts_d1, tz=datetime.timezone.utc).isoformat()
        p3_ts_d2 = datetime.datetime(2026, 10, 29, 10, 30, 0).timestamp()
        p3_iso_d2 = datetime.datetime.fromtimestamp(p3_ts_d2, tz=datetime.timezone.utc).isoformat()

        p3_main_jsonl = p3_sess_dir / "transcript.jsonl"
        p3_main_jsonl.write_text(
            json.dumps(
                {
                    "type": "USER_INPUT",
                    "status": "RUNNING",
                    "created_at": p3_iso_d1,
                    "content": "<USER_REQUEST>\nPhase3 Day1 Main Prompt\n</USER_REQUEST>\nModel Selection: set to gemini-2.5-pro.",
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": p3_iso_d1,
                    "model": "gemini-2.5-pro",
                    "input_tokens": 200000,
                    "cache_read_tokens": 100000,
                    "output_tokens": 50000,
                    "thinking_tokens": 10000,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        p3_sub_file = p3_sub_dir / "chats" / "session-p3-sub.jsonl"
        p3_sub_file.write_text(
            json.dumps({"sessionId": "sub-p3-001", "startTime": p3_iso_d1}, ensure_ascii=False)
            + "\n"
            + json.dumps(
                {"type": "user", "timestamp": p3_iso_d1, "content": "Phase3 Sub Prompt"},
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "type": "gemini",
                    "id": "sub-turn-d1",
                    "timestamp": p3_iso_d1,
                    "model": "gemini-2.5-flash",
                    "tokens": {
                        "input": 50000,
                        "cached": 20000,
                        "output": 10000,
                        "thoughts": 2000,
                        "total": 62000,
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        # 10.1 验证费用引擎计算与 UI 折算费用显示 (pricing_display_ok)
        # Antigravity PLANNER_RESPONSE 中 input_tokens 为未命中缓存输入，总输入 = input_tokens(400k) + cache_read_tokens(200k) = 600k
        pe = TokenPricingEngine()
        expected_d2_main_cost = pe.calculate_cost(
            input_tokens=600000,
            cached_tokens=200000,
            output_tokens=100000,
            thought_tokens=20000,
            model_name="gemini-2.5-pro",
        )
        expected_d2_sub_cost = pe.calculate_cost(
            input_tokens=100000,
            cached_tokens=50000,
            output_tokens=20000,
            thought_tokens=5000,
            model_name="gemini-2.5-flash",
        )
        expected_today_usd = round(expected_d2_main_cost["usd"] + expected_d2_sub_cost["usd"], 6)

        p3_mon = TokenMonitorCache(config_mgr=p3_cfg, history_path=p3_hist_path)
        # 先在 Day 1 采集一次快照，触发 Day 1 归档准备
        snap_p3_d1 = p3_mon.collect_snapshot(
            force_scan=True, now_ts=p3_ts_d1 + 10.0, today_date=p3_day1
        )
        assert snap_p3_d1["today_cost_usd"] > 0.0, "phase3_test: Day1 today_cost_usd 应大于 0"

        # 在 Day 2 追加写入新的对话轮次，模拟跨天后的新增用量
        with p3_main_jsonl.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": p3_iso_d2,
                        "model": "gemini-2.5-pro",
                        "input_tokens": 400000,
                        "cache_read_tokens": 200000,
                        "output_tokens": 100000,
                        "thinking_tokens": 20000,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        with p3_sub_file.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "gemini",
                        "id": "sub-turn-d2",
                        "timestamp": p3_iso_d2,
                        "model": "gemini-2.5-flash",
                        "tokens": {
                            "input": 100000,
                            "cached": 50000,
                            "output": 20000,
                            "thoughts": 5000,
                            "total": 125000,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        # 跨天进入 Day 2：自动归档 Day 1 并计算 Day 2 今日折算费用
        snap_p3_d2 = p3_mon.collect_snapshot(
            force_scan=False, now_ts=p3_ts_d2 + 10.0, today_date=p3_day2
        )
        cost_calc_ok = (
            abs(float(snap_p3_d2["today_cost_usd"]) - expected_today_usd) < 1e-5
            and float(snap_p3_d2["today_cost_cny"]) > 0.0
            and float(snap_p3_d2["main_session"]["today_cost_usd"]) > 0.0
            and float(snap_p3_d2["active_session"]["today_cost_usd"]) > 0.0
        )
        assert cost_calc_ok, f"phase3_test: 费用引擎计算不匹配 ({snap_p3_d2['today_cost_usd']} vs {expected_today_usd})"

        prev_cfg_mgr = hud.config_mgr
        prev_mon = hud.monitor
        hud.config_mgr = p3_cfg
        hud.monitor = p3_mon
        hud.showNormal()
        hud.show()
        hud._apply_snapshot(snap_p3_d2)
        QApplication.processEvents()

        summary_cost_lbl_text = hud.card_views[0]["cost_val"].text()
        header_compact_text = hud._grand_total_compact_text
        header_hover_text = hud._grand_total_hover_text
        expected_cost_str = format_cost_usd(expected_today_usd)

        # 检查侧边栏会话行的第二行副标题与 ToolTip 是否包含折算费用
        sidebar_cost_ok = False
        for row_w in hud._sidebar_rows.values():
            if "≈ $" in row_w._raw_meta and "折算费用" in row_w.toolTip():
                sidebar_cost_ok = True
                break

        # 选中单个会话后验证卡片折算费用切换为本会话折合
        p3_main_fp = str(snap_p3_d2["main_session"]["file_path"])
        hud._select_history_session("main", p3_main_fp)
        pinned_cost_lbl_text = hud.card_views[0]["cost_val"].text()
        hud._reset_pinned_session()

        pricing_display_ok = bool(
            cost_calc_ok
            and ("今日折合" in summary_cost_lbl_text and expected_cost_str in summary_cost_lbl_text)
            and (expected_cost_str in header_compact_text and "今日折合" in header_hover_text)
            and ("本会话折合" in pinned_cost_lbl_text and "$" in pinned_cost_lbl_text)
            and sidebar_cost_ok
        )
        assert pricing_display_ok, (
            f"phase3_test: UI 费用呈现断言失败 (summary={summary_cost_lbl_text}, "
            f"pinned={pinned_cost_lbl_text}, sidebar_ok={sidebar_cost_ok})"
        )

        # 10.2 验证系统托盘菜单项与全局热键常量/切换逻辑 (tray_and_hotkey_ok)
        tray_labels = hud.get_tray_menu_labels()
        expected_tray_labels = [
            "显示 / 隐藏悬浮窗",
            "立即重新扫描",
            "全量重新扫描 (完整历史)",
            "导出历史为 CSV...",
            "设置...",
            "退出",
        ]
        tray_menu_ok = (
            hud.tray_icon is not None
            and hud.tray_menu is not None
            and tray_labels == expected_tray_labels
            and not hud.tray_icon.icon().isNull()
        )
        assert tray_menu_ok, f"phase3_test: 托盘菜单项断言失败 ({tray_labels})"

        # 10.2.1 验证历史日志按需懒加载（方案 1）与显式全量重扫（方案 3）
        lazy_root = p3_root / "lazy_and_full_rescan"
        lazy_main_dir = lazy_root / "brain"
        lazy_sub_dir = lazy_root / "sub"
        (lazy_main_dir / "hist-main-01" / ".system_generated" / "logs").mkdir(parents=True, exist_ok=True)
        (lazy_main_dir / "hist-main-02" / ".system_generated" / "logs").mkdir(parents=True, exist_ok=True)
        (lazy_sub_dir / "chats").mkdir(parents=True, exist_ok=True)

        lazy_main_f1 = lazy_main_dir / "hist-main-01" / ".system_generated" / "logs" / "transcript.jsonl"
        lazy_main_f2 = lazy_main_dir / "hist-main-02" / ".system_generated" / "logs" / "transcript.jsonl"
        lazy_sub_f1 = lazy_sub_dir / "chats" / "session-hist-sub-01.jsonl"

        hist_content_main = p3_main_jsonl.read_text(encoding="utf-8")
        hist_content_sub = p3_sub_file.read_text(encoding="utf-8")
        lazy_main_f1.write_text(hist_content_main, encoding="utf-8")
        lazy_main_f2.write_text(hist_content_main, encoding="utf-8")
        lazy_sub_f1.write_text(hist_content_sub, encoding="utf-8")

        # 将文件 mtime 设为 2 天前（早于今日 00:00）
        old_mtime = time.time() - 86400 * 2
        os.utime(lazy_main_f1, (old_mtime, old_mtime))
        os.utime(lazy_main_f2, (old_mtime - 60, old_mtime - 60))
        os.utime(lazy_sub_f1, (old_mtime - 120, old_mtime - 120))

        lazy_cfg = ConfigManager(config_path=lazy_root / "lazy_cfg.json")
        lazy_cfg.save(
            {
                "main_agent_path": str(lazy_main_dir),
                "sub_agents": [
                    {
                        "id": "sub_hist",
                        "name": "Argon",
                        "path": str(lazy_sub_dir),
                        "parser_type": "gemini_cli_session",
                        "enabled": True,
                    }
                ],
            }
        )
        lazy_mon = TokenMonitorCache(config_mgr=lazy_cfg, history_path=lazy_root / "lazy_hist.json")
        snap_lazy_init = lazy_mon.collect_snapshot(force_scan=True)
        init_main_list = snap_lazy_init.get("all_main_sessions", [])
        init_sub_list = snap_lazy_init.get("all_sub_sessions", [])
        assert len(init_main_list) == 2 and len(init_sub_list) == 1, "lazy_test: 历史会话数量断言失败"
        assert all(
            bool(s.get("is_lazy_loaded")) and int(s.get("total_tokens", 0)) == 0
            for s in init_main_list + init_sub_list
        ), "lazy_test: 冷启动非今日历史会话应标记 is_lazy_loaded=True 且跳过全量解析"

        # 测试方案 1：直接调用 resolve_single_session_full 按需单文件加载
        resolved_single = lazy_mon.resolve_single_session_full(
            lazy_main_f1, parser_type="antigravity_transcript"
        )
        assert (
            isinstance(resolved_single, dict)
            and int(resolved_single.get("total_tokens", 0)) > 0
            and resolved_single.get("is_lazy_loaded") is False
            and lazy_mon.get_file_cache_state(lazy_main_f1)["is_lazy_loaded"] is False
        ), "lazy_test: resolve_single_session_full 按需单文件解析断言失败"

        # 测试方案 1 UI 交互：在侧边栏点击另一条未加载的历史会话（lazy_main_f2），触发单文件全量加载
        prev_hud_mon = hud.monitor
        prev_hud_cfg = hud.config_mgr
        try:
            hud.monitor = lazy_mon
            hud.config_mgr = lazy_cfg
            hud._apply_snapshot(snap_lazy_init)
            assert (
                ("main", str(lazy_main_f2)) in hud._sidebar_rows
                and ("sub", str(lazy_sub_f1)) in hud._sidebar_rows
            ), "lazy_test: 懒加载历史条目应在侧边栏正常显示"
            hud._select_history_session("main", str(lazy_main_f2))
            f2_sess_after_click = next(
                (s for s in hud._last_main_sessions if str(s.get("file_path")) == str(lazy_main_f2)),
                None,
            )
            assert (
                f2_sess_after_click is not None
                and int(f2_sess_after_click.get("total_tokens", 0)) > 0
                and f2_sess_after_click.get("is_lazy_loaded") is False
            ), "lazy_test: 点击侧边栏历史会话触发按需单文件解析断言失败"

            # 此时 lazy_sub_f1 仍处于未点击的懒加载状态，测试方案 3：hud.rescan_now(full_history=True)
            assert lazy_mon.get_file_cache_state(lazy_sub_f1)["is_lazy_loaded"] is True
            snap_full_rescan = hud.rescan_now(full_history=True)
            full_all_sessions = (
                snap_full_rescan.get("all_main_sessions", [])
                + snap_full_rescan.get("all_sub_sessions", [])
            )
            assert len(full_all_sessions) == 3 and all(
                int(s.get("total_tokens", 0)) > 0 and s.get("is_lazy_loaded") is False
                for s in full_all_sessions
            ), "lazy_test: hud.rescan_now(full_history=True) 全量历史重扫断言失败"
            lazy_load_and_full_rescan_ok = True
            hud._reset_pinned_session()
        finally:
            hud.monitor = prev_hud_mon
            hud.config_mgr = prev_hud_cfg
            hud._apply_snapshot(snap_p3_d2)

        hotkey_consts_ok = (
            hud.MOD_CONTROL == 0x0002
            and hud.MOD_ALT == 0x0001
            and hud.VK_T == 0x54
            and hud.HOTKEY_ID == 0x9001
            and hud.WM_HOTKEY == 0x0312
        )
        assert hotkey_consts_ok, "phase3_test: 全局热键常量断言失败"

        # 验证 nativeEvent 拦截 WM_HOTKEY 触发窗口显示/隐藏切换
        hud.showNormal()
        hud.show()
        QApplication.processEvents()
        vis_before = hud.isVisible()
        fake_msg = wintypes.MSG()
        fake_msg.hwnd = int(hud.winId())
        fake_msg.message = hud.WM_HOTKEY
        fake_msg.wParam = hud.HOTKEY_ID
        fake_msg.lParam = 0
        handled_1, res_1 = hud.nativeEvent(b"windows_generic_MSG", ctypes.addressof(fake_msg))
        vis_after_1 = hud.isVisible()
        handled_2, res_2 = hud.nativeEvent(b"windows_generic_MSG", ctypes.addressof(fake_msg))
        vis_after_2 = hud.isVisible()
        hotkey_toggle_ok = (
            vis_before is True
            and handled_1 is True
            and res_1 == 0
            and vis_after_1 is False
            and handled_2 is True
            and res_2 == 0
            and vis_after_2 is True
        )
        assert hotkey_toggle_ok, "phase3_test: WM_HOTKEY 窗口显示/隐藏切换断言失败"

        tray_and_hotkey_ok = bool(tray_menu_ok and hotkey_consts_ok and hotkey_toggle_ok)

        # 10.3 验证历史记录持久化与 CSV 导出内容格式 (history_persistence_and_csv_ok)
        saved_hist_file = p3_mon.save_history(history_path=p3_hist_path)
        assert saved_hist_file.exists(), "phase3_test: token_hud_history.json 未生成"
        hist_json = json.loads(saved_hist_file.read_text(encoding="utf-8"))
        days_map = hist_json.get("days", {})
        hist_json_ok = (
            isinstance(days_map, dict)
            and "2026-10-28" in days_map
            and "2026-10-29" in days_map
            and int(days_map["2026-10-29"].get("total_tokens", 0)) == (700000 + 125000)
            and float(days_map["2026-10-29"].get("cost_usd", 0.0)) > 0.0
            and "by_agent" in days_map["2026-10-29"]
            and isinstance(days_map["2026-10-29"].get("records"), list)
        )
        assert hist_json_ok, "phase3_test: token_hud_history.json 内容结构校验失败"

        exported_file = export_history_to_csv(
            output_path=p3_csv_path,
            history_path=p3_hist_path,
            monitor=p3_mon,
        )
        assert exported_file.exists(), "phase3_test: export_history_to_csv 未生成 CSV 文件"
        with exported_file.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            csv_headers = list(reader.fieldnames or [])
            csv_rows = list(reader)

        expected_csv_headers = [
            "Date",
            "Agent",
            "Model",
            "InputTokens",
            "CachedTokens",
            "OutputTokens",
            "ThoughtTokens",
            "TotalTokens",
            "CostUSD",
        ]
        csv_format_ok = (
            csv_headers == expected_csv_headers
            and CSV_FIELDNAMES == expected_csv_headers
            and len(csv_rows) >= 2
            and any(
                r["Date"] == "2026-10-29"
                and r["Agent"] == "Antigravity"
                and r["Model"] == "gemini-2.5-pro"
                and int(r["InputTokens"]) == 600000
                and float(r["CostUSD"]) > 0.0
                for r in csv_rows
            )
            and any(
                r["Date"] == "2026-10-29"
                and r["Agent"] == "Argon"
                and r["Model"] == "gemini-2.5-flash"
                and int(r["InputTokens"]) == 100000
                and float(r["CostUSD"]) > 0.0
                for r in csv_rows
            )
        )
        assert csv_format_ok, f"phase3_test: CSV 导出表头或行内容校验失败 (headers={csv_headers}, rows={csv_rows})"

        # 验证 SettingsDialog 也可触发 CSV 导出，并测试模型定价表格初始化、自定义费率持久化与折扣/涨价实时重算
        p3_dlg = SettingsDialog(hud, p3_cfg)
        dlg_csv_res = p3_dlg.export_history_to_csv(p3_dlg_csv_path)
        dlg_export_ok = dlg_csv_res is not None and Path(dlg_csv_res).exists()
        assert dlg_export_ok, "phase3_test: SettingsDialog 触发导出 CSV 失败"

        # 10.3.1 验证 SettingsDialog 中的定价控件/表格初始化及数值加载
        expected_pricing_headers = [
            "模型名称",
            "输入单价 ($/1M)",
            "缓存单价 ($/1M)",
            "输出单价 ($/1M)",
            "思考单价 ($/1M)",
        ]
        actual_pricing_headers = [
            p3_dlg.pricing_table.horizontalHeaderItem(col).text()
            for col in range(p3_dlg.pricing_table.columnCount())
        ]
        table_model_names = [
            p3_dlg.pricing_table.item(r, 0).text()
            for r in range(p3_dlg.pricing_table.rowCount())
            if p3_dlg.pricing_table.item(r, 0) is not None
        ]
        required_preset_models = {
            "Gemini 3.8 Flash",
            "Gemini 3.8 Pro",
            "Gemini 4 Argon",
            "Gemini 2.5 Pro",
            "Gemini 2.5 Flash",
            "Gemini 2.0 Flash",
            "Gemini 1.5 Pro",
            "Gemini 1.5 Flash",
            "Claude 3.7 Sonnet",
            "Claude 3.5 Sonnet",
            "Claude 3.5 Haiku",
            "Claude 3 Opus",
            "GPT-4o",
            "GPT-4o mini",
            "OpenAI o1",
            "OpenAI o3-mini",
            "DeepSeek-V3 (deepseek-chat)",
            "DeepSeek-R1 (deepseek-reasoner)",
            "default",
        }
        init_g25_pro_rates = p3_dlg.get_model_pricing_rates("gemini-2.5-pro")
        init_g38_flash_rates = p3_dlg.get_model_pricing_rates("Gemini 3.8 Flash")
        init_gpt4o_rates = p3_dlg.get_model_pricing_rates("gpt-4o")
        init_deepseek_v3_rates = p3_dlg.get_model_pricing_rates("deepseek-v3")
        init_deepseek_r1_rates = p3_dlg.get_model_pricing_rates("deepseek-r1")
        pricing_ui_init_ok = (
            hasattr(p3_dlg, "pricing_table")
            and hasattr(p3_dlg, "usd_to_cny_spin")
            and hasattr(p3_dlg, "reset_pricing_btn")
            and p3_dlg.pricing_scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            and p3_dlg.pricing_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            and p3_dlg.pricing_table.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            and p3_dlg.pricing_table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            and actual_pricing_headers == expected_pricing_headers
            and required_preset_models.issubset(set(table_model_names))
            and abs(float(p3_dlg.usd_to_cny_spin.value()) - 7.25) < 1e-6
            and abs(float(init_g25_pro_rates.get("input", 0.0)) - 1.25) < 1e-6
            and abs(float(init_g25_pro_rates.get("cached", 0.0)) - 0.125) < 1e-6
            and abs(float(init_g25_pro_rates.get("output", 0.0)) - 10.00) < 1e-6
            and abs(float(init_g38_flash_rates.get("input", 0.0)) - 0.15) < 1e-6
            and abs(float(init_gpt4o_rates.get("input", 0.0)) - 2.50) < 1e-6
            and abs(float(init_deepseek_v3_rates.get("input", 0.0)) - 0.27) < 1e-6
            and abs(float(init_deepseek_r1_rates.get("output", 0.0)) - 2.19) < 1e-6
        )
        assert pricing_ui_init_ok, (
            f"phase3_test: SettingsDialog 定价表格初始化校验失败 "
            f"(headers={actual_pricing_headers}, models={table_model_names})"
        )

        # 10.3.2 验证自定义费率（5折折扣与2倍涨价）与汇率修改后保存至 config.json 持久化及主窗口实时重算
        baseline_today_usd = float(snap_p3_d2.get("today_cost_usd", 0.0) or 0.0)
        # 对当天的 gemini-2.5-pro 与 gemini-2.5-flash 全部单价打 5 折，汇率改为 8.00
        for m_name, base_r in (
            ("gemini-2.5-pro", {"input": 1.25, "cached": 0.125, "output": 10.0, "thought": 10.0}),
            ("gemini-2.5-flash", {"input": 0.30, "cached": 0.03, "output": 2.50, "thought": 2.50}),
        ):
            for f_key, f_val in base_r.items():
                assert p3_dlg.set_model_pricing_rate(m_name, f_key, f_val * 0.5)
        p3_dlg.usd_to_cny_spin.setValue(8.00)
        QApplication.processEvents()

        reloaded_p3_cfg = ConfigManager(config_path=p3_cfg_path)
        persisted_pricing = reloaded_p3_cfg.get("pricing", {})
        persisted_rate = float(reloaded_p3_cfg.get("usd_to_cny_rate", 0.0) or 0.0)
        pricing_persistence_ok = (
            p3_cfg_path.is_file()
            and abs(persisted_rate - 8.00) < 1e-6
            and isinstance(persisted_pricing, dict)
            and abs(float(persisted_pricing.get("gemini-2.5-pro", {}).get("input", 0.0)) - 0.625) < 1e-6
            and abs(float(persisted_pricing.get("gemini-2.5-pro", {}).get("output", 0.0)) - 5.00) < 1e-6
            and abs(float(persisted_pricing.get("gemini-2.5-flash", {}).get("input", 0.0)) - 0.15) < 1e-6
        )
        assert pricing_persistence_ok, "phase3_test: 自定义费率与汇率持久化至 config.json 断言失败"

        discount_snap = hud._last_snap
        discount_today_usd = float(discount_snap.get("today_cost_usd", 0.0) or 0.0)
        discount_today_cny = float(discount_snap.get("today_cost_cny", 0.0) or 0.0)
        discount_cost_str = format_cost_usd(discount_today_usd)
        discount_recalc_ok = (
            abs(discount_today_usd - round(baseline_today_usd * 0.5, 6)) < 1e-5
            and abs(discount_today_cny - round(discount_today_usd * 8.00, 6)) < 1e-4
            and discount_cost_str in hud.card_views[0]["cost_val"].text()
            and discount_cost_str in hud._grand_total_compact_text
        )
        assert discount_recalc_ok, (
            f"phase3_test: 5折自定义费率重算断言失败 "
            f"(baseline={baseline_today_usd}, discount={discount_today_usd}, cny={discount_today_cny})"
        )

        # 再模拟 2 倍涨价，验证涨价重算
        for m_name, base_r in (
            ("gemini-2.5-pro", {"input": 1.25, "cached": 0.125, "output": 10.0, "thought": 10.0}),
            ("gemini-2.5-flash", {"input": 0.30, "cached": 0.03, "output": 2.50, "thought": 2.50}),
        ):
            for f_key, f_val in base_r.items():
                assert p3_dlg.set_model_pricing_rate(m_name, f_key, f_val * 2.0)
        QApplication.processEvents()
        surge_snap = hud._last_snap
        surge_today_usd = float(surge_snap.get("today_cost_usd", 0.0) or 0.0)
        surge_cost_str = format_cost_usd(surge_today_usd)
        surge_recalc_ok = (
            abs(surge_today_usd - round(baseline_today_usd * 2.0, 6)) < 1e-5
            and surge_cost_str in hud.card_views[0]["cost_val"].text()
        )
        assert surge_recalc_ok, (
            f"phase3_test: 2倍涨价费率重算断言失败 (baseline={baseline_today_usd}, surge={surge_today_usd})"
        )

        # 点击「重置为默认费率」按钮，验证恢复官方默认单价与默认汇率 7.25
        p3_dlg.reset_pricing_btn.click()
        QApplication.processEvents()
        reset_snap = hud._last_snap
        reset_today_usd = float(reset_snap.get("today_cost_usd", 0.0) or 0.0)
        reset_pricing_ok = (
            abs(float(p3_dlg.usd_to_cny_spin.value()) - 7.25) < 1e-6
            and abs(reset_today_usd - baseline_today_usd) < 1e-5
            and format_cost_usd(baseline_today_usd) in hud.card_views[0]["cost_val"].text()
        )
        assert reset_pricing_ok, "phase3_test: 重置为默认费率按钮恢复与重算断言失败"

        # ------------------------------------------------------------------
        # 在线汇率/价格表同步、代理探测、范围校验与离线异常降级 (Fail-Safe) 断言
        # ------------------------------------------------------------------
        # 1. 代理探测优先级（用户自定义 -> 环境变量 -> 系统代理）与规范化
        assert normalize_proxy_url("127.0.0.1:7897") == "http://127.0.0.1:7897"
        assert normalize_proxy_url("  http://127.0.0.1:10809  ") == "http://127.0.0.1:10809"
        assert resolve_effective_proxy("127.0.0.1:7897") == "http://127.0.0.1:7897"
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://127.0.0.1:7890", "HTTP_PROXY": "http://127.0.0.1:7891"},
            clear=False,
        ):
            assert resolve_effective_proxy("") == "http://127.0.0.1:7890"
            assert resolve_effective_proxy("127.0.0.1:9999") == "http://127.0.0.1:9999"
            env_proxies = resolve_proxy_settings("")
            assert env_proxies.get("https") == "http://127.0.0.1:7890"
            assert env_proxies.get("http") == "http://127.0.0.1:7891"

        # 2. 汇率范围校验（5.0 ~ 10.0 合法，超出范围或非法格式返回 None）
        assert MIN_VALID_USD_TO_CNY_RATE == 5.0 and MAX_VALID_USD_TO_CNY_RATE == 10.0
        assert parse_usd_to_cny_payload({"rates": {"CNY": 7.28}}) == 7.28
        assert parse_usd_to_cny_payload({"usd_to_cny_rate": 7.31}) == 7.31
        assert parse_usd_to_cny_payload({"rates": {"CNY": 4.99}}) is None
        assert parse_usd_to_cny_payload({"rates": {"CNY": 10.01}}) is None
        assert parse_usd_to_cny_payload({"rates": {"CNY": True}}) is None
        assert parse_usd_to_cny_payload("invalid") is None

        # 3. 离线/异常降级 (Fail-Safe) 断言：断网、超时或无效响应均安全返回 None，绝不抛异常
        assert safe_fetch_json("http://127.0.0.1:1/unreachable", timeout=0.2) is None
        with patch("hud_core.pricing.safe_fetch_json", return_value=None):
            assert fetch_online_usd_rate(proxy="127.0.0.1:7897", timeout=0.2) is None
            assert fetch_remote_pricing_table(proxy="127.0.0.1:7897", timeout=0.2) is None

        # 4. SettingsDialog 槽函数与 UI 联动断言（成功与失败降级）
        p3_dlg.proxy_edit.setText("127.0.0.1:7897")
        p3_dlg._last_remote_pricing_table = {
            "gemini-2.5-pro": {"input": 1.50, "cached": 0.15, "output": 12.0, "thought": 12.0},
        }
        p3_dlg._on_online_fetch_finished(True, "✓ 汇率已更新为 7.28", 7.28, 1)
        QApplication.processEvents()
        assert p3_dlg.fetch_now_btn.isEnabled()
        assert "7.28" in p3_dlg.fetch_status_lbl.text()
        assert abs(float(p3_dlg.usd_to_cny_spin.value()) - 7.28) < 1e-6
        assert abs(float(p3_cfg.get("usd_to_cny_rate", 0.0)) - 7.28) < 1e-6

        # 模拟离线更新失败槽函数回调，确认保留当前汇率 7.28 不被清零
        p3_dlg._on_online_fetch_finished(False, "✗ 汇率更新失败，已保留本地配置", 0.0, 0)
        QApplication.processEvents()
        assert "保留本地配置" in p3_dlg.fetch_status_lbl.text()
        assert abs(float(p3_dlg.usd_to_cny_spin.value()) - 7.28) < 1e-6

        # 恢复 7.25 默认费率
        p3_dlg.reset_pricing_btn.click()
        QApplication.processEvents()
        p3_dlg.close()

        online_pricing_and_proxy_ok = True

        custom_pricing_test_ok = bool(
            pricing_ui_init_ok
            and pricing_persistence_ok
            and discount_recalc_ok
            and surge_recalc_ok
            and reset_pricing_ok
            and online_pricing_and_proxy_ok
        )
        history_persistence_and_csv_ok = bool(hist_json_ok and csv_format_ok and dlg_export_ok)

        # 恢复原配置与监控实例
        hud.config_mgr = prev_cfg_mgr
        hud.monitor = prev_mon
        hud.monitor.sync_pricing_from_config()
        hud._apply_snapshot(snap)

    phase3_feature_test = {
        "passed": bool(
            pricing_display_ok
            and custom_pricing_test_ok
            and tray_and_hotkey_ok
            and lazy_load_and_full_rescan_ok
            and history_persistence_and_csv_ok
        ),
        "pricing_display_ok": pricing_display_ok,
        "custom_pricing_test_ok": custom_pricing_test_ok,
        "tray_and_hotkey_ok": tray_and_hotkey_ok,
        "lazy_load_and_full_rescan_ok": lazy_load_and_full_rescan_ok,
        "history_persistence_and_csv_ok": history_persistence_and_csv_ok,
    }

    all_main = snap.pop("all_main_sessions", [])
    all_sub = snap.pop("all_sub_sessions", [])
    raw_sba = snap.get("sessions_by_agent", {})
    raw_tba = snap.get("today_by_agent", {})
    required_today_keys = {
        "sessions_count",
        "steps_count",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "thought_tokens",
        "total_tokens",
    }
    agent_aggregation_ok = (
        isinstance(raw_sba, dict)
        and "main" in raw_sba
        and isinstance(raw_tba, dict)
        and "main" in raw_tba
        and required_today_keys.issubset(set(raw_tba["main"].keys()))
    )
    assert agent_aggregation_ok, "self_test: sessions_by_agent / today_by_agent 聚合结构断言失败"
    snap["sessions_by_agent"] = {
        aid: sess_list[:3] for aid, sess_list in raw_sba.items()
    }
    snap["sessions_by_agent_counts"] = {
        aid: len(sess_list) for aid, sess_list in raw_sba.items()
    }
    snap["agent_aggregation_test"] = {
        "passed": agent_aggregation_ok,
        "agent_ids": list(raw_tba.keys()),
        "sessions_by_agent_counts": snap["sessions_by_agent_counts"],
        "today_by_agent": raw_tba,
    }
    grand_main_tok = sum(int(s.get("total_tokens", 0)) for s in all_main)
    grand_sub_tok = sum(int(s.get("total_tokens", 0)) for s in all_sub)
    snap["grand_total_stats"] = {
        "main_total_tokens": grand_main_tok,
        "sub_total_tokens": grand_sub_tok,
        "grand_total_tokens": grand_main_tok + grand_sub_tok,
        "header_label_text": hud.grand_total_lbl.cget("text"),
        "header_hover_text": hud._grand_total_hover_text,
    }
    snap["all_main_sessions_count"] = len(all_main)
    snap["all_sub_sessions_count"] = len(all_sub)
    snap["all_main_sessions_preview"] = all_main[:3]
    snap["all_sub_sessions_preview"] = all_sub[:3]
    snap["dwm_info"] = hud.dwm_info
    snap["active_card_title"] = hud.title_lbl.cget("text")
    snap["card_geometries"] = card_geometries
    snap["sidebar_geometry"] = sidebar_geom
    snap["drag_test"] = drag_test
    snap["resize_test"] = resize_test
    snap["splitter_test"] = splitter_test
    snap["config_test"] = config_test
    snap["path_detection_test"] = path_detection_test
    snap["theme_and_settings_test"] = theme_and_settings_test
    snap["settings_test"] = theme_and_settings_test
    snap["phase2_io_and_perf_test"] = phase2_io_and_perf_test
    snap["phase3_feature_test"] = phase3_feature_test
    hud.close()
    sample_sanitized = sanitize_snapshot_for_output(
        {
            "main_agent_path": str(Path.home() / ".gemini" / "antigravity" / "brain"),
            "upper_path": str(Path.home()).upper() + "\\.gemini\\tmp",
            "main_session": {
                "file_path": "C:\\Users\\TestUser\\.gemini\\transcript.jsonl",
                "session_name": "测试真实对话敏感标题内容",
            },
            "lazy_session": {
                "session_name": "72190b1e",
            },
        }
    )
    assert isinstance(sample_sanitized, dict)
    assert sample_sanitized["main_agent_path"].startswith("~")
    assert sample_sanitized["upper_path"].startswith("~")
    assert sample_sanitized["main_session"]["file_path"].startswith("~")
    assert sample_sanitized["main_session"]["session_name"] == "测试真实*** [Session Masked]"
    assert sample_sanitized["lazy_session"]["session_name"] == "72190b1e"

    sanitized_snap = sanitize_snapshot_for_output(snap)
    print(json.dumps(sanitized_snap, ensure_ascii=False, indent=2))
    return 0


_GENERIC_WINDOWS_USER_RE = re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\"'\s]+", re.IGNORECASE)
_HEX_PREFIX_ID_RE = re.compile(r"^[0-9a-fA-F]{6,12}$")


def _sanitize_path_str(val: str, home_patterns: list[re.Pattern[str]]) -> str:
    """将字符串中出现的本地用户主目录路径不区分大小写地替换为通用占位符 `~`。"""
    if not val:
        return val
    res = val
    for pat in home_patterns:
        res = pat.sub("~", res)
    res = _GENERIC_WINDOWS_USER_RE.sub("~", res)
    return res


def _sanitize_session_name(name: str) -> str:
    """对真实对话标题进行掩码脱敏，保留短十六进制 ID 或占位符。"""
    if not isinstance(name, str):
        return name
    stripped = name.strip()
    if not stripped or stripped == "[Session Masked]":
        return stripped
    if _HEX_PREFIX_ID_RE.fullmatch(stripped):
        return stripped
    prefix = stripped[:4]
    return f"{prefix}*** [Session Masked]"


def sanitize_snapshot_for_output(data: object) -> object:
    """在输出自测快照前递归清洗本地用户路径与真实会话标题。"""
    home_str = str(Path.home()).rstrip("\\/")
    raw_variants = {home_str, home_str.replace("\\", "/"), home_str.replace("/", "\\")}
    home_patterns = [
        re.compile(re.escape(v), re.IGNORECASE)
        for v in sorted(raw_variants, key=len, reverse=True)
        if v
    ]

    def _walk(node: object, key_name: str = "") -> object:
        if isinstance(node, dict):
            return {k: _walk(v, str(k)) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item, key_name) for item in node]
        if isinstance(node, str):
            if key_name == "session_name":
                return _sanitize_session_name(node)
            return _sanitize_path_str(node, home_patterns)
        return node

    return _walk(data)

