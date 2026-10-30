# -*- coding: utf-8 -*-
"""
token_hud.py - Antigravity 主 Agent 与子 Agent 实时双引擎 Token 监控置顶悬浮窗入口门面。
"""
from __future__ import annotations

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import hud_core as _hud_core  # noqa: E402
from hud_core import (  # noqa: E402,F401
    ANTIGRAVITY_BRAIN_ROOT, DEFAULT_CONFIG, DEFAULT_MODEL_PRICING_PER_1M,
    DEFAULT_USD_TO_CNY_RATE, GEMINI_ROOT, GEMINI_TMP_ROOT, LIVE_STATE_FILE,
    MUTEX_NAME, PARSER_REGISTRY, THEME_PRESETS, AntigravityBrainParser,
    ClaudeCodeSessionParser, ConfigManager, GeminiCliSessionParser,
    ResizeGripWidget, SettingsDialog, SidebarRowWidget, SplitterHandleWidget,
    TokenHUDWindow, TokenMonitorCache, TokenPricingEngine, _CompatLabelAdapter,
    _CompatWidgetGeoAdapter, _derive_theme_palette, _discover_antigravity_files,
    _discover_claude_code_files, _discover_gemini_cli_files,
    _extract_user_prompt_first_line, _HEX_COLOR_RE, _hex_to_rgba_css, _HoverLabel,
    _probe_antigravity_brain_dir, _probe_sub_agent_status,
    _read_jsonl_incremental_records, _SnapshotBridge, _truncate_session_name,
    acquire_single_instance_mutex, apply_win11_mica_and_rounded_corners,
    auto_detect_antigravity_path, bind_interactive_desktop,
    bring_existing_hud_to_front, enable_dpi_awareness, export_history_to_csv,
    format_compact_tokens, format_cost_cny, format_cost_usd,
    format_duration, format_num, format_short_time,
    parse_antigravity_transcript, parse_claude_code_session, parse_iso_timestamp,
    parse_session_jsonl, reset_session_today_stats, resolve_top_level_hwnd,
    run_self_test, sanitize_snapshot_for_output,
)

__version__ = "1.2.0"

__all__ = list(_hud_core.__all__)
if "__version__" not in __all__:
    __all__.insert(0, "__version__")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Antigravity 实时 Token 监控置顶悬浮窗 v{__version__} (PySide6)"
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"Token HUD v{__version__}",
        help="显示程序版本号 (1.2.0) 并退出",
    )
    parser.add_argument("-s", "--session", help="指定优先锁定的会话 ID（前缀或完整 ID）")
    parser.add_argument("-p", "--project", help="指定优先过滤的项目目录名")
    parser.add_argument("-i", "--interval", type=int, default=1000, help="界面刷新间隔毫秒数（默认 1000ms）")
    parser.add_argument("--card", type=int, choices=[0], default=0, help="初始展示的卡片索引：0=Token 总用量")
    parser.add_argument("--auto-close-idle", type=float, default=0.0, help="会话完成后自动关闭等待秒数")
    parser.add_argument("--duration", type=float, default=0.0, help="窗口运行指定秒数后自动退出")
    parser.add_argument("-c", "--config", help="指定配置文件路径")
    parser.add_argument("--reset-config", action="store_true", help="重置配置文件为默认值")
    parser.add_argument("--self-test", action="store_true", help="运行非阻塞自检并打印 JSON 报告后退出")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test(args.session, args.project, args.interval, args.config))

    config_mgr = ConfigManager(config_path=args.config)
    if args.reset_config:
        config_mgr.save(DEFAULT_CONFIG)

    if not acquire_single_instance_mutex():
        bring_existing_hud_to_front()
        sys.exit(0)

    bind_interactive_desktop()
    enable_dpi_awareness()
    monitor = TokenMonitorCache(config_mgr=config_mgr)
    hud = TokenHUDWindow(
        monitor=monitor,
        config_mgr=config_mgr,
        refresh_ms=args.interval,
        target_session=args.session,
        target_project=args.project,
        auto_close_idle_sec=args.auto_close_idle,
    )
    if args.card in (0, 1, 2):
        hud._show_card(args.card)
    hud.run(duration_sec=args.duration)


if __name__ == "__main__":
    main()
