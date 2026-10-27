# -*- coding: utf-8 -*-
"""
token_hud.py - Antigravity 主 Agent 与 Gemini CLI (gemini-4-argon) 子 Agent 实时双引擎 Token 监控置顶悬浮窗。

基于 PySide6 (Qt 6 单句柄 Alien Widgets 硬件加速渲染) + Windows 11 DWM API 实现：
1. 单窗口常驻左右双栏布局：
   - 左侧常驻全量历史会话子菜单（无滚动条静默滚轮滑动、QPainter 零重排高亮切换、组件永久复用）
   - 右侧默认展示 Token 总用量视图；仅在左侧列表中显式选中某个子 Agent 历史对话时切换为该子 Agent 会话用量明细，单击顶部标题栏可取消选中恢复总用量视图。
2. 标题栏实时展示所有项目主从 Agent 累计总 Token（悬停切换完整千分位明细）。
3. 后台独立工作线程采集磁盘 JSONL 快照，UI 主线程零 I/O 阻塞。
4. Windows 11 原生 DWM 圆角 (DWMWCP_ROUND)、沉浸式暗黑模式与 Mica / Mica Alt 材质特效。
5. Win32 命名互斥体 AntigravityTokenHUDMutex 单实例保护，防止多开重叠。
"""
import argparse
import ctypes
import datetime
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    from ctypes import wintypes

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


GEMINI_ROOT = Path.home() / ".gemini"
GEMINI_TMP_ROOT = GEMINI_ROOT / "tmp"
ANTIGRAVITY_BRAIN_ROOT = GEMINI_ROOT / "antigravity" / "brain"
LIVE_STATE_FILE = GEMINI_TMP_ROOT / "argon_live_state.json"

THEME_PRESETS: dict[str, dict[str, str]] = {
    "Gruvbox Material": {
        "preset": "Gruvbox Material",
        "bg_color": "#141617",
        "fg_color": "#D4BE98",
        "accent_color": "#E78A4E",
        "card_bg_color": "#1D2021",
        "border_color": "#3C3836",
    },
    "Cyberpunk Dark": {
        "preset": "Cyberpunk Dark",
        "bg_color": "#0d0e15",
        "fg_color": "#00ffcc",
        "accent_color": "#ff0055",
        "card_bg_color": "#16192b",
        "border_color": "#ff0055",
    },
    "Catppuccin Mocha": {
        "preset": "Catppuccin Mocha",
        "bg_color": "#1e1e2e",
        "fg_color": "#cdd6f4",
        "accent_color": "#f5c2e7",
        "card_bg_color": "#181825",
        "border_color": "#313244",
    },
    "纯黑 OLED": {
        "preset": "纯黑 OLED",
        "bg_color": "#000000",
        "fg_color": "#e0e0e0",
        "accent_color": "#4da6ff",
        "card_bg_color": "#111111",
        "border_color": "#222222",
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "main_agent_path": "",
    "sub_agents": [],  # 默认为空列表 []！
    "poll_interval_ms": 1000,
    "scan_ttl_sec": 2.5,
    "theme": {
        "preset": "Gruvbox Material",
        "bg_color": "#141617",
        "fg_color": "#D4BE98",
        "accent_color": "#E78A4E",
        "card_bg_color": "#1D2021",
        "border_color": "#3C3836",
    },
}

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class ConfigManager:
    """配置中心：负责管理 HUD 路径、轮询频率、子 Agent 列表与主题配置，支持线程安全读写与脏数据校验。"""

    def __init__(self, config_path: Path | str | None = None):
        self._lock = threading.RLock()
        self._explicit_path = Path(config_path).expanduser() if config_path else None
        self.local_config_path = Path(__file__).resolve().parent / "config.json"
        self.fallback_config_path = Path.home() / ".gemini" / "token_hud_config.json"
        self.config_path = self._resolve_config_path()
        self._data: dict[str, Any] = self._deep_copy_default()
        self.load()

    def _resolve_config_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        if self.local_config_path.is_file():
            return self.local_config_path
        if self.fallback_config_path.is_file():
            return self.fallback_config_path
        return self.local_config_path

    @staticmethod
    def _deep_copy_default() -> dict[str, Any]:
        return json.loads(json.dumps(DEFAULT_CONFIG))

    @staticmethod
    def _resolve_sub_agent_display_name(raw_name: Any, raw_path: Any) -> str:
        """解析子 Agent 展示名称：优先采用用户自定义名称，若未自定义则自动采用监控路径所在文件夹名称。"""
        name_str = str(raw_name or "").strip()
        if name_str and name_str.lower() not in ("subagent", "gemini cli"):
            return name_str
        path_str = str(raw_path or "").strip()
        if path_str:
            try:
                p = Path(path_str).expanduser()
                folder_name = p.parent.name if p.suffix.lower() in (".jsonl", ".json") else p.name
                if folder_name:
                    return folder_name
            except Exception:
                pass
        return name_str or "SubAgent"

    @classmethod
    def _sanitize_config(cls, raw: Any) -> dict[str, Any]:
        clean = cls._deep_copy_default()
        if not isinstance(raw, dict):
            return clean

        main_path = raw.get("main_agent_path")
        if isinstance(main_path, str):
            clean["main_agent_path"] = main_path.strip()

        raw_subs = raw.get("sub_agents")
        if isinstance(raw_subs, list):
            valid_subs: list[dict[str, str]] = []
            for item in raw_subs:
                if not isinstance(item, dict):
                    continue
                path_str = str(item.get("path") or "").strip()
                name = cls._resolve_sub_agent_display_name(item.get("name"), path_str)
                parser_type = str(item.get("parser_type") or "gemini_cli_session").strip()
                if parser_type not in ("antigravity_transcript", "gemini_cli_session"):
                    parser_type = "gemini_cli_session"
                live_state_path = str(item.get("live_state_path") or "").strip()
                valid_subs.append(
                    {
                        "name": name,
                        "path": path_str,
                        "parser_type": parser_type,
                        "live_state_path": live_state_path,
                    }
                )
            clean["sub_agents"] = valid_subs

        poll_ms = raw.get("poll_interval_ms")
        if isinstance(poll_ms, (int, float)) and not isinstance(poll_ms, bool):
            clean["poll_interval_ms"] = max(100, min(60000, int(poll_ms)))

        ttl_sec = raw.get("scan_ttl_sec")
        if isinstance(ttl_sec, (int, float)) and not isinstance(ttl_sec, bool):
            clean["scan_ttl_sec"] = max(0.1, min(300.0, float(ttl_sec)))

        raw_theme = raw.get("theme")
        if isinstance(raw_theme, dict):
            preset_name = str(raw_theme.get("preset") or clean["theme"]["preset"]).strip()
            base_theme = dict(THEME_PRESETS.get(preset_name, THEME_PRESETS["Gruvbox Material"]))
            base_theme["preset"] = preset_name if preset_name else "Gruvbox Material"
            for color_key in ("bg_color", "fg_color", "accent_color", "card_bg_color", "border_color"):
                val = raw_theme.get(color_key)
                if isinstance(val, str) and _HEX_COLOR_RE.match(val.strip()):
                    base_theme[color_key] = val.strip()
            clean["theme"] = base_theme

        for k, v in raw.items():
            if k not in clean:
                clean[k] = v

        return clean

    def load(self) -> dict[str, Any]:
        """从配置文件加载并校验配置，文件不存在或损坏时回退至默认配置。"""
        with self._lock:
            self.config_path = self._resolve_config_path()
            loaded_raw: Any = {}
            if self.config_path.is_file():
                try:
                    text = self.config_path.read_text(encoding="utf-8")
                    loaded_raw = json.loads(text)
                except Exception:
                    loaded_raw = {}
            self._data = self._sanitize_config(loaded_raw)
            return json.loads(json.dumps(self._data))

    def save(self, data: dict[str, Any] | None = None) -> None:
        """线程安全地校验并保存配置到文件（优先保存在同级 config.json，失败时回退 ~/.gemini/token_hud_config.json）。"""
        with self._lock:
            if data is not None:
                self._data = self._sanitize_config(data)
            else:
                self._data = self._sanitize_config(self._data)

            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            target_candidates = [self.config_path]
            if self.fallback_config_path not in target_candidates:
                target_candidates.append(self.fallback_config_path)

            for target in target_candidates:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = target.with_suffix(target.suffix + ".tmp")
                    tmp_path.write_text(payload, encoding="utf-8")
                    tmp_path.replace(target)
                    self.config_path = target
                    return
                except Exception:
                    continue

    def get(self, key: str, default: Any = None) -> Any:
        """线程安全读取配置项。"""
        with self._lock:
            if key in self._data:
                val = self._data[key]
                if isinstance(val, (dict, list)):
                    return json.loads(json.dumps(val))
                return val
            return default

    def set(self, key: str, value: Any) -> None:
        """线程安全设置配置项并立即持久化校验。"""
        with self._lock:
            candidate = dict(self._data)
            candidate[key] = value
            self._data = self._sanitize_config(candidate)
            self.save()


def _probe_antigravity_brain_dir(raw_path: str | Path) -> tuple[Path | None, bool]:
    """
    检查候选路径是否为有效的 Antigravity brain 目录（或可直接定位到 brain 目录），
    并判断其下是否包含至少一个 transcript.jsonl 会话文件。
    """
    try:
        p = Path(raw_path).expanduser()
        if not p.exists():
            return None, False
        if p.is_file():
            if p.name == "transcript.jsonl":
                # ~/.gemini/antigravity/brain/<cid>/.system_generated/logs/transcript.jsonl
                brain_cand = p.parent.parent.parent.parent
                if brain_cand.is_dir():
                    return brain_cand, True
                return p.parent, True
            return None, False

        # 如果传入的是 ~/.gemini 或 ~/.gemini/antigravity，尝试向下解析到 brain
        if (p / "antigravity" / "brain").is_dir():
            p = p / "antigravity" / "brain"
        elif (p / "brain").is_dir() and p.name == "antigravity":
            p = p / "brain"

        if not p.is_dir():
            return None, False

        # 检查是否直接含 <cid>/.system_generated/logs/transcript.jsonl 或自身就是单个 <cid> 目录
        has_transcript = any(p.glob("*/.system_generated/logs/transcript.jsonl")) or (
            (p / ".system_generated" / "logs" / "transcript.jsonl").is_file()
        )
        return p, bool(has_transcript)
    except Exception:
        return None, False


def auto_detect_antigravity_path(
    custom_path: str = "",
    config_mgr: ConfigManager | None = None,
) -> tuple[Path | None, bool]:
    """
    跨平台/多环境自动探测主 Agent (Antigravity) 数据路径。
    优先级：
    1. custom_path（若传入且有效）；
    2. config 中的 main_agent_path；
    3. 环境变量 GEMINI_HOME 下的 antigravity/brain；
    4. Path.home() / ".gemini" / "antigravity" / "brain"；
    返回 (detected_path, is_valid_and_has_data)。
    """
    candidates: list[Path] = []

    if isinstance(custom_path, str) and custom_path.strip():
        candidates.append(Path(custom_path.strip()).expanduser())

    cfg_path_str = ""
    if config_mgr is not None:
        cfg_path_str = str(config_mgr.get("main_agent_path", "") or "").strip()
    else:
        try:
            cfg_path_str = str(ConfigManager().get("main_agent_path", "") or "").strip()
        except Exception:
            cfg_path_str = ""
    if cfg_path_str:
        candidates.append(Path(cfg_path_str).expanduser())

    gemini_home_env = os.environ.get("GEMINI_HOME", "").strip()
    if gemini_home_env:
        candidates.append(Path(gemini_home_env).expanduser() / "antigravity" / "brain")

    candidates.append(Path.home() / ".gemini" / "antigravity" / "brain")

    first_existing_dir: Path | None = None
    seen: set[str] = set()
    for cand in candidates:
        norm_key = str(cand)
        if norm_key in seen:
            continue
        seen.add(norm_key)
        resolved_dir, has_data = _probe_antigravity_brain_dir(cand)
        if resolved_dir is not None:
            if has_data:
                return resolved_dir, True
            if first_existing_dir is None:
                first_existing_dir = resolved_dir

    if first_existing_dir is not None:
        return first_existing_dir, False
    return None, False

MUTEX_NAME = "Global\\" + "AntigravityTokenHUDMutex"
_MUTEX_HANDLE = None


def acquire_single_instance_mutex() -> bool:
    """
    使用 Win32 命名互斥体确保全局只有一个 HUD 实例运行。
    若已存在实例（get_last_error() == 183 ERROR_ALREADY_EXISTS），返回 False。
    """
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE

        ctypes.set_last_error(0)
        handle = k32.CreateMutexW(None, False, MUTEX_NAME)
        last_err = ctypes.get_last_error()
        if not handle:
            ctypes.set_last_error(0)
            handle = k32.CreateMutexW(None, False, "Local\\" + "AntigravityTokenHUDMutex")
            last_err = ctypes.get_last_error()
        _MUTEX_HANDLE = handle
        if last_err == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return True


def bring_existing_hud_to_front() -> bool:
    """查找已运行的 Token HUD 窗口并将其恢复显示、置于最顶层及激活焦点。"""
    if sys.platform != "win32":
        return False
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u32.FindWindowW.restype = wintypes.HWND
        u32.IsIconic.argtypes = [wintypes.HWND]
        u32.IsIconic.restype = wintypes.BOOL
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.ShowWindow.restype = wintypes.BOOL
        u32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        u32.SetWindowPos.restype = wintypes.BOOL
        u32.BringWindowToTop.argtypes = [wintypes.HWND]
        u32.BringWindowToTop.restype = wintypes.BOOL
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.SetForegroundWindow.restype = wintypes.BOOL

        hwnd = u32.FindWindowW(None, "Antigravity & Argon Token HUD")
        if not hwnd:
            return False

        SW_RESTORE = 9
        SW_SHOW = 5
        HWND_TOPMOST = wintypes.HWND(-1)
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040

        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, SW_RESTORE)
        else:
            u32.ShowWindow(hwnd, SW_SHOW)

        u32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def bind_interactive_desktop() -> None:
    """在 Windows 下将当前进程/线程绑定至物理交互桌面 Default，确保后台拉起时窗口可见。"""
    if sys.platform != "win32":
        return
    try:
        u32 = ctypes.WinDLL("user32")
        u32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        u32.OpenDesktopW.restype = wintypes.HANDLE
        u32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        u32.SetThreadDesktop.restype = wintypes.BOOL
        h_desk = u32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            u32.SetThreadDesktop(h_desk)
    except Exception:
        pass


