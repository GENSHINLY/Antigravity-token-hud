# -*- coding: utf-8 -*-
"""
hud_core.config - 配置管理中心、默认配置与主题预设。
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from .pricing import (
    DEFAULT_MODEL_PRICING_PER_1M,
    DEFAULT_USD_TO_CNY_RATE,
    TokenPricingEngine,
    get_default_pricing_config,
)

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
    "auto_update_pricing": True,
    "proxy": "",
    "usd_to_cny_rate": DEFAULT_USD_TO_CNY_RATE,
    "pricing": get_default_pricing_config(),
    "theme": {
        "preset": "Gruvbox Material",
        "bg_color": "#141617",
        "fg_color": "#D4BE98",
        "accent_color": "#E78A4E",
        "card_bg_color": "#1D2021",
        "border_color": "#3C3836",
        "opacity": 0.94,
        "bg_opacity": 0.65,
        "backdrop": "mica",
    },
}

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class ConfigManager:
    """配置中心：负责管理 HUD 路径、轮询频率、子 Agent 列表、模型费率与主题配置，支持线程安全读写与脏数据校验。"""

    def __init__(self, config_path: Path | str | None = None):
        self._lock = threading.RLock()
        self._explicit_path = Path(config_path).expanduser() if config_path else None
        self.local_config_path = Path(__file__).resolve().parent.parent / "config.json"
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
        if name_str and name_str.lower() != "subagent":
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
    def normalize_config(cls, raw: Any) -> dict[str, Any]:
        """校验并规范化配置字典，保证所有默认字段、模型定价表与汇率均正确合并。"""
        clean = cls._deep_copy_default()
        if not isinstance(raw, dict):
            return clean

        main_path = raw.get("main_agent_path")
        if isinstance(main_path, str):
            clean["main_agent_path"] = main_path.strip()

        raw_subs = raw.get("sub_agents")
        if isinstance(raw_subs, list):
            valid_subs: list[dict[str, Any]] = []
            seen_ids: set[str] = {"main"}
            for idx, item in enumerate(raw_subs):
                if not isinstance(item, dict):
                    continue
                path_str = str(item.get("path") or "").strip()
                name = cls._resolve_sub_agent_display_name(item.get("name"), path_str)
                raw_id = str(item.get("id") or "").strip()
                agent_id = raw_id if (raw_id and raw_id not in seen_ids) else f"sub_{idx}"
                while agent_id in seen_ids:
                    idx += 1
                    agent_id = f"sub_{idx}"
                seen_ids.add(agent_id)
                parser_type = str(item.get("parser_type") or "gemini_cli_session").strip()
                if parser_type not in (
                    "antigravity_transcript",
                    "gemini_cli_session",
                    "claude_code_session",
                ):
                    parser_type = "gemini_cli_session"
                live_state_path = str(item.get("live_state_path") or "").strip()
                raw_enabled = item.get("enabled", True)
                if isinstance(raw_enabled, str):
                    enabled = raw_enabled.strip().lower() not in ("false", "0", "no", "off")
                else:
                    enabled = bool(raw_enabled)
                valid_subs.append(
                    {
                        "id": agent_id,
                        "enabled": enabled,
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

        raw_auto_update = raw.get("auto_update_pricing", True)
        if isinstance(raw_auto_update, str):
            clean["auto_update_pricing"] = raw_auto_update.strip().lower() not in (
                "false",
                "0",
                "no",
                "off",
            )
        else:
            clean["auto_update_pricing"] = bool(raw_auto_update)

        raw_proxy = raw.get("proxy", "")
        if isinstance(raw_proxy, str):
            clean["proxy"] = raw_proxy.strip()
        else:
            clean["proxy"] = ""

        raw_rate = raw.get("usd_to_cny_rate", DEFAULT_USD_TO_CNY_RATE)
        if not isinstance(raw_rate, bool):
            try:
                rate_val = float(raw_rate)
                if (
                    rate_val == rate_val
                    and rate_val not in (float("inf"), float("-inf"))
                    and rate_val > 0.0
                ):
                    clean["usd_to_cny_rate"] = round(max(0.01, min(1000.0, rate_val)), 4)
            except (TypeError, ValueError):
                pass

        base_pricing = get_default_pricing_config()
        raw_pricing = raw.get("pricing")
        if isinstance(raw_pricing, dict):
            for raw_model_key, raw_rates in raw_pricing.items():
                if not isinstance(raw_rates, dict):
                    continue
                key_str = str(raw_model_key or "").strip()
                if not key_str:
                    continue
                canon_key = TokenPricingEngine.canonicalize_model_key(key_str)
                fallback_rates = dict(
                    base_pricing.get(
                        canon_key,
                        DEFAULT_MODEL_PRICING_PER_1M.get(
                            canon_key, DEFAULT_MODEL_PRICING_PER_1M["default"]
                        ),
                    )
                )
                for field in ("input", "cached", "output", "thought"):
                    if field in raw_rates and not isinstance(raw_rates[field], bool):
                        try:
                            f_val = float(raw_rates[field])
                            if (
                                f_val == f_val
                                and f_val not in (float("inf"), float("-inf"))
                                and f_val >= 0.0
                            ):
                                fallback_rates[field] = round(min(100000.0, f_val), 6)
                        except (TypeError, ValueError):
                            pass
                if (
                    "thought" not in raw_rates
                    and "output" in raw_rates
                    and canon_key not in DEFAULT_MODEL_PRICING_PER_1M
                ):
                    fallback_rates["thought"] = fallback_rates["output"]
                base_pricing[canon_key] = fallback_rates
        clean["pricing"] = base_pricing

        raw_theme = raw.get("theme")
        if isinstance(raw_theme, dict):
            preset_name = str(raw_theme.get("preset") or clean["theme"]["preset"]).strip()
            base_theme: dict[str, Any] = dict(
                THEME_PRESETS.get(preset_name, THEME_PRESETS["Gruvbox Material"])
            )
            base_theme["preset"] = preset_name if preset_name else "Gruvbox Material"
            for color_key in ("bg_color", "fg_color", "accent_color", "card_bg_color", "border_color"):
                val = raw_theme.get(color_key)
                if isinstance(val, str) and _HEX_COLOR_RE.match(val.strip()):
                    base_theme[color_key] = val.strip()
            raw_opacity = raw_theme.get("opacity", clean["theme"]["opacity"])
            if isinstance(raw_opacity, bool):
                opacity_val = 0.94
            else:
                try:
                    opacity_val = float(raw_opacity)
                    if opacity_val != opacity_val or opacity_val in (float("inf"), float("-inf")):
                        opacity_val = 0.94
                except (TypeError, ValueError):
                    opacity_val = 0.94
            base_theme["opacity"] = round(max(0.50, min(1.00, opacity_val)), 2)

            raw_backdrop = str(raw_theme.get("backdrop") or clean["theme"]["backdrop"]).strip().lower()
            if raw_backdrop not in ("mica", "acrylic", "mica_alt", "none"):
                raw_backdrop = "mica"
            base_theme["backdrop"] = raw_backdrop

            try:
                raw_bg_op = raw_theme.get("bg_opacity", 0.65)
                if isinstance(raw_bg_op, bool):
                    bg_op = 0.65
                else:
                    bg_op = float(raw_bg_op)
                    if bg_op != bg_op or bg_op in (float("inf"), float("-inf")):
                        bg_op = 0.65
                    bg_op = round(max(0.05, min(1.0, bg_op)), 2)
            except Exception:
                bg_op = 0.65
            base_theme["bg_opacity"] = bg_op
            clean["theme"] = base_theme

        for k, v in raw.items():
            if k not in clean:
                clean[k] = v

        return clean

    @classmethod
    def _sanitize_config(cls, raw: Any) -> dict[str, Any]:
        return cls.normalize_config(raw)

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

    def set(self, key: str, value: Any, persist: bool = True) -> None:
        """线程安全设置配置项并校验，默认立即持久化，高频预览场景可传 persist=False 仅更新内存。"""
        with self._lock:
            candidate = dict(self._data)
            candidate[key] = value
            self._data = self._sanitize_config(candidate)
            if persist:
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
