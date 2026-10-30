# -*- coding: utf-8 -*-
"""
hud_core.win32_dwm - 进程单实例互斥体、DPI 感知、DWM 客户区扩展、Mica/Acrylic 与暗色属性。
"""
from __future__ import annotations

import ctypes
import sys
from typing import Any

if sys.platform == "win32":
    from ctypes import wintypes

MUTEX_NAME = "Global" + "AntigravityTokenHUDMutex"
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
            handle = k32.CreateMutexW(None, False, "Local" + "AntigravityTokenHUDMutex")
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


def apply_win11_mica_and_rounded_corners(
    window: Any, backdrop_type: str = "mica"
) -> dict[str, Any]:
    """
    对顶层窗口应用 Windows 11 原生暗色模式、圆角与 DWM 背景材质属性：
    - DwmExtendFrameIntoClientArea: MARGINS(-1, -1, -1, -1) 扩展至全客户区
    - DWMWA_USE_IMMERSIVE_DARK_MODE (20) = 1
    - DWMWA_WINDOW_CORNER_PREFERENCE (33) = 2 (DWMWCP_ROUND)
    - DWMWA_SYSTEMBACKDROP_TYPE (38):
      - "none" -> 1 (DWMSBT_NONE)
      - "mica" -> 2 (DWMSBT_MAINWINDOW)
      - "acrylic" -> 3 (DWMSBT_TRANSIENTWINDOW)
      - "mica_alt" -> 4 (DWMSBT_TABBEDWINDOW)
    """
    backdrop_map = {
        "none": 1,
        "mica": 2,
        "acrylic": 3,
        "mica_alt": 4,
    }
    norm_backdrop = str(backdrop_type or "mica").strip().lower()
    if norm_backdrop not in backdrop_map:
        norm_backdrop = "mica"
    backdrop_val = backdrop_map[norm_backdrop]

    result: dict[str, Any] = {
        "hwnd": 0,
        "dark_mode": False,
        "rounded": False,
        "mica": False,
        "extended_frame": False,
        "backdrop": norm_backdrop,
    }
    if sys.platform != "win32":
        return result
    try:
        class _MARGINS(ctypes.Structure):
            _fields_ = [
                ("cxLeftWidth", ctypes.c_int),
                ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int),
                ("cyBottomHeight", ctypes.c_int),
            ]

        wid = int(window.winId()) if hasattr(window, "winId") else int(window)
        top_hwnd = resolve_top_level_hwnd(window)
        dwmapi = ctypes.windll.dwmapi

        hwnd_candidates = []
        if top_hwnd:
            hwnd_candidates.append(top_hwnd)
        if wid and wid not in hwnd_candidates:
            hwnd_candidates.append(wid)

        margins = _MARGINS(-1, -1, -1, -1)
        for hwnd in hwnd_candidates:
            result["hwnd"] = hwnd
            # 0. 将 DWM 玻璃边框扩展至全客户区
            hr_ext = dwmapi.DwmExtendFrameIntoClientArea(
                hwnd, ctypes.byref(margins)
            )
            if hr_ext == 0:
                result["extended_frame"] = True

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

            # 3. 系统背景材质 (DWMWA_SYSTEMBACKDROP_TYPE = 38)
            hr_backdrop = dwmapi.DwmSetWindowAttribute(
                hwnd, 38, ctypes.byref(ctypes.c_int(backdrop_val)), 4
            )
            if hr_backdrop != 0 and norm_backdrop == "mica":
                hr_backdrop = dwmapi.DwmSetWindowAttribute(
                    hwnd, 38, ctypes.byref(ctypes.c_int(4)), 4
                )
            if hr_backdrop == 0:
                result["mica"] = norm_backdrop != "none"
                result["backdrop"] = norm_backdrop
    except Exception:
        pass
    return result