def enable_dpi_awareness() -> None:
    """启用 Windows 高分屏 DPI 感知，防止悬浮窗字体模糊。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def resolve_top_level_hwnd(widget_or_hwnd: Any) -> int:
    """获取窗口对应的真实 Win32 顶级窗口句柄 (HWND)。"""
    if sys.platform != "win32":
        return 0
    try:
        if isinstance(widget_or_hwnd, int):
            wid = widget_or_hwnd
        elif hasattr(widget_or_hwnd, "winId"):
            wid = int(widget_or_hwnd.winId())
        elif hasattr(widget_or_hwnd, "winfo_id"):
            wid = int(widget_or_hwnd.winfo_id())
        else:
            return 0

        u32 = ctypes.windll.user32
        u32.GetParent.argtypes = [wintypes.HWND]
        u32.GetParent.restype = wintypes.HWND
        u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u32.GetAncestor.restype = wintypes.HWND

        parent = u32.GetParent(wid)
        if parent:
            return int(parent)
        root_hwnd = u32.GetAncestor(wid, 2)  # GA_ROOT = 2
        if root_hwnd:
            return int(root_hwnd)
        return wid
    except Exception:
        return 0


def apply_win11_mica_and_rounded_corners(window: Any) -> dict[str, Any]:
    """
    对顶层窗口应用 Windows 11 原生暗色模式、圆角与 Mica 材质属性：
    - DWMWA_USE_IMMERSIVE_DARK_MODE (20) = 1
    - DWMWA_WINDOW_CORNER_PREFERENCE (33) = 2 (DWMWCP_ROUND)
    - DWMWA_SYSTEMBACKDROP_TYPE (38) = 2 (DWMSBT_MAINWINDOW / Mica) 或 4 (DWMSBT_TABBEDWINDOW / Mica Alt)
    """
    result: dict[str, Any] = {"hwnd": 0, "dark_mode": False, "rounded": False, "mica": False}
    if sys.platform != "win32":
        return result
    try:
        wid = int(window.winId()) if hasattr(window, "winId") else int(window)
        top_hwnd = resolve_top_level_hwnd(window)
        dwmapi = ctypes.windll.dwmapi

        hwnd_candidates = []
        if top_hwnd:
            hwnd_candidates.append(top_hwnd)
        if wid and wid not in hwnd_candidates:
            hwnd_candidates.append(wid)

        for hwnd in hwnd_candidates:
            result["hwnd"] = hwnd
            # 1. 沉浸式暗色模式 (DWMWA_USE_IMMERSIVE_DARK_MODE = 20)
            hr_dark = dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4
            )
            if hr_dark == 0:
                result["dark_mode"] = True

            # 2. 原生圆角 (DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2)
            hr_round = dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4
            )
            if hr_round == 0:
                result["rounded"] = True

            # 3. Mica 材质 (DWMWA_SYSTEMBACKDROP_TYPE = 38, 2 = Mica, 4 = Mica Alt)
            hr_mica = dwmapi.DwmSetWindowAttribute(
                hwnd, 38, ctypes.byref(ctypes.c_int(2)), 4
            )
            if hr_mica != 0:
                hr_mica = dwmapi.DwmSetWindowAttribute(
                    hwnd, 38, ctypes.byref(ctypes.c_int(4)), 4
                )
            if hr_mica == 0:
                result["mica"] = True
    except Exception:
        pass
    return result


def parse_iso_timestamp(ts_str: str) -> float:
    """将 ISO 8601 时间戳解析为本地 epoch 秒数。"""
    if not ts_str:
        return 0.0
    try:
        norm = ts_str.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(norm)
        return dt.timestamp()
    except Exception:
        return 0.0


def _truncate_session_name(raw_text: str, max_len: int = 30) -> str:
    """清理多行空白并截断会话名称至指定字符数。"""
    cleaned = " ".join(raw_text.strip().split())
    if not cleaned:
        return ""
    if len(cleaned) > max_len:
        return cleaned[:max_len] + ".."
    return cleaned


def parse_antigravity_transcript(file_path: Path) -> dict[str, Any]:
    """
    解析主 Agent (Antigravity) 的 transcript.jsonl 日志文件：
    路径形如：~/.gemini/antigravity/brain/<cid>/.system_generated/logs/transcript.jsonl
    """
    cid = "unknown"
    try:
        cid = file_path.parent.parent.parent.name
    except Exception:
        pass

    short_cid = cid[:8] if len(cid) > 8 else cid
    info: dict[str, Any] = {
        "file_path": str(file_path),
        "cid": cid,
        "session_name": short_cid,
        "model": "Antigravity (Main)",
        "start_time": 0.0,
        "last_updated": 0.0,
        "steps": 0,
        "tool_calls_count": 0,
        "last_tool": "",
        "status": "idle",
        "context_tokens": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "thought_tokens": 0,
        "total_tokens": 0,
    }

    try:
        mtime = file_path.stat().st_mtime
        info["last_updated"] = mtime
    except OSError:
        return info

    last_record_status = "DONE"
    last_record_type = ""
    has_pending_tool = False
    extracted_session_name = ""

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                created_ts = parse_iso_timestamp(str(obj.get("created_at") or ""))
                if created_ts > 0:
                    if info["start_time"] <= 0:
                        info["start_time"] = created_ts
                    if created_ts > info["last_updated"]:
                        info["last_updated"] = created_ts

                etype = str(obj.get("type") or "")
                last_record_type = etype
                last_record_status = str(obj.get("status") or "DONE")

                if etype == "USER_INPUT":
                    has_pending_tool = False
                    content_str = str(obj.get("content") or "")
                    if not extracted_session_name and content_str:
                        m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content_str, re.DOTALL)
                        req_body = m.group(1) if m else content_str
                        for req_line in req_body.splitlines():
                            s_line = req_line.strip()
                            if s_line and not s_line.startswith("<"):
                                extracted_session_name = _truncate_session_name(s_line, 30)
                                break
                    if "Model Selection" in content_str:
                        for line_part in content_str.splitlines():
                            if "Model Selection" in line_part and " to " in line_part:
                                after_to = line_part.split(" to ", 1)[1]
                                model_cand = after_to.split(". ")[0].split("<")[0].rstrip(".").strip()
                                if model_cand:
                                    info["model"] = model_cand

                elif etype == "PLANNER_RESPONSE":
                    in_raw = int(obj.get("input_tokens", 0) or 0)
                    cache_read = int(obj.get("cache_read_tokens", 0) or 0)
                    out_tok = int(obj.get("output_tokens", 0) or 0)

                    thought_tok = int(obj.get("thinking_tokens", 0) or obj.get("thought_tokens", 0) or 0)
                    if thought_tok == 0 and obj.get("thinking"):
                        thinking_text = str(obj.get("thinking") or "")
                        if thinking_text:
                            thought_tok = max(1, len(thinking_text) // 4)

                    ctx_window = in_raw + cache_read
                    step_total_in = in_raw + cache_read
                    step_total = step_total_in + out_tok

                    info["steps"] += 1
                    info["context_tokens"] = ctx_window if ctx_window > 0 else in_raw
                    info["input_tokens"] += step_total_in
                    info["cached_tokens"] += cache_read
                    info["output_tokens"] += out_tok
                    info["thought_tokens"] += thought_tok
                    info["total_tokens"] += step_total

                    t_calls = obj.get("tool_calls")
                    if isinstance(t_calls, list) and t_calls:
                        info["tool_calls_count"] += len(t_calls)
                        has_pending_tool = True
                        last_tc = t_calls[-1]
                        if isinstance(last_tc, dict) and last_tc.get("name"):
                            info["last_tool"] = str(last_tc["name"])
                    else:
                        has_pending_tool = False
    except Exception:
        return info

    if extracted_session_name:
        info["session_name"] = extracted_session_name

    now = time.time()
    age = now - info["last_updated"]
    if last_record_status == "RUNNING" and age < 120.0:
        info["status"] = "running"
    elif age < 30.0 and (last_record_type == "USER_INPUT" or has_pending_tool):
        info["status"] = "running"
    elif age < 10.0:
        info["status"] = "active"
    else:
        info["status"] = "completed"

    return info


def _extract_user_prompt_first_line(content: Any) -> str:
    """从 Gemini CLI user 消息的 content 中提取首行有效文本（剥离 <session_context> 等头部标签）。"""
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
            elif isinstance(item, str):
                texts.append(item)
    raw = "\n".join(texts)
    if not raw:
        return ""
    raw = re.sub(r"<session_context>.*?</session_context>", "", raw, flags=re.DOTALL)
    for line in raw.splitlines():
        s = line.strip()
        if s and not s.startswith("<"):
            return _truncate_session_name(s, 30)
    return ""


def parse_session_jsonl(file_path: Path) -> dict[str, Any]:
    """
    解析单个子 Agent (Gemini CLI) session-*.jsonl 文件，按消息 id 去重提取每个 API Turn 的精确 Token 统计与最新状态。
    """
    project_name = file_path.parent.parent.name if file_path.parent.name == "chats" else "unknown"
    info: dict[str, Any] = {
        "file_path": str(file_path),
        "project": project_name,
        "session_id": "",
        "session_name": project_name,
        "model": "",
        "start_time": 0.0,
        "last_updated": 0.0,
        "steps": 0,
        "tool_calls_count": 0,
        "last_tool": "",
        "status": "idle",
        "context_tokens": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "thought_tokens": 0,
        "tool_tokens": 0,
        "total_tokens": 0,
    }

    try:
        mtime = file_path.stat().st_mtime
        info["last_updated"] = mtime
    except OSError:
        return info

    turns_by_id: dict[str, dict[str, Any]] = {}
    last_event_type = ""
    has_active_tool = False
    topic_title = ""
    first_user_line = ""

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if "sessionId" in obj and not info["session_id"]:
                    info["session_id"] = str(obj.get("sessionId", ""))
                    st = parse_iso_timestamp(str(obj.get("startTime", "")))
                    if st > 0:
                        info["start_time"] = st

                if "$set" in obj and isinstance(obj["$set"], dict):
                    lu = parse_iso_timestamp(str(obj["$set"].get("lastUpdated", "")))
                    if lu > info["last_updated"]:
                        info["last_updated"] = lu
                    continue

                etype = obj.get("type") or obj.get("role")
                if etype == "user":
                    last_event_type = "user"
                    has_active_tool = False
                    if not first_user_line:
                        first_user_line = _extract_user_prompt_first_line(obj.get("content"))
                elif etype == "gemini":
                    last_event_type = "gemini"
                    msg_id = str(obj.get("id") or f"anon_{len(turns_by_id)}")
                    model_name = str(obj.get("model") or "")
                    if model_name:
                        info["model"] = model_name

                    tok_obj = obj.get("tokens")
                    t_calls = obj.get("toolCalls")
                    if isinstance(t_calls, list) and t_calls:
                        has_active_tool = True
                        last_tc = t_calls[-1]
                        if isinstance(last_tc, dict) and last_tc.get("name"):
                            info["last_tool"] = str(last_tc["name"])
                        for tc in t_calls:
                            if isinstance(tc, dict) and tc.get("name") == "update_topic":
                                args_obj = tc.get("args")
                                if isinstance(args_obj, dict) and args_obj.get("title"):
                                    topic_title = _truncate_session_name(str(args_obj["title"]), 30)
                    else:
                        has_active_tool = False

                    prev = turns_by_id.get(msg_id, {})
                    turns_by_id[msg_id] = {
                        "tokens": tok_obj if isinstance(tok_obj, dict) else prev.get("tokens", {}),
                        "tool_calls": len(t_calls) if isinstance(t_calls, list) else prev.get("tool_calls", 0),
                    }
    except Exception:
        return info

    short_sid = info["session_id"][:8] if info["session_id"] else ""
    if topic_title:
        info["session_name"] = topic_title
    elif first_user_line:
        info["session_name"] = first_user_line
    elif project_name and project_name != "unknown":
        info["session_name"] = project_name
    elif short_sid:
        info["session_name"] = short_sid

    for turn_data in turns_by_id.values():
        tok = turn_data.get("tokens") or {}
        in_t = int(tok.get("input", 0) or 0)
        out_t = int(tok.get("output", 0) or 0)
        cached_t = int(tok.get("cached", 0) or 0)
        thought_t = int(tok.get("thoughts", 0) or 0)
        tool_t = int(tok.get("tool", 0) or 0)
        tot_t = int(tok.get("total", 0) or (in_t + out_t + thought_t + tool_t))

        if tot_t > 0 or in_t > 0 or out_t > 0:
            info["steps"] += 1
            info["context_tokens"] = in_t
            info["input_tokens"] += in_t
            info["cached_tokens"] += cached_t
            info["output_tokens"] += out_t
            info["thought_tokens"] += thought_t
            info["tool_tokens"] += tool_t
            info["total_tokens"] += tot_t

        info["tool_calls_count"] += int(turn_data.get("tool_calls", 0))

    now = time.time()
    age = now - info["last_updated"]
    if age < 30.0 and (last_event_type == "user" or has_active_tool):
        info["status"] = "running"
    elif age < 8.0:
        info["status"] = "active"
    else:
        info["status"] = "completed"

    return info


class TokenMonitorCache:
    """增量缓存主 Agent (Antigravity) 与子 Agent (Gemini CLI / Antigravity) 日志解析结果（支持多线程安全读取）。"""

    def __init__(
        self,
        config_mgr: ConfigManager | None = None,
        tmp_root: Path = GEMINI_TMP_ROOT,
        brain_root: Path = ANTIGRAVITY_BRAIN_ROOT,
        scan_ttl_sec: float | None = None,
    ):
        self.config_mgr = config_mgr if config_mgr is not None else ConfigManager()
        self.tmp_root = tmp_root
        self.brain_root = brain_root
        if scan_ttl_sec is not None:
            self.scan_ttl_sec = float(scan_ttl_sec)
        else:
            self.scan_ttl_sec = float(self.config_mgr.get("scan_ttl_sec", 2.5) or 2.5)
        self._lock = threading.Lock()
        self._sub_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
        self._main_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
        self._main_paths_cache: list[Path] = []
        self._sub_entries_cache: list[tuple[dict[str, str], Path]] = []
        self._main_agent_valid: bool = False
        self._last_dir_scan_ts: float = 0.0
        self._last_scan_config_sig: str = ""

    def read_live_state(self, live_state_path: str | Path | None = None) -> dict[str, Any]:
        """读取实时流式状态文件（若存在）。"""
        candidates: list[Path] = []
        if live_state_path:
            p_str = str(live_state_path).strip()
            if p_str:
                candidates.append(Path(p_str).expanduser())
        else:
            sub_agent_configs = self.config_mgr.get("sub_agents", [])
            if isinstance(sub_agent_configs, list):
                for item in sub_agent_configs:
                    if isinstance(item, dict):
                        lp = str(item.get("live_state_path") or "").strip()
                        if lp:
                            candidates.append(Path(lp).expanduser())
            if not candidates and sub_agent_configs:
                candidates.append(LIVE_STATE_FILE)

        for cand in candidates:
            if not cand.is_file():
                continue
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return {}

    @staticmethod
    def _discover_sub_agent_files(base_path: Path, parser_type: str) -> list[Path]:
        """根据配置的路径和 parser_type 查找对应的会话日志文件列表。"""
        try:
            p = base_path.expanduser()
            if not p.exists():
                return []
            if p.is_file():
                return [p]
            if not p.is_dir():
                return []

            if parser_type == "antigravity_transcript":
                resolved, _ = _probe_antigravity_brain_dir(p)
                target_dir = resolved if resolved is not None else p
                if (target_dir / ".system_generated" / "logs" / "transcript.jsonl").is_file():
                    return [target_dir / ".system_generated" / "logs" / "transcript.jsonl"]
                found = list(target_dir.glob("*/.system_generated/logs/transcript.jsonl"))
                if found:
                    return found
                return list(target_dir.glob("**/transcript.jsonl"))
            else:
                # gemini_cli_session
                if (p / "tmp").is_dir():
                    p = p / "tmp"
                direct_sessions = list(p.glob("session-*.jsonl"))
                if direct_sessions:
                    return direct_sessions
                if (p / "chats").is_dir():
                    chat_sessions = list((p / "chats").glob("session-*.jsonl"))
                    if chat_sessions:
                        return chat_sessions
                found = list(p.glob("*/chats/session-*.jsonl"))
                if found:
                    return found
                return list(p.glob("**/session-*.jsonl"))
        except Exception:
            return []

    def _get_session_files(
        self, now: float, force_scan: bool = False
    ) -> tuple[list[Path], list[tuple[dict[str, str], Path]], bool]:
        """缓存目录枚举结果，避免每次刷新都全量 glob 遍历数百个子目录。"""
        self.scan_ttl_sec = float(self.config_mgr.get("scan_ttl_sec", self.scan_ttl_sec) or 2.5)
        main_cfg_path = str(self.config_mgr.get("main_agent_path", "") or "").strip()
        sub_agent_configs = self.config_mgr.get("sub_agents", [])
        if not isinstance(sub_agent_configs, list):
            sub_agent_configs = []

        config_sig = json.dumps(
            {"main": main_cfg_path, "subs": sub_agent_configs},
            sort_keys=True,
            ensure_ascii=False,
        )

        if (
            force_scan
            or config_sig != self._last_scan_config_sig
            or (now - self._last_dir_scan_ts) >= self.scan_ttl_sec
        ):
            self._last_dir_scan_ts = now
            self._last_scan_config_sig = config_sig

            detected_main, is_valid = auto_detect_antigravity_path(
                custom_path=main_cfg_path,
                config_mgr=self.config_mgr,
            )
            self.brain_root = detected_main if detected_main is not None else Path("")
            if detected_main is not None and detected_main.is_dir():
                if (detected_main / ".system_generated" / "logs" / "transcript.jsonl").is_file():
                    self._main_paths_cache = [
                        detected_main / ".system_generated" / "logs" / "transcript.jsonl"
                    ]
                else:
                    self._main_paths_cache = list(
                        detected_main.glob("*/.system_generated/logs/transcript.jsonl")
                    )
                self._main_agent_valid = bool(is_valid and len(self._main_paths_cache) > 0)
            else:
                self._main_paths_cache = []
                self._main_agent_valid = False

            sub_entries: list[tuple[dict[str, str], Path]] = []
            seen_sub_files: set[str] = set()
            if sub_agent_configs:
                for raw_cfg in sub_agent_configs:
                    if not isinstance(raw_cfg, dict):
                        continue
                    path_str = str(raw_cfg.get("path") or "").strip()
                    if not path_str:
                        continue
                    agent_name = ConfigManager._resolve_sub_agent_display_name(
                        raw_cfg.get("name"), path_str
                    )
                    parser_type = str(raw_cfg.get("parser_type") or "gemini_cli_session").strip()
                    if parser_type not in ("antigravity_transcript", "gemini_cli_session"):
                        parser_type = "gemini_cli_session"
                    live_state_path = str(raw_cfg.get("live_state_path") or "").strip()
                    norm_cfg = {
                        "name": agent_name,
                        "path": path_str,
                        "parser_type": parser_type,
                        "live_state_path": live_state_path,
                    }
                    for fpath in self._discover_sub_agent_files(Path(path_str), parser_type):
                        fkey = str(fpath)
                        if fkey not in seen_sub_files:
                            seen_sub_files.add(fkey)
                            sub_entries.append((norm_cfg, fpath))
            self._sub_entries_cache = sub_entries

        return self._main_paths_cache, self._sub_entries_cache, self._main_agent_valid

    def collect_snapshot(
        self,
        target_session: str | None = None,
        target_project: str | None = None,
        force_scan: bool = False,
    ) -> dict[str, Any]:
        """采集主 Agent、子 Agent 当前活跃会话数据以及今日双引擎全局统计快照。"""
        with self._lock:
            now = time.time()
            today_date = datetime.date.today()
            main_paths, sub_entries, main_agent_valid = self._get_session_files(
                now, force_scan=force_scan
            )

            # 1. 扫描主 Agent (Antigravity) transcript.jsonl
            main_sessions: list[dict[str, Any]] = []
            for tr_path in main_paths:
                key = str(tr_path)
                try:
                    st = tr_path.stat()
                    mtime, size = st.st_mtime, st.st_size
                except OSError:
                    continue

                cached_entry = self._main_cache.get(key)
                if cached_entry and cached_entry[0] == mtime and cached_entry[1] == size:
                    parsed = dict(cached_entry[2])
                    age = now - parsed["last_updated"]
                    if age >= 30.0:
                        parsed["status"] = "completed"
                else:
                    parsed = parse_antigravity_transcript(tr_path)
                    self._main_cache[key] = (mtime, size, dict(parsed))

                main_sessions.append(parsed)

            main_active: dict[str, Any] | None = None
            if main_sessions:
                main_sessions.sort(key=lambda x: x.get("last_updated", 0.0), reverse=True)
                main_active = dict(main_sessions[0])

            # 2. 扫描子 Agent（按 sub_agents 配置列表解析，默认为空列表时不扫描）
            sub_agent_configs = self.config_mgr.get("sub_agents", [])
            if not isinstance(sub_agent_configs, list):
                sub_agent_configs = []

            sub_sessions: list[dict[str, Any]] = []
            if sub_agent_configs:
                for agent_cfg, jsonl_path in sub_entries:
                    parser_type = agent_cfg.get("parser_type", "gemini_cli_session")
                    agent_name = agent_cfg.get("name", "SubAgent")
                    live_state_path = agent_cfg.get("live_state_path", "")
                    key = f"{parser_type}:{jsonl_path}"
                    try:
                        st = jsonl_path.stat()
                        mtime, size = st.st_mtime, st.st_size
                    except OSError:
                        continue

                    cached_entry = self._sub_cache.get(key)
                    if cached_entry and cached_entry[0] == mtime and cached_entry[1] == size:
                        parsed = dict(cached_entry[2])
                        age = now - parsed["last_updated"]
                        if age >= 30.0:
                            parsed["status"] = "completed"
                    else:
                        if parser_type == "antigravity_transcript":
                            parsed = parse_antigravity_transcript(jsonl_path)
                            if not parsed.get("session_id"):
                                parsed["session_id"] = str(parsed.get("cid") or "")
                            if not parsed.get("project"):
                                parsed["project"] = agent_name
                        else:
                            parsed = parse_session_jsonl(jsonl_path)
                        self._sub_cache[key] = (mtime, size, dict(parsed))

                    parsed["agent_name"] = agent_name
                    parsed["sub_agent_name"] = agent_name
                    parsed["parser_type"] = parser_type
                    parsed["live_state_path"] = live_state_path
                    sub_sessions.append(parsed)

            if sub_sessions:
                sub_sessions.sort(key=lambda x: x.get("last_updated", 0.0), reverse=True)

            live_state = self.read_live_state() if sub_agent_configs else {}
            live_session_id = str(live_state.get("session_id") or "")
            live_updated = float(live_state.get("updated_at") or 0.0)
            live_is_fresh = (
                bool(sub_agent_configs)
                and (now - live_updated) < 15.0
                and live_state.get("status") == "running"
            )

            active_session: dict[str, Any] | None = None
            if target_session and sub_sessions:
                t_lower = target_session.lower()
                for s in sub_sessions:
                    sid = str(s.get("session_id", "")).lower()
                    fname = Path(s["file_path"]).name.lower()
                    if sid.startswith(t_lower) or t_lower in fname:
                        active_session = dict(s)
                        break

            if active_session is None and live_is_fresh and live_session_id:
                for s in sub_sessions:
                    if str(s.get("session_id", "")).startswith(live_session_id[:8]):
                        active_session = dict(s)
                        break

            if active_session is None and sub_sessions:
                candidates = sub_sessions
                if target_project:
                    proj_filtered = [s for s in sub_sessions if s.get("project") == target_project]
                    if proj_filtered:
                        candidates = proj_filtered
                candidates.sort(key=lambda x: x.get("last_updated", 0.0), reverse=True)
                active_session = dict(candidates[0])

            if active_session is not None:
                sess_live_path = str(active_session.get("live_state_path") or "").strip()
                sess_live = self.read_live_state(sess_live_path) if sess_live_path else live_state
                sess_live_sid = str(sess_live.get("session_id") or "")
                sess_live_upd = float(sess_live.get("updated_at") or 0.0)
                sess_live_fresh = (now - sess_live_upd) < 15.0 and sess_live.get("status") == "running"
                if sess_live_fresh:
                    sid = str(active_session.get("session_id", ""))
                    if not sess_live_sid or sid.startswith(sess_live_sid[:8]):
                        active_session["status"] = "running"
                        if sess_live.get("model"):
                            active_session["model"] = str(sess_live["model"])
                        if sess_live.get("current_tool"):
                            active_session["last_tool"] = str(sess_live["current_tool"])
                        if sess_live.get("start_time"):
                            active_session["start_time"] = float(sess_live["start_time"])
                        est_stream_tok = int(sess_live.get("stream_est_output_tokens") or 0)
                        active_session["stream_est_tokens"] = est_stream_tok
            elif active_session is None and live_is_fresh:
                proj_fallback = str(live_state.get("project") or "active")
                first_sub_name = str(sub_agent_configs[0].get("name") or "SubAgent") if sub_agent_configs else "SubAgent"
                first_parser = str(sub_agent_configs[0].get("parser_type") or "gemini_cli_session") if sub_agent_configs else "gemini_cli_session"
                active_session = {
                    "file_path": "",
                    "project": proj_fallback,
                    "session_id": live_session_id or "初始化中...",
                    "session_name": str(live_state.get("session_name") or proj_fallback),
                    "model": str(live_state.get("model") or "gemini-4-argon"),
                    "start_time": float(live_state.get("start_time") or now),
                    "last_updated": live_updated,
                    "steps": 0,
                    "tool_calls_count": int(live_state.get("tool_calls_count") or 0),
                    "last_tool": str(live_state.get("current_tool") or "等待模型响应..."),
                    "status": "running",
                    "context_tokens": 0,
                    "input_tokens": 0,
                    "cached_tokens": 0,
                    "output_tokens": 0,
                    "thought_tokens": 0,
                    "tool_tokens": 0,
                    "total_tokens": 0,
                    "stream_est_tokens": int(live_state.get("stream_est_output_tokens") or 0),
                    "agent_name": first_sub_name,
                    "sub_agent_name": first_sub_name,
                    "parser_type": first_parser,
                }

            # 3. 汇总今日双引擎全局统计
            main_today = {
                "sessions_count": 0,
                "steps_count": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "thought_tokens": 0,
                "total_tokens": 0,
            }
            for ms in main_sessions:
                lu = float(ms.get("last_updated") or 0.0)
                if lu <= 0:
                    continue
                if datetime.datetime.fromtimestamp(lu).date() == today_date and int(ms.get("steps", 0)) > 0:
                    main_today["sessions_count"] += 1
                    main_today["steps_count"] += int(ms.get("steps", 0))
                    main_today["input_tokens"] += int(ms.get("input_tokens", 0))
                    main_today["cached_tokens"] += int(ms.get("cached_tokens", 0))
                    main_today["output_tokens"] += int(ms.get("output_tokens", 0))
                    main_today["thought_tokens"] += int(ms.get("thought_tokens", 0))
                    main_today["total_tokens"] += int(ms.get("total_tokens", 0))

            sub_today = {
                "sessions_count": 0,
                "steps_count": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "thought_tokens": 0,
                "total_tokens": 0,
            }
            for ss in sub_sessions:
                lu = float(ss.get("last_updated") or 0.0)
                if lu <= 0:
                    continue
                if datetime.datetime.fromtimestamp(lu).date() == today_date and int(ss.get("steps", 0)) > 0:
                    sub_today["sessions_count"] += 1
                    sub_today["steps_count"] += int(ss.get("steps", 0))
                    sub_today["input_tokens"] += int(ss.get("input_tokens", 0))
                    sub_today["cached_tokens"] += int(ss.get("cached_tokens", 0))
                    sub_today["output_tokens"] += int(ss.get("output_tokens", 0))
                    sub_today["thought_tokens"] += int(ss.get("thought_tokens", 0))
                    sub_today["total_tokens"] += int(ss.get("total_tokens", 0))

            today_stats = {
                "main_sessions_count": main_today["sessions_count"],
                "sub_sessions_count": sub_today["sessions_count"],
                "sessions_count": main_today["sessions_count"] + sub_today["sessions_count"],
                "main_steps_count": main_today["steps_count"],
                "sub_steps_count": sub_today["steps_count"],
                "steps_count": main_today["steps_count"] + sub_today["steps_count"],
                "main_total_tokens": main_today["total_tokens"],
                "sub_total_tokens": sub_today["total_tokens"],
                "input_tokens": main_today["input_tokens"] + sub_today["input_tokens"],
                "cached_tokens": main_today["cached_tokens"] + sub_today["cached_tokens"],
                "output_tokens": main_today["output_tokens"] + sub_today["output_tokens"],
                "thought_tokens": main_today["thought_tokens"] + sub_today["thought_tokens"],
                "total_tokens": main_today["total_tokens"] + sub_today["total_tokens"],
            }

            return {
                "timestamp": now,
                "main_agent_valid": main_agent_valid,
                "main_agent_path": str(self.brain_root) if self.brain_root and str(self.brain_root) != "." else "",
                "main_session": main_active,
                "active_session": active_session,
                "all_main_sessions": [dict(s) for s in main_sessions],
                "all_sub_sessions": [dict(s) for s in sub_sessions],
                "today_stats": today_stats,
                "live_state": live_state if live_is_fresh else {},
            }


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


# ==============================================================================
# PySide6 (Qt 6) 高性能单句柄硬件加速悬浮窗实现
# ==============================================================================
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


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
    左右双栏（子菜单与右侧 Token 统计页）之间的横向比例调整手柄：
    - 平时显示低对比度垂直握把条，悬停或拖拽时高亮为 Gruvbox 橙色；
    - 支持左右拖拽实时调整子菜单与右侧统计页的宽度占比。
    """

    def __init__(self, hud_window: "TokenHUDWindow", parent: QWidget | None = None):
        super().__init__(parent)
        self._hud = hud_window
        self._hovered = False
        self._dragging = False
        self.setFixedWidth(10)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip("左右拖拽调整子菜单与统计页宽度占比")

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
            self._hud._on_splitter_press(g_pos.x())
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            g_pos = event.globalPosition().toPoint()
            self._hud._on_splitter_motion(g_pos.x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            g_pos = event.globalPosition().toPoint()
            self._hud._on_splitter_release(g_pos.x())
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        cx = w // 2
        pill_h = max(28, min(72, int(h * 0.22)))
        y0 = (h - pill_h) // 2
        accent_hex = getattr(self._hud, "FG_ACCENT", "#E78A4E")
        border_hex = getattr(self._hud, "BORDER_COLOR", "#504945")
        color = QColor(accent_hex) if (self._hovered or self._dragging) else QColor(border_hex)
        pen = QPen(color, 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(cx, y0, cx, y0 + pill_h)


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

    def apply_theme(self, palette: dict[str, str]) -> None:
        """应用动态配色方案并立即重绘。"""
        self._bg_inner = QColor(palette.get("BG_INNER", "#181A1B"))
        self._bg_hover = QColor(palette.get("BG_HOVER", "#262A2C"))
        self._bg_selected = QColor(palette.get("BG_SELECTED", "#32302F"))
        self._border_normal = QColor(palette.get("BORDER_COLOR", "#1D2021"))
        self._border_selected = QColor(palette.get("FG_ACCENT", "#E78A4E"))
        self._fg_primary = palette.get("FG_PRIMARY", "#D4BE98")
        self._fg_muted = palette.get("FG_MUTED", "#928374")
        self._fg_accent = palette.get("FG_ACCENT", "#E78A4E")

        badge_color = self._fg_accent if self.eng == "main" else "#A9B665"
        self.badge_lbl.setStyleSheet(f"color: {badge_color}; background: transparent; border: none;")
        self.name_lbl.setStyleSheet(f"color: {self._fg_primary}; background: transparent; border: none;")
        self.meta_left.setStyleSheet(f"color: {self._fg_muted}; background: transparent; border: none;")
        self.time_lbl.setStyleSheet(f"color: {self._fg_muted}; background: transparent; border: none;")
        tok_color = "#D8A657" if self.is_selected else "#A9B665"
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
        self._raw_tok = format_compact_tokens(int(sess.get("total_tokens") or 0))

        raw_id = str(sess.get("cid") if self.eng == "main" else sess.get("session_id") or "")
        short_id = raw_id[:8] if raw_id else "-"
        steps_num = int(sess.get("steps") or 0)
        self._raw_meta = f"#{short_id} · {steps_num}轮"
        self._raw_time = format_short_time(float(sess.get("last_updated") or 0.0))

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
            bg = self.COLOR_BG_HOVER
        elif self.is_selected:
            bg = self.COLOR_BG_SELECTED
        else:
            bg = self.COLOR_BG_INNER

        border = self.COLOR_BORDER_SELECTED if self.is_selected else self.COLOR_BORDER_NORMAL
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

    def __init__(self, parent: QWidget | None = None, config_mgr: ConfigManager | None = None):
        super().__init__(parent)
        self._hud_parent = parent
        if config_mgr is not None:
            self.config_mgr = config_mgr
        elif parent is not None and hasattr(parent, "config_mgr") and parent.config_mgr is not None:
            self.config_mgr = parent.config_mgr
        else:
            self.config_mgr = ConfigManager()

        self.setWindowTitle("Token HUD 设置")
        self.setMinimumSize(620, 450)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowFlags(
            (self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
            | Qt.WindowType.WindowStaysOnTopHint
        )

        self._updating_preset_fields = False
        self._build_dialog_ui()
        self._apply_dialog_stylesheet()
        self._load_from_config()

    def _apply_dialog_stylesheet(self) -> None:
        theme_cfg = self.config_mgr.get("theme", {})
        pal = _derive_theme_palette(theme_cfg if isinstance(theme_cfg, dict) else {})
        bg_main = pal["BG_MAIN"]
        bg_card = pal["BG_CARD"]
        bg_inner = pal["BG_INNER"]
        bg_hover = pal["BG_HOVER"]
        fg_primary = pal["FG_PRIMARY"]
        fg_muted = pal["FG_MUTED"]
        fg_accent = pal["FG_ACCENT"]
        border_col = pal["BORDER_COLOR"]

        self.setStyleSheet(
            f"""
            QDialog {{
                background-color: {bg_main};
                color: {fg_primary};
                font-family: "Microsoft YaHei UI";
                font-size: 9pt;
            }}
            QLabel {{
                color: {fg_primary};
                background: transparent;
            }}
            QTabWidget::pane {{
                background-color: {bg_card};
                border: 1px solid {border_col};
                border-radius: 4px;
            }}
            QTabBar::tab {{
                background-color: {bg_inner};
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
                background-color: {bg_card};
                color: {fg_accent};
            }}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
                background-color: {bg_inner};
                color: {fg_primary};
                border: 1px solid {border_col};
                border-radius: 3px;
                padding: 4px 6px;
                selection-background-color: {fg_accent};
                selection-color: {bg_main};
            }}
            QComboBox QAbstractItemView {{
                background-color: {bg_card};
                color: {fg_primary};
                border: 1px solid {border_col};
                selection-background-color: {fg_accent};
                selection-color: {bg_main};
            }}
            QPushButton {{
                background-color: {bg_inner};
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
                background-color: {bg_inner};
                color: {fg_primary};
                gridline-color: {border_col};
                border: 1px solid {border_col};
                border-radius: 3px;
                selection-background-color: {bg_hover};
                selection-color: {fg_accent};
            }}
            QHeaderView::section {{
                background-color: {bg_card};
                color: {fg_muted};
                border: 1px solid {border_col};
                padding: 4px 6px;
                font-weight: bold;
            }}
            """
        )

    def _build_dialog_ui(self) -> None:
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(12, 12, 12, 12)
        root_lay.setSpacing(10)

        self.tabs = QTabWidget(self)
        root_lay.addWidget(self.tabs, 1)

        # ------------------------------------------------------------------
        # Tab 1: Agents 监控源管理
        # ------------------------------------------------------------------
        tab_agents = QWidget(self)
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
        self.main_path_edit.textChanged.connect(lambda _t: self._update_main_path_status())
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
        sub_title = QLabel("子 Agent 监控源列表（留空时左侧隐藏主/子切换栏）：", self.sub_section_widget)
        sub_title.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Bold))
        sub_head_row.addWidget(sub_title, 1)

        self.add_sub_btn = QPushButton("+ 添加", self.sub_section_widget)
        self.add_sub_btn.clicked.connect(lambda: self.add_sub_agent_row())
        sub_head_row.addWidget(self.add_sub_btn, 0)

        self.browse_sub_btn = QPushButton("浏览路径", self.sub_section_widget)
        self.browse_sub_btn.clicked.connect(self._on_browse_sub_path)
        sub_head_row.addWidget(self.browse_sub_btn, 0)

        self.remove_sub_btn = QPushButton("删除", self.sub_section_widget)
        self.remove_sub_btn.clicked.connect(self.remove_selected_sub_agent_row)
        sub_head_row.addWidget(self.remove_sub_btn, 0)
        sub_sec_lay.addLayout(sub_head_row, 0)

        self.sub_table = QTableWidget(0, 4, self.sub_section_widget)
        self.sub_table.setHorizontalHeaderLabels(
            ["名称", "监控路径", "解析器 (parser_type)", "实时状态文件 (可选)"]
        )
        self.sub_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sub_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sub_table.verticalHeader().setVisible(False)
        hdr = self.sub_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.sub_table.setColumnWidth(0, 110)
        sub_sec_lay.addWidget(self.sub_table, 1)

        self.sub_section_widget.hide()
        agents_lay.addWidget(self.sub_section_widget, 0)
        agents_lay.addStretch(1)

        self.tabs.addTab(tab_agents, "Agents 监控源")

        # ------------------------------------------------------------------
        # Tab 2: Appearance 外观与配色
        # ------------------------------------------------------------------
        tab_appear = QWidget(self)
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
            edit.textChanged.connect(lambda txt, k=key: self._update_color_swatch(k, txt))
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
        tab_perf = QWidget(self)
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
        form_perf.addRow("界面刷新频次 (250 ~ 5000ms)：", self.poll_spin)

        self.ttl_spin = QDoubleSpinBox(tab_perf)
        self.ttl_spin.setRange(1.0, 10.0)
        self.ttl_spin.setSingleStep(0.5)
        self.ttl_spin.setDecimals(1)
        self.ttl_spin.setSuffix(" s")
        form_perf.addRow("目录扫描缓存 TTL (1.0 ~ 10.0s)：", self.ttl_spin)

        perf_lay.addLayout(form_perf, 0)
        perf_hint = QLabel(
            "说明：后台采用独立工作线程增量解析 JSONL 日志，调低刷新间隔可提升实时性，调高扫描 TTL 可减少磁盘目录遍历次数。",
            tab_perf,
        )
        perf_hint.setWordWrap(True)
        perf_hint.setStyleSheet("color: #928374;")
        perf_lay.addWidget(perf_hint, 0)
        perf_lay.addStretch(1)
        self.tabs.addTab(tab_perf, "Performance 性能")

        # ------------------------------------------------------------------
        # 底部操作按钮栏
        # ------------------------------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.save_btn = QPushButton("保存并应用", self)
        self.save_btn.setObjectName("primarySaveBtn")
        self.save_btn.clicked.connect(self.save_and_apply)
        btn_row.addWidget(self.save_btn, 0)

        self.cancel_btn = QPushButton("取消", self)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn, 0)

        root_lay.addLayout(btn_row, 0)

    def _load_from_config(self) -> None:
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
        finally:
            self._updating_preset_fields = False

        poll_ms = int(self.config_mgr.get("poll_interval_ms", 1000) or 1000)
        self.poll_spin.setValue(max(250, min(5000, poll_ms)))

        ttl_sec = float(self.config_mgr.get("scan_ttl_sec", 2.5) or 2.5)
        self.ttl_spin.setValue(max(1.0, min(10.0, ttl_sec)))

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
            self.main_status_lbl.setStyleSheet("color: #A9B665; font-weight: bold;")
        else:
            self.main_status_lbl.setText("🔴 路径无效")
            self.main_status_lbl.setStyleSheet("color: #EA6962; font-weight: bold;")

    def _on_browse_main_path(self) -> None:
        start_dir = self.main_path_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择 Antigravity Brain 监控目录", start_dir)
        if chosen:
            self.main_path_edit.setText(chosen)
            self._update_main_path_status()

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
        self._update_main_path_status()

    def add_sub_agent_row(
        self,
        name: str = "",
        path: str = "",
        parser_type: str = "gemini_cli_session",
        live_state_path: str = "",
    ) -> int:
        row_idx = self.sub_table.rowCount()
        self.sub_table.insertRow(row_idx)

        resolved_name = ConfigManager._resolve_sub_agent_display_name(name, path) if path else name
        name_item = QTableWidgetItem(resolved_name)
        path_item = QTableWidgetItem(path)
        live_item = QTableWidgetItem(live_state_path)
        self.sub_table.setItem(row_idx, 0, name_item)
        self.sub_table.setItem(row_idx, 1, path_item)

        parser_combo = QComboBox(self.sub_table)
        parser_combo.addItems(["gemini_cli_session", "antigravity_transcript"])
        if parser_type in ("gemini_cli_session", "antigravity_transcript"):
            parser_combo.setCurrentText(parser_type)
        self.sub_table.setCellWidget(row_idx, 2, parser_combo)
        self.sub_table.setItem(row_idx, 2, QTableWidgetItem(parser_combo.currentText()))
        parser_combo.currentTextChanged.connect(
            lambda txt, r=row_idx: (
                self.sub_table.item(r, 2).setText(txt)
                if self.sub_table.item(r, 2) is not None
                else None
            )
        )

        self.sub_table.setItem(row_idx, 3, live_item)
        self.sub_table.selectRow(row_idx)
        return row_idx

    def _on_browse_sub_path(self) -> None:
        row_idx = self.sub_table.currentRow()
        if row_idx < 0:
            if self.sub_table.rowCount() == 0:
                row_idx = self.add_sub_agent_row()
            else:
                row_idx = self.sub_table.rowCount() - 1
        cur_item = self.sub_table.item(row_idx, 1)
        start_dir = (cur_item.text().strip() if cur_item else "") or str(GEMINI_TMP_ROOT)
        chosen = QFileDialog.getExistingDirectory(self, "选择子 Agent 会话日志目录", start_dir)
        if chosen:
            if cur_item is None:
                cur_item = QTableWidgetItem(chosen)
                self.sub_table.setItem(row_idx, 1, cur_item)
            else:
                cur_item.setText(chosen)
            name_item = self.sub_table.item(row_idx, 0)
            cur_name = name_item.text().strip() if name_item else ""
            if not cur_name:
                auto_name = ConfigManager._resolve_sub_agent_display_name("", chosen)
                if name_item is None:
                    self.sub_table.setItem(row_idx, 0, QTableWidgetItem(auto_name))
                else:
                    name_item.setText(auto_name)

    def remove_selected_sub_agent_row(self) -> None:
        row_idx = self.sub_table.currentRow()
        if row_idx < 0 and self.sub_table.rowCount() > 0:
            row_idx = self.sub_table.rowCount() - 1
        if row_idx >= 0:
            self.sub_table.removeRow(row_idx)

    def _collect_sub_agents(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for r in range(self.sub_table.rowCount()):
            name_item = self.sub_table.item(r, 0)
            path_item = self.sub_table.item(r, 1)
            parser_widget = self.sub_table.cellWidget(r, 2)
            parser_item = self.sub_table.item(r, 2)
            live_item = self.sub_table.item(r, 3)

            path_val = path_item.text().strip() if path_item else ""
            raw_name = name_item.text().strip() if name_item else ""
            name_val = ConfigManager._resolve_sub_agent_display_name(raw_name, path_val)
            if isinstance(parser_widget, QComboBox):
                parser_val = parser_widget.currentText().strip()
            elif parser_item is not None:
                parser_val = parser_item.text().strip()
            else:
                parser_val = "gemini_cli_session"
            if parser_val not in ("gemini_cli_session", "antigravity_transcript"):
                parser_val = "gemini_cli_session"
            live_val = live_item.text().strip() if live_item else ""

            if path_val:
                result.append(
                    {
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

    def save_and_apply(self) -> None:
        main_path = self.main_path_edit.text().strip()
        sub_agents = self._collect_sub_agents()
        poll_ms = int(self.poll_spin.value())
        ttl_sec = round(float(self.ttl_spin.value()), 2)

        preset_name = self.preset_combo.currentText().strip() or "Gruvbox Material"
        base_preset = THEME_PRESETS.get(preset_name, THEME_PRESETS["Gruvbox Material"])
        theme_data: dict[str, str] = {"preset": preset_name}
        for key, _ in self.COLOR_FIELD_SPECS:
            raw_hex = self.color_edits[key].text().strip()
            theme_data[key] = raw_hex if _HEX_COLOR_RE.match(raw_hex) else base_preset[key]

        self.config_mgr.set("main_agent_path", main_path)
        self.config_mgr.set("sub_agents", sub_agents)
        self.config_mgr.set("poll_interval_ms", poll_ms)
        self.config_mgr.set("scan_ttl_sec", ttl_sec)
        self.config_mgr.set("theme", theme_data)
        self.config_mgr.save()

        if self._hud_parent is not None and hasattr(self._hud_parent, "apply_settings"):
            self._hud_parent.apply_settings()

        self.accept()


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
        cfg_poll_ms = int(self.config_mgr.get("poll_interval_ms", refresh_ms) or refresh_ms)
        self.refresh_ms = max(250, refresh_ms if refresh_ms != 1000 else cfg_poll_ms)
        self.target_session = target_session
        self.target_project = target_project
        self.auto_close_idle_sec = auto_close_idle_sec
        self.alpha = max(0.5, min(1.0, alpha))
        self._settings_dialog: SettingsDialog | None = None

        self.current_card_idx = 0
        self._in_mainloop = False
        self.sidebar_filter = "all"  # "all" | "main" | "sub"
        self.pinned_main_file: str | None = None
        self.pinned_sub_file: str | None = None
        self._last_main_sessions: list[dict[str, Any]] = []
        self._last_sub_sessions: list[dict[str, Any]] = []
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
        self.dwm_info: dict[str, Any] = {}

        # 初始化时同步一次主题调色板
        self._sync_palette_from_config()

        # 无边框 + 置顶 + 工具窗口属性
        self.setWindowTitle("Antigravity & Argon Token HUD")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setMinimumSize(self.RESIZE_MIN_W, self.RESIZE_MIN_H)
        self.setWindowOpacity(self.alpha)
        self.setStyleSheet(
            f"""
            TokenHUDWindow {{
                background-color: {self.BG_MAIN};
                border: 1px solid {self.BORDER_COLOR};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """
        )

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

        self._timer = QTimer(self)
        self._timer.setInterval(self.refresh_ms)
        self._timer.timeout.connect(self._schedule_tick)

        # 显示并应用 Windows 11 DWM 圆角与 Mica 材质
        self.show()
        QApplication.processEvents()
        self.dwm_info = apply_win11_mica_and_rounded_corners(self)
        self._top_hwnd = resolve_top_level_hwnd(self) or int(self.dwm_info.get("hwnd") or 0)

        # 兼容属性封装（供自动化自检脚本无缝读取）
        self.root = _CompatWidgetGeoAdapter(self)
        self.title_lbl = _CompatLabelAdapter(self._title_qlabel)
        self.grand_total_lbl = _CompatLabelAdapter(self._grand_total_qlabel)
        self.sidebar_frame = _CompatWidgetGeoAdapter(self._sidebar_qframe)
        self.deck = _CompatWidgetGeoAdapter(self._deck_qwidget)

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
        self.header.setStyleSheet(f"background-color: {self.BG_HEADER}; border: none;")
        self.header_lay = QHBoxLayout(self.header)
        self.header_lay.setContentsMargins(10, 0, 6, 0)
        self.header_lay.setSpacing(6)

        self.status_dot = QLabel("●", self.header)
        self.status_dot.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.status_dot.setStyleSheet(f"color: {self.FG_GREEN};")
        self.status_dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.header_lay.addWidget(self.status_dot, 0)

        self._title_qlabel = QLabel(self.CARD_TITLES[0], self.header)
        self._title_qlabel.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Bold))
        self._title_qlabel.setStyleSheet(f"color: {self.FG_PRIMARY};")
        self._title_qlabel.setMinimumWidth(0)
        self._title_qlabel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.header_lay.addWidget(self._title_qlabel, 0)

        self.header_lay.addStretch(1)

        self.reset_active_btn = _HoverLabel("[返回总用量]", self.header)
        self.reset_active_btn.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        self.reset_active_btn.setStyleSheet(
            f"color: {self.FG_GREEN}; background-color: {self.BG_INNER}; padding: 2px 6px; border-radius: 3px;"
        )
        self.reset_active_btn.clicked.connect(self._reset_pinned_session)
        self.reset_active_btn.hide()
        self.header_lay.addWidget(self.reset_active_btn, 0)

        self.switch_hint_lbl = QLabel("", self.header)
        self.switch_hint_lbl.setFont(QFont("Microsoft YaHei UI", 8))
        self.switch_hint_lbl.setStyleSheet(f"color: {self.FG_ACCENT}; padding: 0 4px;")
        self.switch_hint_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.switch_hint_lbl.hide()
        self.header_lay.addWidget(self.switch_hint_lbl, 0)

        self._grand_total_qlabel = _HoverLabel(self._grand_total_compact_text, self.header)
        self._grand_total_qlabel.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        self._grand_total_qlabel.setMinimumWidth(0)
        self._grand_total_qlabel.setStyleSheet(
            f"color: {self.FG_YELLOW}; background-color: {self.BG_INNER}; padding: 2px 6px; border-radius: 3px;"
        )
        self._grand_total_qlabel.hovered.connect(self._on_grand_total_hovered)
        self._grand_total_qlabel.clicked.connect(self._reset_pinned_session)
        self.header_lay.addWidget(self._grand_total_qlabel, 0)

        self.settings_btn = _HoverLabel("⚙", self.header)
        self.settings_btn.setFont(QFont("Segoe UI Symbol", 9, QFont.Weight.Bold))
        self.settings_btn.setToolTip("打开设置面板")
        self.settings_btn.setStyleSheet(f"color: {self.FG_MUTED}; padding: 2px 6px;")
        self.settings_btn.hovered.connect(
            lambda h: self.settings_btn.setStyleSheet(
                f"color: {self.FG_ACCENT if h else self.FG_MUTED}; padding: 2px 6px;"
            )
        )
        self.settings_btn.clicked.connect(self.open_settings)
        self.header_lay.addWidget(self.settings_btn, 0)

        self.close_btn = _HoverLabel("✕", self.header)
        self.close_btn.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.close_btn.setStyleSheet(f"color: {self.FG_MUTED}; padding: 2px 8px;")
        self.close_btn.hovered.connect(
            lambda h: self.close_btn.setStyleSheet(
                f"color: {self.FG_RED if h else self.FG_MUTED}; padding: 2px 8px;"
            )
        )
        self.close_btn.clicked.connect(self.close)
        self.header_lay.addWidget(self.close_btn, 0)

        outer_layout.addWidget(self.header, 0)

        # ------------------------------------------------------------------
        # 2. 主体常驻左右双栏容器（中间含可拖拽横向分隔手柄）
        # ------------------------------------------------------------------
        self.body_frame = QFrame(self)
        self.body_frame.setStyleSheet(f"background-color: {self.BG_MAIN}; border: none;")
        self.body_lay = QHBoxLayout(self.body_frame)
        self.body_lay.setContentsMargins(8, 6, 8, 8)
        self.body_lay.setSpacing(0)

        self._build_sidebar()
        self.body_lay.addWidget(self._sidebar_qframe, 0)

        self.splitter_handle = SplitterHandleWidget(self, self.body_frame)
        self.body_lay.addWidget(self.splitter_handle, 0)

        self._deck_qwidget = QStackedWidget(self.body_frame)
        self._deck_qwidget.setStyleSheet(f"background-color: {self.BG_MAIN}; border: none;")

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
        self._sidebar_qframe = QFrame(self.body_frame)
        self._sidebar_qframe.setFixedWidth(self.SIDEBAR_W)
        self._sidebar_qframe.setStyleSheet(
            f"QFrame#sidebarRoot {{ background-color: {self.BG_CARD}; border: 1px solid {self.BORDER_COLOR}; border-radius: 4px; }}"
        )
        self._sidebar_qframe.setObjectName("sidebarRoot")

        self.sb_lay = QVBoxLayout(self._sidebar_qframe)
        self.sb_lay.setContentsMargins(4, 4, 4, 4)
        self.sb_lay.setSpacing(3)

        # 顶部过滤切换栏（专注主 Agent 模式常驻隐藏）
        self.filter_bar = QFrame(self._sidebar_qframe)
        self.filter_bar.setStyleSheet(f"background-color: {self.BG_INNER}; border: none; border-radius: 3px;")
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
        self.sidebar_count_lbl.setStyleSheet(f"color: {self.FG_MUTED}; padding: 1px 4px;")
        self.sidebar_count_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sidebar_count_lbl.setMinimumWidth(0)
        self.sb_lay.addWidget(self.sidebar_count_lbl, 0)

        # 静默滚动区域（彻底隐藏垂直与水平滚动条）
        self.sidebar_scroll = QScrollArea(self._sidebar_qframe)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setStyleSheet(f"background-color: {self.BG_CARD}; border: none;")

        self.sidebar_list_inner = QWidget(self.sidebar_scroll)
        self.sidebar_list_inner.setStyleSheet(f"background-color: {self.BG_CARD}; border: none;")
        self.sidebar_list_inner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sidebar_list_inner.setMinimumWidth(0)
        self.sidebar_list_layout = QVBoxLayout(self.sidebar_list_inner)
        self.sidebar_list_layout.setContentsMargins(1, 1, 1, 1)
        self.sidebar_list_layout.setSpacing(3)
        self.sidebar_list_layout.addStretch(1)

        self.empty_sidebar_lbl = QLabel("暂无匹配的会话记录", self.sidebar_list_inner)
        self.empty_sidebar_lbl.setFont(QFont("Microsoft YaHei UI", 8))
        self.empty_sidebar_lbl.setStyleSheet(f"color: {self.FG_MUTED}; padding: 16px;")
        self.empty_sidebar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_sidebar_lbl.hide()
        self.sidebar_list_layout.insertWidget(0, self.empty_sidebar_lbl)

        self.sidebar_scroll.setWidget(self.sidebar_list_inner)
        self.sb_lay.addWidget(self.sidebar_scroll, 1)

    def _update_filter_btn_styles(self) -> None:
        for k, btn in self.filter_btns.items():
            if k == self.sidebar_filter:
                btn.setStyleSheet(
                    f"color: {self.FG_ACCENT}; background-color: {self.BG_CARD}; border-radius: 2px;"
                )
            else:
                btn.setStyleSheet(
                    f"color: {self.FG_MUTED}; background-color: {self.BG_INNER}; border-radius: 2px;"
                )

    def _build_agent_card(self, engine_label: str, accent_color: str) -> dict[str, Any]:
        """构建主/子 Agent 卡片视图（全高度自适应填充 + 2x2 细分指标大字网格，消除底部留白）。"""
        frame = QFrame(self)
        frame.setObjectName("agentCard")
        frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        frame.setMinimumSize(0, 0)
        frame.setStyleSheet(
            f"QFrame#agentCard {{ background-color: {self.BG_CARD}; border: 1px solid {self.BORDER_COLOR}; border-radius: 4px; }}"
        )
        lay = QVBoxLayout(frame)
        lay.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # 1. 顶部引擎标题行（固定紧凑占比）
        row_head = QHBoxLayout()
        row_head.setContentsMargins(0, 0, 0, 0)
        engine_title_lbl = QLabel(engine_label, frame)
        engine_title_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        engine_title_lbl.setStyleSheet(f"color: {accent_color};")
        engine_title_lbl.setMinimumWidth(0)
        engine_title_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row_head.addWidget(engine_title_lbl, 0)

        row_head.addStretch(1)

        back_active_lbl = _HoverLabel("[返回总用量]", frame)
        back_active_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        back_active_lbl.setStyleSheet(
            f"color: {self.FG_GREEN}; background-color: {self.BG_INNER}; padding: 1px 6px; border-radius: 3px;"
        )
        back_active_lbl.clicked.connect(self._reset_pinned_session)
        back_active_lbl.hide()
        row_head.addWidget(back_active_lbl, 0)

        status_lbl = QLabel("[就绪]", frame)
        status_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        status_lbl.setStyleSheet(f"color: {self.FG_MUTED};")
        status_lbl.setMinimumWidth(0)
        status_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row_head.addWidget(status_lbl, 0)
        lay.addLayout(row_head, 0)

        # 2. 会话元信息面板（随高度弹性伸展，内部垂直均匀分布）
        info_frame = QFrame(frame)
        info_frame.setStyleSheet(f"background-color: {self.BG_INNER}; border-radius: 4px;")
        info_frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        info_frame.setMinimumWidth(0)
        info_frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay = QVBoxLayout(info_frame)
        info_lay.setContentsMargins(10, 5, 10, 5)
        info_lay.setSpacing(2)

        session_lbl = QLabel("会话: 无", info_frame)
        session_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        session_lbl.setStyleSheet(f"color: {self.FG_PRIMARY};")
        session_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        session_lbl.setMinimumWidth(0)
        session_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(session_lbl, 1)

        meta_lbl = QLabel("ID: - | 模型: - | 耗时: 0s", info_frame)
        meta_lbl.setFont(QFont("Consolas", 8))
        meta_lbl.setStyleSheet(f"color: {self.FG_MUTED};")
        meta_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        meta_lbl.setMinimumWidth(0)
        meta_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(meta_lbl, 1)

        step_lbl = QLabel("进度: 0 轮 | 最近工具: -", info_frame)
        step_lbl.setFont(QFont("Microsoft YaHei UI", 8))
        step_lbl.setStyleSheet(f"color: {self.FG_PRIMARY};")
        step_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        step_lbl.setMinimumWidth(0)
        step_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(step_lbl, 1)
        lay.addWidget(info_frame, 3)

        # 3. 核心大字指标框（本会话累计 Token + 当前上下文水位）
        tot_frame = QFrame(frame)
        tot_frame.setObjectName("totFrame")
        tot_frame.setStyleSheet(
            f"QFrame#totFrame {{ background-color: {self.BG_INNER}; border: 1px solid {self.BORDER_COLOR}; border-radius: 4px; }}"
        )
        tot_frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        tot_frame.setMinimumWidth(0)
        tot_frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        tot_lay = QHBoxLayout(tot_frame)
        tot_lay.setContentsMargins(10, 6, 10, 6)

        tot_left = QVBoxLayout()
        tot_left.setSpacing(2)
        tot_left.addStretch(1)
        lbl_t1 = QLabel("本会话累计 Token", tot_frame)
        lbl_t1.setFont(QFont("Microsoft YaHei UI", 7))
        lbl_t1.setStyleSheet(f"color: {self.FG_MUTED};")
        lbl_t1.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lbl_t1.setMinimumWidth(0)
        total_val = QLabel("0", tot_frame)
        total_val.setFont(QFont("Consolas", 15, QFont.Weight.Bold))
        total_val.setStyleSheet(f"color: {self.FG_GREEN};")
        total_val.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        total_val.setMinimumWidth(0)
        tot_left.addWidget(lbl_t1)
        tot_left.addWidget(total_val)
        tot_left.addStretch(1)
        tot_lay.addLayout(tot_left, 3)

        tot_right = QVBoxLayout()
        tot_right.setSpacing(2)
        tot_right.addStretch(1)
        lbl_t2 = QLabel("当前上下文水位", tot_frame)
        lbl_t2.setFont(QFont("Microsoft YaHei UI", 7))
        lbl_t2.setStyleSheet(f"color: {self.FG_MUTED};")
        lbl_t2.setAlignment(Qt.AlignmentFlag.AlignRight)
        lbl_t2.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lbl_t2.setMinimumWidth(0)
        ctx_val = QLabel("0", tot_frame)
        ctx_val.setFont(QFont("Consolas", 13, QFont.Weight.Bold))
        ctx_val.setStyleSheet(f"color: {self.FG_ACCENT};")
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
        frame = QFrame(self)
        frame.setObjectName("summaryCard")
        frame.setStyleSheet(
            f"QFrame#summaryCard {{ background-color: {self.BG_CARD}; border: 1px solid {self.BORDER_COLOR}; border-radius: 4px; }}"
        )
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        row_head = QHBoxLayout()
        row_head.setContentsMargins(0, 0, 0, 0)
        t_lbl = QLabel("Token 总用量", frame)
        t_lbl.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.Bold))
        t_lbl.setStyleSheet(f"color: {self.FG_ACCENT};")
        t_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row_head.addWidget(t_lbl, 0)
        row_head.addStretch(1)

        date_lbl = QLabel(datetime.date.today().strftime("%Y-%m-%d"), frame)
        date_lbl.setFont(QFont("Consolas", 8))
        date_lbl.setStyleSheet(f"color: {self.FG_MUTED};")
        date_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row_head.addWidget(date_lbl, 0)
        lay.addLayout(row_head, 0)

        info_frame = QFrame(frame)
        info_frame.setStyleSheet(f"background-color: {self.BG_INNER}; border-radius: 4px;")
        info_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        info_frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay = QVBoxLayout(info_frame)
        info_lay.setContentsMargins(10, 5, 10, 5)
        info_lay.setSpacing(2)

        counts_lbl = QLabel("总会话: 0 次 (主 0 / 从 0) | 总步数: 0 步 (主 0 / 从 0)", info_frame)
        counts_lbl.setFont(QFont("Microsoft YaHei UI", 8))
        counts_lbl.setStyleSheet(f"color: {self.FG_PRIMARY};")
        counts_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(counts_lbl, 1)

        split_lbl = QLabel("主 Agent 累计: 0 | 子 Agent 累计: 0", info_frame)
        split_lbl.setFont(QFont("Consolas", 8))
        split_lbl.setStyleSheet(f"color: {self.FG_MUTED};")
        split_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        info_lay.addWidget(split_lbl, 1)
        lay.addWidget(info_frame, 3)

        tot_frame = QFrame(frame)
        tot_frame.setObjectName("totFrame")
        tot_frame.setStyleSheet(
            f"QFrame#totFrame {{ background-color: {self.BG_INNER}; border: 1px solid {self.BORDER_COLOR}; border-radius: 4px; }}"
        )
        tot_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        tot_frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        tot_lay = QHBoxLayout(tot_frame)
        tot_lay.setContentsMargins(10, 6, 10, 6)

        tot_left = QVBoxLayout()
        tot_left.setSpacing(2)
        tot_left.addStretch(1)
        lbl_t1 = QLabel("今日累计总 Token", tot_frame)
        lbl_t1.setFont(QFont("Microsoft YaHei UI", 7))
        lbl_t1.setStyleSheet(f"color: {self.FG_MUTED};")
        total_val = QLabel("0", tot_frame)
        total_val.setFont(QFont("Consolas", 15, QFont.Weight.Bold))
        total_val.setStyleSheet(f"color: {self.FG_ACCENT};")
        tot_left.addWidget(lbl_t1)
        tot_left.addWidget(total_val)
        tot_left.addStretch(1)
        tot_lay.addLayout(tot_left, 3)

        tot_right = QVBoxLayout()
        tot_right.setSpacing(2)
        tot_right.addStretch(1)
        lbl_t2 = QLabel("综合缓存命中率", tot_frame)
        lbl_t2.setFont(QFont("Microsoft YaHei UI", 7))
        lbl_t2.setStyleSheet(f"color: {self.FG_MUTED};")
        lbl_t2.setAlignment(Qt.AlignmentFlag.AlignRight)
        cache_rate_val = QLabel("0.0%", tot_frame)
        cache_rate_val.setFont(QFont("Consolas", 13, QFont.Weight.Bold))
        cache_rate_val.setStyleSheet(f"color: {self.FG_GREEN};")
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
        cell.setStyleSheet(f"background-color: {self.BG_INNER}; border-radius: 4px;")
        cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        cell.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        c_lay = QVBoxLayout(cell)
        c_lay.setContentsMargins(8, 5, 8, 5)
        c_lay.setSpacing(2)
        c_lay.addStretch(1)

        t_lbl = QLabel(title, cell)
        t_lbl.setFont(QFont("Microsoft YaHei UI", 7))
        t_lbl.setStyleSheet(f"color: {self.FG_MUTED};")
        val_lbl = QLabel("0", cell)
        val_lbl.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        val_lbl.setStyleSheet(f"color: {color};")
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

        # 3. 左右双栏比例（支持横向分隔手柄拖拽占比）与间距自适应
        b_pad = max(5, min(16, int(round(8 * scale))))
        b_top = max(4, min(14, int(round(6 * scale))))
        self.body_lay.setContentsMargins(b_pad, b_top, b_pad, b_pad)
        self.body_lay.setSpacing(0)

        splitter_w = max(8, min(14, int(round(10 * scale))))
        if hasattr(self, "splitter_handle"):
            self.splitter_handle.setFixedWidth(splitter_w)

        avail_body_w = max(380, w - b_pad * 2 - splitter_w)
        min_sb_w = 155
        max_sb_w = max(min_sb_w + 20, avail_body_w - 250)
        sidebar_w = max(min_sb_w, min(max_sb_w, int(round(avail_body_w * self._sidebar_ratio))))
        self._current_sidebar_w = sidebar_w
        self._sidebar_qframe.setFixedWidth(sidebar_w)

        filter_h = max(18, min(36, int(round(22 * scale))))
        filter_pt = max(7, min(13, int(round(8 * scale))))
        for btn in self.filter_btns.values():
            btn.setFixedHeight(filter_h)
            btn.setFont(QFont("Microsoft YaHei UI", filter_pt, QFont.Weight.Bold))

        cnt_pt = max(6, min(12, int(round(7 * scale))))
        self.sidebar_count_lbl.setFont(QFont("Microsoft YaHei UI", cnt_pt))

        for row in self._sidebar_rows.values():
            row.apply_scale(scale, sidebar_w)

        # 4. 右侧 Token 统计页全区域宽高自适应（结合右侧实际宽高二维缩放）
        deck_avail_w = max(250, avail_body_w - sidebar_w)
        card_avail_h = max(195, h - header_h - b_top - b_pad - 2)

        deck_sx = deck_avail_w / 465.0
        deck_sy = card_avail_h / 280.0
        card_scale = max(0.72, min(2.5, deck_sx * 0.48 + deck_sy * 0.52))

        c_pad_x = max(7, min(22, int(round(10 * card_scale))))
        c_pad_y = max(5, min(18, int(round(7 * card_scale))))
        c_space = max(3, min(12, int(round(5 * card_scale))))

        info_pad_x = max(7, min(20, int(round(10 * card_scale))))
        info_pad_y = max(3, min(12, int(round(5 * card_scale))))
        tot_pad_x = max(8, min(22, int(round(10 * card_scale))))
        tot_pad_y = max(4, min(14, int(round(6 * card_scale))))
        grid_space = max(4, min(14, int(round(6 * card_scale))))
        cell_pad_x = max(6, min(18, int(round(9 * card_scale))))
        cell_pad_y = max(3, min(12, int(round(5 * card_scale))))

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

        big_main_target_pt = max(11, min(32, int(round(15.5 * card_scale))))
        big_sub_target_pt = max(10, min(26, int(round(13.0 * card_scale))))
        grid_val_target_pt = max(9, min(22, int(round(11.5 * card_scale))))

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
                f"color: {self.FG_PURPLE}; background-color: {self.BG_HOVER}; padding: 2px 6px; border-radius: 3px;"
            )
        else:
            self._grand_total_qlabel.setText(self._grand_total_compact_text)
            self._grand_total_qlabel.setStyleSheet(
                f"color: {self.FG_YELLOW}; background-color: {self.BG_INNER}; padding: 2px 6px; border-radius: 3px;"
            )
        self._adjust_window_geometry()

    def _on_mousewheel(self, event: Any) -> None:
        """兼容自检与滚轮滚动调用。"""
        delta = getattr(event, "delta", -120)
        bar = self.sidebar_scroll.verticalScrollBar()
        bar.setValue(bar.value() - int(delta / 2))

    def _set_sidebar_filter(self, filter_key: str) -> None:
        self.sidebar_filter = "main"
        self._update_filter_btn_styles()
        self._sidebar_order_sig = None
        self._refresh_sidebar_items()

    def _update_sidebar_selection_visuals(self) -> None:
        """毫秒级选中态更新：仅触发受影响行的 QPainter 局部重绘，零布局重排。"""
        for (eng, fp), row_widget in self._sidebar_rows.items():
            is_sel = eng == "main" and fp and fp == self.pinned_main_file
            row_widget.set_selected(is_sel)

    def _select_history_session(self, engine_type: str, file_path: str) -> None:
        if engine_type == "main":
            self.pinned_main_file = file_path
        self.pinned_sub_file = None
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
        渲染侧边栏 Antigravity 会话列表：
        - 所有 SidebarRowWidget 实例永久缓存在 self._sidebar_rows 中；
        - 数值变化（Token/轮数/时间）仅原地更新 QLabel 文本，不触发布局重建；
        - 仅当列表顺序改变时调整布局顺序。
        """
        if hasattr(self, "filter_bar"):
            self.filter_bar.hide()

        items: list[tuple[str, dict[str, Any]]] = []
        for s in self._last_main_sessions:
            if int(s.get("steps", 0)) > 0 or int(s.get("total_tokens", 0)) > 0:
                items.append(("main", s))

        items.sort(key=lambda pair: float(pair[1].get("last_updated") or 0.0), reverse=True)
        self.sidebar_count_lbl.setText(f"共 {len(items)} 条会话 · 点击切换")

        order_keys = [(eng, str(d.get("file_path") or "")) for eng, d in items]
        order_sig = ("main", tuple(order_keys))

        palette = self._get_current_palette()
        # 无论顺序是否改变，先原地更新每个条目的文本与选中状态
        for eng, sess in items:
            fpath = str(sess.get("file_path") or "")
            key = (eng, fpath)
            is_selected = bool(eng == "main" and fpath and fpath == self.pinned_main_file)
            row = self._sidebar_rows.get(key)
            if row is None:
                row = SidebarRowWidget(eng, fpath, self.sidebar_list_inner)
                row.row_clicked.connect(self._select_history_session)
                row.apply_theme(palette)
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

    def _sync_palette_from_config(self) -> dict[str, str]:
        theme_cfg = self.config_mgr.get("theme", {})
        palette = _derive_theme_palette(theme_cfg if isinstance(theme_cfg, dict) else {})
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
        return palette

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

    def apply_theme(self) -> None:
        """动态热重载主窗口、卡片视图与所有 SidebarRowWidget 的主题配色样式。"""
        palette = self._sync_palette_from_config()

        self.setStyleSheet(
            f"""
            TokenHUDWindow {{
                background-color: {self.BG_MAIN};
                border: 1px solid {self.BORDER_COLOR};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """
        )

        if hasattr(self, "header"):
            self.header.setStyleSheet(f"background-color: {self.BG_HEADER}; border: none;")
        if hasattr(self, "_title_qlabel"):
            self._title_qlabel.setStyleSheet(f"color: {self.FG_PRIMARY};")
        if hasattr(self, "_grand_total_qlabel"):
            self._on_grand_total_hovered(self._is_hovering_grand_total)
        if hasattr(self, "reset_active_btn"):
            self.reset_active_btn.setStyleSheet(
                f"color: {self.FG_GREEN}; background-color: {self.BG_INNER}; padding: 2px 6px; border-radius: 3px;"
            )
        if hasattr(self, "switch_hint_lbl"):
            self.switch_hint_lbl.setStyleSheet(f"color: {self.FG_ACCENT}; padding: 0 4px;")
        if hasattr(self, "settings_btn"):
            self.settings_btn.setStyleSheet(f"color: {self.FG_MUTED}; padding: 2px 6px;")
        if hasattr(self, "close_btn"):
            self.close_btn.setStyleSheet(f"color: {self.FG_MUTED}; padding: 2px 8px;")

        if hasattr(self, "body_frame"):
            self.body_frame.setStyleSheet(f"background-color: {self.BG_MAIN}; border: none;")
        if hasattr(self, "_deck_qwidget"):
            self._deck_qwidget.setStyleSheet(f"background-color: {self.BG_MAIN}; border: none;")

        if hasattr(self, "_sidebar_qframe"):
            self._sidebar_qframe.setStyleSheet(
                f"QFrame#sidebarRoot {{ background-color: {self.BG_CARD}; border: 1px solid {self.BORDER_COLOR}; border-radius: 4px; }}"
            )
        if hasattr(self, "filter_bar"):
            self.filter_bar.setStyleSheet(f"background-color: {self.BG_INNER}; border: none; border-radius: 3px;")
            self.filter_bar.hide()
        if hasattr(self, "filter_btns"):
            self._update_filter_btn_styles()
        if hasattr(self, "sidebar_count_lbl"):
            self.sidebar_count_lbl.setStyleSheet(f"color: {self.FG_MUTED}; padding: 1px 4px;")
        if hasattr(self, "sidebar_scroll"):
            self.sidebar_scroll.setStyleSheet(f"background-color: {self.BG_CARD}; border: none;")
        if hasattr(self, "sidebar_list_inner"):
            self.sidebar_list_inner.setStyleSheet(f"background-color: {self.BG_CARD}; border: none;")
        if hasattr(self, "empty_sidebar_lbl"):
            self.empty_sidebar_lbl.setStyleSheet(f"color: {self.FG_MUTED}; padding: 16px;")

        if hasattr(self, "card_views"):
            for view in self.card_views:
                kind = view.get("kind")
                obj_name = "summaryCard" if kind == "summary" else "agentCard"
                view["frame"].setStyleSheet(
                    f"QFrame#{obj_name} {{ background-color: {self.BG_CARD}; border: 1px solid {self.BORDER_COLOR}; border-radius: 4px; }}"
                )
                if "info_frame" in view:
                    view["info_frame"].setStyleSheet(f"background-color: {self.BG_INNER}; border-radius: 4px;")
                if "tot_frame" in view:
                    view["tot_frame"].setStyleSheet(
                        f"QFrame#totFrame {{ background-color: {self.BG_INNER}; border: 1px solid {self.BORDER_COLOR}; border-radius: 4px; }}"
                    )
                view["lbl_t1"].setStyleSheet(f"color: {self.FG_MUTED};")
                view["lbl_t2"].setStyleSheet(f"color: {self.FG_MUTED};")
                view["lbl_in"].setStyleSheet(f"color: {self.FG_PRIMARY};")
                view["lbl_cache"].setStyleSheet(f"color: {self.FG_PURPLE};")
                view["lbl_out"].setStyleSheet(f"color: {self.FG_YELLOW};")
                view["lbl_thought"].setStyleSheet(f"color: {self.FG_ACCENT};")

                if kind == "summary":
                    view["t_lbl"].setStyleSheet(f"color: {self.FG_ACCENT};")
                    view["date_lbl"].setStyleSheet(f"color: {self.FG_MUTED};")
                    view["counts_lbl"].setStyleSheet(f"color: {self.FG_PRIMARY};")
                    view["split_lbl"].setStyleSheet(f"color: {self.FG_MUTED};")
                    view["total_val"].setStyleSheet(f"color: {self.FG_ACCENT};")
                    view["cache_rate_val"].setStyleSheet(f"color: {self.FG_GREEN};")
                else:
                    view["back_active_lbl"].setStyleSheet(
                        f"color: {self.FG_GREEN}; background-color: {self.BG_INNER}; padding: 1px 6px; border-radius: 3px;"
                    )
                    view["session_lbl"].setStyleSheet(f"color: {self.FG_PRIMARY};")
                    view["meta_lbl"].setStyleSheet(f"color: {self.FG_MUTED};")
                    view["step_lbl"].setStyleSheet(f"color: {self.FG_PRIMARY};")
                    view["total_val"].setStyleSheet(f"color: {self.FG_GREEN};")
                    view["ctx_val"].setStyleSheet(f"color: {self.FG_ACCENT};")

                for cell_info in view.get("grid_cells", []):
                    cell_info["cell"].setStyleSheet(f"background-color: {self.BG_INNER}; border-radius: 4px;")
                    cell_info["title_lbl"].setStyleSheet(f"color: {self.FG_MUTED};")

        for row in self._sidebar_rows.values():
            row.apply_theme(palette)

        if hasattr(self, "splitter_handle"):
            self.splitter_handle.update()
        if hasattr(self, "resize_grip"):
            self.resize_grip.update()
        self.update()

    def apply_settings(self) -> None:
        """设置保存后的热重载入口：动态更新定时器周期、目录扫描 TTL、主题配色并立即重扫数据。"""
        poll_ms = int(self.config_mgr.get("poll_interval_ms", self.refresh_ms) or self.refresh_ms)
        self.refresh_ms = max(250, min(5000, poll_ms))
        if hasattr(self, "_timer"):
            self._timer.setInterval(self.refresh_ms)

        ttl_sec = float(self.config_mgr.get("scan_ttl_sec", 2.5) or 2.5)
        self.monitor.scan_ttl_sec = max(1.0, min(10.0, ttl_sec))

        self.apply_theme()
        self._sidebar_order_sig = None

        snap = self.monitor.collect_snapshot(
            target_session=self.target_session,
            target_project=self.target_project,
            force_scan=True,
        )
        self._apply_snapshot(snap)

    def open_settings(self) -> int:
        """实例化并弹出设置对话框。"""
        dlg = SettingsDialog(self, self.config_mgr)
        self._settings_dialog = dlg
        return int(dlg.exec())

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
        self._sidebar_ratio = 0.355
        screen = self._app.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        win_w, win_h = self.MIN_WIN_W, self.MIN_WIN_H
        pos_x = max(20, avail.right() - win_w - 28)
        pos_y = max(20, avail.top() + 36)
        self.setGeometry(pos_x, pos_y, win_w, win_h)
        self._apply_responsive_scale()

    def _has_sub_agents(self) -> bool:
        sub_agents = self.config_mgr.get("sub_agents", [])
        return bool(isinstance(sub_agents, list) and len(sub_agents) > 0)

    def _get_sub_agent_display_name(self, sub_sess: dict[str, Any] | None = None) -> str:
        if sub_sess:
            sess_agent_name = str(sub_sess.get("sub_agent_name") or sub_sess.get("agent_name") or "").strip()
            if sess_agent_name:
                return sess_agent_name
        sub_agents = self.config_mgr.get("sub_agents", [])
        if isinstance(sub_agents, list) and sub_agents:
            names: list[str] = []
            for item in sub_agents:
                if isinstance(item, dict):
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
        if self.pinned_main_file:
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
        dx = int(x_root) - self._split_start_x
        b_pad = self.body_lay.contentsMargins().left()
        splitter_w = self.splitter_handle.width() if hasattr(self, "splitter_handle") else 10
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
        act_settings = QAction("⚙ 设置...", self)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)

        act_refresh = QAction("立即刷新", self)
        act_refresh.triggered.connect(self._update_once)
        menu.addAction(act_refresh)

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
            view["status_lbl"].setStyleSheet(f"color: {self.FG_MUTED};")
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
            view["status_lbl"].setStyleSheet(f"color: {self.FG_YELLOW};")
        elif status == "running":
            est_tag = f" (+~{stream_est})" if stream_est > 0 else ""
            view["status_lbl"].setText(f"[执行中{est_tag}]")
            view["status_lbl"].setStyleSheet(f"color: {self.FG_GREEN};")
        elif status == "active":
            view["status_lbl"].setText("[刚更新]")
            view["status_lbl"].setStyleSheet(f"color: {self.FG_YELLOW};")
        else:
            view["status_lbl"].setText("[已完成]")
            view["status_lbl"].setStyleSheet(f"color: {self.FG_MUTED};")

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

    def _refresh_displayed_cards(self) -> None:
        """使用已缓存的会话数据立即刷新右侧卡片（供点击侧边栏切换时零延迟调用，不触发磁盘读取）。"""
        if not hasattr(self, "_last_snap") or not self._last_snap:
            return
        self._apply_snapshot(self._last_snap)

    def _apply_snapshot(self, snap: dict[str, Any]) -> None:
        self._last_snap = snap
        now_ts = snap["timestamp"]
        self._last_main_sessions = snap.get("all_main_sessions") or []
        self._last_sub_sessions = []

        main_sess = snap.get("main_session")
        today = snap.get("today_stats", {})

        today_main_tok = int(today.get("main_total_tokens", today.get("total_tokens", 0)))
        self._grand_total_compact_text = f"今日累计: {format_compact_tokens(today_main_tok)}"
        self._grand_total_hover_text = f"今日累计总 Token: {format_num(today_main_tok)}"

        self._grand_total_qlabel.setText(
            self._grand_total_hover_text if self._is_hovering_grand_total else self._grand_total_compact_text
        )

        is_main_pinned = False
        if self.pinned_main_file:
            for ms in self._last_main_sessions:
                if str(ms.get("file_path") or "") == self.pinned_main_file:
                    main_sess = ms
                    is_main_pinned = True
                    break

        self.pinned_sub_file = None
        if self.current_card_idx != 0:
            self.current_card_idx = 0
            self._deck_qwidget.setCurrentIndex(0)

        self.reset_active_btn.setVisible(is_main_pinned)

        # 同步左侧常驻历史会话列表
        self._refresh_sidebar_items()

        # 更新卡片 0：默认显示今日总用量；选中主 Agent 对话时显示该条对话的 Token 总量与各项细分指标
        v0 = self.card_views[0]
        main_valid = bool(snap.get("main_agent_valid", False))
        has_any_sessions = bool(self._last_main_sessions)

        if is_main_pinned and main_sess:
            raw_cid = str(main_sess.get("cid") or main_sess.get("session_id") or "-")
            short_cid = raw_cid[:8] if len(raw_cid) > 8 else raw_cid
            sess_title = _truncate_session_name(str(main_sess.get("session_name") or short_cid), 26)
            sess_model = str(main_sess.get("model") or "Antigravity")
            sess_steps = int(main_sess.get("steps", 0))
            sess_tools = int(main_sess.get("tool_calls_count", 0))
            sess_last_tool = str(main_sess.get("last_tool") or "无")
            st_ts = float(main_sess.get("start_time") or main_sess.get("last_updated") or now_ts)
            lu_ts = float(main_sess.get("last_updated") or now_ts)
            dur_s = format_duration(max(0.0, lu_ts - st_ts))

            s_in = int(main_sess.get("input_tokens", 0))
            s_cache = int(main_sess.get("cached_tokens", 0))
            s_out = int(main_sess.get("output_tokens", 0))
            s_thought = int(main_sess.get("thought_tokens", 0))
            s_tot = int(main_sess.get("total_tokens", 0))
            s_ctx = int(main_sess.get("context_tokens", 0))
            s_cache_rate = (s_cache * 100.0 / s_in) if s_in > 0 else 0.0

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

            t_in = int(today.get("input_tokens", 0))
            t_cache = int(today.get("cached_tokens", 0))
            t_out = int(today.get("output_tokens", 0))
            t_thought = int(today.get("thought_tokens", 0))
            t_tot = int(today.get("main_total_tokens", today.get("total_tokens", 0)))
            t_cache_rate = (t_cache * 100.0 / t_in) if t_in > 0 else 0.0

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
            else:
                v0["counts_lbl"].setText(
                    f"今日会话: {m_sess_c} 次 | 今日总步数: {m_step_c} 步"
                )
                v0["split_lbl"].setText(
                    f"Antigravity 今日累计: {format_num(m_tot_tok)} | 历史会话总数: {len(self._last_main_sessions)}"
                )
            v0["total_val"].setText(format_num(t_tot))
            v0["cache_rate_val"].setText(f"{t_cache_rate:.1f}%")
            v0["lbl_in"].setText(format_num(t_in))
            v0["lbl_cache"].setText(format_num(t_cache))
            v0["lbl_out"].setText(format_num(t_out))
            v0["lbl_thought"].setText(format_num(t_thought))

        if is_main_pinned:
            self._title_qlabel.setText("[当前选中对话]")
        else:
            self._title_qlabel.setText("[Token 总用量]")

        main_status = str(main_sess.get("status") or "idle") if main_sess else "idle"

        # 更新顶部指示灯与空闲自动关闭计时
        if main_status == "running":
            self.status_dot.setStyleSheet(f"color: {self.FG_GREEN};")
        elif main_status == "active":
            self.status_dot.setStyleSheet(f"color: {self.FG_YELLOW};")
        else:
            self.status_dot.setStyleSheet(f"color: {self.FG_MUTED};")

        if main_status in ("running", "active"):
            self._seen_running = True
            self._idle_since = None
        elif self._seen_running and self._idle_since is None:
            self._idle_since = now_ts

        self._adjust_window_geometry()

        if self.auto_close_idle_sec > 0 and self._idle_since is not None:
            if (now_ts - self._idle_since) >= self.auto_close_idle_sec:
                self.close()

    def _update_once(self) -> None:
        """同步采集并更新一次界面（用于首次启动、手动立即刷新与 --self-test）。"""
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
        self._update_once()
        if duration_sec > 0:
            QTimer.singleShot(int(duration_sec * 1000), self.close)
        self._timer.start()
        self._app.exec()

    def close(self) -> bool:
        self._in_mainloop = False
        if hasattr(self, "_timer"):
            self._timer.stop()
        res = super().close()
        if self._app:
            self._app.quit()
        return res


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity 实时 Token 监控置顶悬浮窗 (PySide6)")
    parser.add_argument("-s", "--session", help="指定优先锁定的会话 ID（前缀或完整 ID）")
    parser.add_argument("-p", "--project", help="指定优先过滤的项目目录名")
    parser.add_argument("-i", "--interval", type=int, default=1000, help="界面刷新间隔毫秒数（默认 1000ms）")
    parser.add_argument(
        "--card",
        type=int,
        choices=[0],
        default=0,
        help="初始展示的卡片索引：0=Token 总用量",
    )
    parser.add_argument(
        "--auto-close-idle",
        type=float,
        default=0.0,
        help="当监听的会话从运行态变为完成态后，等待多少秒自动关闭窗口（默认 0 常驻不自动关）",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="窗口运行指定秒数后自动退出（用于自动化测试或定时展示，默认 0 常驻）",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="运行非阻塞自检：验证 Antigravity JSONL 解析、界面组件隔离、DWM 圆角/Mica 属性设置，打印 JSON 报告后退出",
    )

    args = parser.parse_args()
    config_mgr = ConfigManager()
    monitor = TokenMonitorCache(config_mgr=config_mgr)

    if args.self_test:
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
            )
            assert dirty_filtered_ok, "config_test: 脏数据过滤与热重载断言失败"

            tmp_cfg_path.write_text("{broken json", encoding="utf-8")
            corrupted_fallback_ok = tmp_cfg.load()["sub_agents"] == []
            assert corrupted_fallback_ok, "config_test: 损坏 JSON 回退默认配置断言失败"

            config_test = {
                "passed": True,
                "default_loaded_ok": default_loaded_ok,
                "custom_save_ok": custom_save_ok,
                "dirty_filtered_ok": dirty_filtered_ok,
                "corrupted_fallback_ok": corrupted_fallback_ok,
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

            # 3. theme_and_settings_test：测试 SettingsDialog 子 Agent 区域隐藏、主题切换（4 套预设）、拾色器色值应用、QTimer 周期动态生效、侧边栏过滤栏常驻隐藏与纯主 Agent 卡片模式断言
            ui_cfg = ConfigManager(config_path=tmp_dir_path / "ui_test_config.json")
            hud.config_mgr = ui_cfg
            hud.monitor.config_mgr = ui_cfg
            hud.apply_settings()
            filter_hidden_always = not hud.filter_bar.isVisible()
            single_card_only_ok = len(hud.card_views) == 1
            assert filter_hidden_always, "theme_and_settings_test: 侧边栏过滤栏彻底隐藏断言失败"
            assert single_card_only_ok, "theme_and_settings_test: 仅保留第 0 张主卡片断言失败"

            # 验证纯主 Agent 模式下，点击标题栏翻页被禁用，始终保持在卡片 0，且不显示切换提示
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
            sub_section_hidden_ok = (
                hasattr(dlg, "sub_section_widget")
                and not dlg.sub_section_widget.isVisible()
                and not dlg.sub_table.isVisible()
                and not dlg.add_sub_btn.isVisible()
                and not dlg.remove_sub_btn.isVisible()
            )
            assert sub_section_hidden_ok, "theme_and_settings_test: SettingsDialog 隐藏子 Agent 管理区域断言失败"
            dlg.reject()
            QApplication.processEvents()
            cancel_keeps_main_alive_ok = (
                hud.isVisible()
                and hud._in_mainloop is False
                and not hud._app.quitOnLastWindowClosed()
            )
            assert cancel_keeps_main_alive_ok, "theme_and_settings_test: 取消设置弹窗后主窗口保持运行断言失败"

            dlg = SettingsDialog(hud, ui_cfg)
            dlg.show()
            QApplication.processEvents()
            presets_checked: dict[str, bool] = {}
            for preset_name, preset_colors in THEME_PRESETS.items():
                dlg.preset_combo.setCurrentText(preset_name)
                dlg._on_preset_changed(preset_name)
                matched = all(
                    dlg.color_edits[k].text().lower() == preset_colors[k].lower()
                    for k, _ in dlg.COLOR_FIELD_SPECS
                )
                presets_checked[preset_name] = matched
                assert matched, f"theme_and_settings_test: 预设 {preset_name} 联动断言失败"
            all_4_presets_ok = len(presets_checked) == 4 and all(presets_checked.values())
            assert all_4_presets_ok, "theme_and_settings_test: 4 套预设切换断言失败"

            # 切回 Cyberpunk Dark 并测试 QColorDialog 拾色器色值应用
            dlg.preset_combo.setCurrentText("Cyberpunk Dark")
            dlg._on_preset_changed("Cyberpunk Dark")
            expected_bg = THEME_PRESETS["Cyberpunk Dark"]["bg_color"]
            picked_accent = "#ff8800"
            with patch.object(QColorDialog, "getColor", return_value=QColor(picked_accent)):
                dlg._on_pick_color("accent_color")
            color_picker_ok = (
                dlg.color_edits["accent_color"].text().lower() == picked_accent
                and picked_accent in dlg.color_swatches["accent_color"].styleSheet().lower()
            )
            assert color_picker_ok, "theme_and_settings_test: 拾色器色值应用断言失败"

            dlg.poll_spin.setValue(750)
            dlg.ttl_spin.setValue(4.0)
            dlg.save_and_apply()
            QApplication.processEvents()

            save_keeps_main_alive_ok = hud.isVisible() and not hud._app.quitOnLastWindowClosed()
            assert save_keeps_main_alive_ok, "theme_and_settings_test: 保存设置弹窗后主窗口保持运行断言失败"

            timer_updated_ok = hud._timer.interval() == 750 and abs(hud.monitor.scan_ttl_sec - 4.0) < 1e-6
            theme_updated_ok = (
                hud.BG_MAIN.lower() == expected_bg.lower()
                and hud.FG_ACCENT.lower() == picked_accent.lower()
            )
            assert timer_updated_ok, "theme_and_settings_test: QTimer 周期与 TTL 动态生效断言失败"
            assert theme_updated_ok, "theme_and_settings_test: 主题与拾色器颜色实时热重载断言失败"

            # 测试选中某条对话时主卡片区域显示该对话的 Token 总量与细分指标，且标题栏右侧常驻显示今日累计总 Token
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
            "sub_section_hidden_ok": sub_section_hidden_ok,
            "filter_hidden_always": filter_hidden_always,
            "single_card_only_ok": single_card_only_ok,
            "no_sub_flip_disabled_ok": no_sub_flip_disabled_ok,
            "selected_session_display_ok": selected_session_display_ok,
            "cancel_keeps_main_alive_ok": cancel_keeps_main_alive_ok,
            "save_keeps_main_alive_ok": save_keeps_main_alive_ok,
            "all_4_presets_ok": all_4_presets_ok,
            "presets_checked": presets_checked,
            "color_picker_ok": color_picker_ok,
            "timer_updated_ok": timer_updated_ok,
            "theme_updated_ok": theme_updated_ok,
            "empty_prompt_ok": empty_prompt_ok,
        }

        all_main = snap.pop("all_main_sessions", [])
        all_sub = snap.pop("all_sub_sessions", [])
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
        hud.close()
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        sys.exit(0)

    if not acquire_single_instance_mutex():
        bring_existing_hud_to_front()
        sys.exit(0)

    bind_interactive_desktop()
    enable_dpi_awareness()
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
