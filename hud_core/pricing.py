# -*- coding: utf-8 -*-
"""
hud_core.pricing - 模型费率定义、Token 成本折算引擎与在线汇率/价格表同步层。
"""
from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_USD_TO_CNY_RATE: float = 7.25
MIN_VALID_USD_TO_CNY_RATE: float = 5.0
MAX_VALID_USD_TO_CNY_RATE: float = 10.0

USD_RATE_ENDPOINTS: tuple[str, ...] = (
    "https://open.er-api.com/v6/latest/USD",
    "https://api.exchangerate-api.com/v4/latest/USD",
)

REMOTE_PRICING_ENDPOINTS: tuple[str, ...] = (
    "https://raw.githubusercontent.com/GENSHINLY/Antigravity-token-hud/main/pricing.remote.json",
    "https://cdn.jsdelivr.net/gh/GENSHINLY/Antigravity-token-hud@main/pricing.remote.json",
)

# 官方预设模型列表：(标准配置键名, 界面显示名称)
MODEL_PRICING_PRESETS: list[tuple[str, str]] = [
    ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    ("gemini-3.8-pro", "Gemini 3.8 Pro"),
    ("gemini-4-argon", "Gemini 4 Argon"),
    ("gemini-3.1-pro", "Gemini 3.1 Pro"),
    ("gemini-3.1-flash", "Gemini 3.1 Flash"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("gemini-2.0-flash", "Gemini 2.0 Flash"),
    ("gemini-1.5-pro", "Gemini 1.5 Pro"),
    ("gemini-1.5-flash", "Gemini 1.5 Flash"),
    ("claude-opus-4-6", "Claude Opus 4.6"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-3.7-sonnet", "Claude 3.7 Sonnet"),
    ("claude-3.5-sonnet", "Claude 3.5 Sonnet"),
    ("claude-3.5-haiku", "Claude 3.5 Haiku"),
    ("claude-3-opus", "Claude 3 Opus"),
    ("gpt-4o", "GPT-4o"),
    ("gpt-4o-mini", "GPT-4o mini"),
    ("o1", "OpenAI o1"),
    ("o3-mini", "OpenAI o3-mini"),
    ("deepseek-chat", "DeepSeek-V3 (deepseek-chat)"),
    ("deepseek-reasoner", "DeepSeek-R1 (deepseek-reasoner)"),
    ("default", "default"),
]

# 每百万 Token (1M Tokens) 美元参考费率表：
# - input: 未命中缓存的输入单价 ($ / 1M tokens)
# - cached: 缓存读取单价 ($ / 1M tokens)
# - output: 输出单价 ($ / 1M tokens)
# - thought: 思考/推理 Token 单价 ($ / 1M tokens，默认与 output 一致)
DEFAULT_MODEL_PRICING_PER_1M: dict[str, dict[str, float]] = {
    "gemini-3.8-flash": {
        "input": 0.15,
        "cached": 0.0375,
        "output": 0.60,
        "thought": 3.50,
    },
    "gemini-3.8-pro": {
        "input": 1.25,
        "cached": 0.3125,
        "output": 10.00,
        "thought": 10.00,
    },
    "gemini-4-argon": {
        "input": 1.25,
        "cached": 0.3125,
        "output": 10.00,
        "thought": 10.00,
    },
    "gemini-3.1-pro": {
        "input": 1.25,
        "cached": 0.3125,
        "output": 10.00,
        "thought": 10.00,
    },
    "gemini-3.1-flash": {
        "input": 0.15,
        "cached": 0.0375,
        "output": 0.60,
        "thought": 3.50,
    },
    "gemini-2.5-pro": {
        "input": 1.25,
        "cached": 0.125,
        "output": 10.00,
        "thought": 10.00,
    },
    "gemini-2.5-flash": {
        "input": 0.30,
        "cached": 0.03,
        "output": 2.50,
        "thought": 2.50,
    },
    "gemini-2.0-flash": {
        "input": 0.10,
        "cached": 0.025,
        "output": 0.40,
        "thought": 0.40,
    },
    "gemini-1.5-pro": {
        "input": 1.25,
        "cached": 0.3125,
        "output": 5.00,
        "thought": 5.00,
    },
    "gemini-1.5-flash": {
        "input": 0.075,
        "cached": 0.01875,
        "output": 0.30,
        "thought": 0.30,
    },
    "claude-opus-4-6": {
        "input": 15.00,
        "cached": 1.50,
        "output": 75.00,
        "thought": 75.00,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "cached": 0.30,
        "output": 15.00,
        "thought": 15.00,
    },
    "claude-3-7-sonnet": {
        "input": 3.00,
        "cached": 0.30,
        "output": 15.00,
        "thought": 15.00,
    },
    "claude-3.7-sonnet": {
        "input": 3.00,
        "cached": 0.30,
        "output": 15.00,
        "thought": 15.00,
    },
    "claude-3-5-sonnet": {
        "input": 3.00,
        "cached": 0.30,
        "output": 15.00,
        "thought": 15.00,
    },
    "claude-3.5-sonnet": {
        "input": 3.00,
        "cached": 0.30,
        "output": 15.00,
        "thought": 15.00,
    },
    "claude-3-5-haiku": {
        "input": 0.80,
        "cached": 0.08,
        "output": 4.00,
        "thought": 4.00,
    },
    "claude-3.5-haiku": {
        "input": 0.80,
        "cached": 0.08,
        "output": 4.00,
        "thought": 4.00,
    },
    "claude-3-opus": {
        "input": 15.00,
        "cached": 1.50,
        "output": 75.00,
        "thought": 75.00,
    },
    "gpt-4o": {
        "input": 2.50,
        "cached": 1.25,
        "output": 10.00,
        "thought": 10.00,
    },
    "gpt-4o-mini": {
        "input": 0.15,
        "cached": 0.075,
        "output": 0.60,
        "thought": 0.60,
    },
    "o1": {
        "input": 15.00,
        "cached": 7.50,
        "output": 60.00,
        "thought": 60.00,
    },
    "o3-mini": {
        "input": 1.10,
        "cached": 0.55,
        "output": 4.40,
        "thought": 4.40,
    },
    "deepseek-chat": {
        "input": 0.27,
        "cached": 0.07,
        "output": 1.10,
        "thought": 1.10,
    },
    "deepseek-v3": {
        "input": 0.27,
        "cached": 0.07,
        "output": 1.10,
        "thought": 1.10,
    },
    "deepseek-reasoner": {
        "input": 0.55,
        "cached": 0.14,
        "output": 2.19,
        "thought": 2.19,
    },
    "deepseek-r1": {
        "input": 0.55,
        "cached": 0.14,
        "output": 2.19,
        "thought": 2.19,
    },
    "default": {
        "input": 3.00,
        "cached": 0.30,
        "output": 15.00,
        "thought": 15.00,
    },
}

_MODEL_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "claude-3.7-sonnet": ("claude-3.7-sonnet", "claude-3-7-sonnet"),
    "claude-3-7-sonnet": ("claude-3.7-sonnet", "claude-3-7-sonnet"),
    "claude-3.5-sonnet": ("claude-3.5-sonnet", "claude-3-5-sonnet"),
    "claude-3-5-sonnet": ("claude-3.5-sonnet", "claude-3-5-sonnet"),
    "claude-3.5-haiku": ("claude-3.5-haiku", "claude-3-5-haiku"),
    "claude-3-5-haiku": ("claude-3.5-haiku", "claude-3-5-haiku"),
    "claude-3-opus": ("claude-3-opus", "claude-3.0-opus"),
    "claude-3.0-opus": ("claude-3-opus", "claude-3.0-opus"),
    "claude-opus-4.6": ("claude-opus-4-6", "claude-opus-4.6"),
    "claude-opus-4-6": ("claude-opus-4-6", "claude-opus-4.6"),
    "claude-sonnet-4.6": ("claude-sonnet-4-6", "claude-sonnet-4.6"),
    "claude-sonnet-4-6": ("claude-sonnet-4-6", "claude-sonnet-4.6"),
    "gpt-4o": ("gpt-4o", "chatgpt-4o-latest"),
    "chatgpt-4o-latest": ("gpt-4o", "chatgpt-4o-latest"),
    "gpt-4o-mini": ("gpt-4o-mini", "gpt4o-mini"),
    "gpt4o-mini": ("gpt-4o-mini", "gpt4o-mini"),
    "o1": ("o1", "openai-o1", "o1-preview"),
    "openai-o1": ("o1", "openai-o1", "o1-preview"),
    "o1-preview": ("o1", "openai-o1", "o1-preview"),
    "o3-mini": ("o3-mini", "openai-o3-mini"),
    "openai-o3-mini": ("o3-mini", "openai-o3-mini"),
    "deepseek-chat": ("deepseek-chat", "deepseek-v3"),
    "deepseek-v3": ("deepseek-chat", "deepseek-v3"),
    "deepseek-reasoner": ("deepseek-reasoner", "deepseek-r1"),
    "deepseek-r1": ("deepseek-reasoner", "deepseek-r1"),
}


def get_default_pricing_config() -> dict[str, dict[str, float]]:
    """返回官方预设模型默认费率字典的深拷贝（供 DEFAULT_CONFIG 与重置按钮使用）。"""
    return {
        model_key: dict(
            DEFAULT_MODEL_PRICING_PER_1M.get(
                model_key, DEFAULT_MODEL_PRICING_PER_1M["default"]
            )
        )
        for model_key, _ in MODEL_PRICING_PRESETS
    }


def format_cost_usd(usd: float) -> str:
    """将美元成本格式化为紧凑可读文本（如 $0.15, $0.0042, $12.34）。"""
    val = max(0.0, float(usd or 0.0))
    if val == 0.0:
        return "$0.00"
    if val < 0.01:
        return f"${val:.4f}"
    return f"${val:.2f}"


class TokenPricingEngine:
    """Token 成本折算引擎：根据模型名称与各维度 Token 计数计算 USD / CNY 预估成本。"""

    def __init__(
        self,
        pricing_table: dict[str, dict[str, float]] | None = None,
        usd_to_cny: float = DEFAULT_USD_TO_CNY_RATE,
    ):
        self.pricing_table: dict[str, dict[str, float]] = {}
        self.usd_to_cny: float = DEFAULT_USD_TO_CNY_RATE
        self.update_pricing(pricing_table=pricing_table, usd_to_cny=usd_to_cny)

    @staticmethod
    def _normalize_model_key(name: str) -> str:
        """统一模型名格式（小写、空格转连字符），便于兼容 'Claude 3.7 Sonnet' 与 'claude-3-7-sonnet' 等写法。"""
        s = str(name or "").strip().lower()
        s = "-".join(s.replace("_", "-").split())
        return s

    @classmethod
    def canonicalize_model_key(cls, name: str) -> str:
        """将模型名称或显示名称转为标准配置键名（如 'Claude 3.7 Sonnet' -> 'claude-3.7-sonnet'）。"""
        norm = cls._normalize_model_key(name)
        if not norm:
            return "default"
        if norm in _MODEL_KEY_ALIASES:
            return _MODEL_KEY_ALIASES[norm][0]
        for preset_key, display_name in MODEL_PRICING_PRESETS:
            if norm == preset_key or norm == cls._normalize_model_key(display_name):
                return preset_key
            if norm.replace(".", "-") == preset_key.replace(".", "-"):
                return preset_key
        # 针对常见变体模糊匹配为标准键名
        if "deepseek" in norm and ("r1" in norm or "reasoner" in norm):
            return "deepseek-reasoner"
        if "deepseek" in norm and ("v3" in norm or "chat" in norm):
            return "deepseek-chat"
        if "gpt-4o-mini" in norm or "gpt4o-mini" in norm:
            return "gpt-4o-mini"
        if "gpt-4o" in norm or "gpt4o" in norm or "chatgpt-4o" in norm:
            return "gpt-4o"
        if "o3-mini" in norm:
            return "o3-mini"
        if norm in ("o1", "openai-o1") or norm.startswith("o1-"):
            return "o1"
        if "claude-3-opus" in norm or "claude-3.0-opus" in norm:
            return "claude-3-opus"
        return norm

    def update_pricing(
        self,
        pricing_table: dict[str, dict[str, float]] | None = None,
        usd_to_cny: float | None = None,
    ) -> None:
        """动态重置并合并自定义模型费率表与 USD->CNY 汇率。"""
        merged: dict[str, dict[str, float]] = {
            k: dict(v) for k, v in DEFAULT_MODEL_PRICING_PER_1M.items()
        }
        if isinstance(pricing_table, dict):
            for raw_k, raw_v in pricing_table.items():
                if not isinstance(raw_v, dict):
                    continue
                norm_k = self._normalize_model_key(str(raw_k))
                canon_k = self.canonicalize_model_key(str(raw_k))
                base_rates = dict(
                    merged.get(
                        canon_k,
                        merged.get(norm_k, DEFAULT_MODEL_PRICING_PER_1M["default"]),
                    )
                )
                for field in ("input", "cached", "output", "thought"):
                    if field in raw_v and not isinstance(raw_v[field], bool):
                        try:
                            val = float(raw_v[field])
                            if val == val and val not in (float("inf"), float("-inf")) and val >= 0.0:
                                base_rates[field] = round(val, 6)
                        except (TypeError, ValueError):
                            pass
                if "thought" not in raw_v and "output" in raw_v and canon_k not in DEFAULT_MODEL_PRICING_PER_1M:
                    base_rates["thought"] = base_rates["output"]

                target_keys = {str(raw_k).strip(), norm_k, canon_k, norm_k.replace(".", "-")}
                for alias in _MODEL_KEY_ALIASES.get(norm_k, ()):
                    target_keys.add(alias)
                for alias in _MODEL_KEY_ALIASES.get(canon_k, ()):
                    target_keys.add(alias)
                for tk in target_keys:
                    if tk:
                        merged[tk] = dict(base_rates)

        self.pricing_table = merged
        if usd_to_cny is not None and not isinstance(usd_to_cny, bool):
            try:
                rate_val = float(usd_to_cny)
                if rate_val == rate_val and rate_val not in (float("inf"), float("-inf")) and rate_val > 0:
                    self.usd_to_cny = rate_val
            except (TypeError, ValueError):
                pass
        elif self.usd_to_cny <= 0:
            self.usd_to_cny = DEFAULT_USD_TO_CNY_RATE

    def resolve_model_rates(self, model_name: str = "") -> dict[str, float]:
        """按模型名称（精确匹配、点号/横杠归一化匹配或子串匹配）解析每百万 Token 费率。"""
        norm = self._normalize_model_key(model_name)
        if not norm:
            return self.pricing_table.get("default", DEFAULT_MODEL_PRICING_PER_1M["default"])

        if norm in self.pricing_table:
            return self.pricing_table[norm]

        canon = self.canonicalize_model_key(model_name)
        if canon in self.pricing_table:
            return self.pricing_table[canon]

        norm_dash = norm.replace(".", "-")
        if norm_dash in self.pricing_table:
            return self.pricing_table[norm_dash]

        # 按键名长度降序匹配，避免短键名（如 gpt-4o 或 o1）抢先匹配长键名（如 gpt-4o-mini）
        sorted_items = sorted(
            ((k, v) for k, v in self.pricing_table.items() if k != "default"),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for key, rates in sorted_items:
            key_norm = self._normalize_model_key(key)
            key_dash = key_norm.replace(".", "-")
            if len(key_norm) <= 2:
                continue
            if key_norm in norm or key_dash in norm_dash:
                return rates

        # 针对常见系列关键词回退
        if "deepseek" in norm:
            if "r1" in norm or "reasoner" in norm:
                return self.pricing_table.get(
                    "deepseek-reasoner", DEFAULT_MODEL_PRICING_PER_1M["deepseek-reasoner"]
                )
            return self.pricing_table.get(
                "deepseek-chat", DEFAULT_MODEL_PRICING_PER_1M["deepseek-chat"]
            )
        if "gpt-4o-mini" in norm or "4o-mini" in norm:
            return self.pricing_table.get(
                "gpt-4o-mini", DEFAULT_MODEL_PRICING_PER_1M["gpt-4o-mini"]
            )
        if "gpt-4o" in norm or "4o" in norm:
            return self.pricing_table.get(
                "gpt-4o", DEFAULT_MODEL_PRICING_PER_1M["gpt-4o"]
            )
        if "o3-mini" in norm or "o3" in norm:
            return self.pricing_table.get(
                "o3-mini", DEFAULT_MODEL_PRICING_PER_1M["o3-mini"]
            )
        if norm == "o1" or norm.startswith("o1-") or "-o1" in norm:
            return self.pricing_table.get(
                "o1", DEFAULT_MODEL_PRICING_PER_1M["o1"]
            )
        if "flash" in norm:
            return self.pricing_table.get(
                "gemini-3.8-flash", DEFAULT_MODEL_PRICING_PER_1M["gemini-3.8-flash"]
            )
        if "argon" in norm:
            return self.pricing_table.get(
                "gemini-4-argon", DEFAULT_MODEL_PRICING_PER_1M["gemini-4-argon"]
            )
        if "gemini" in norm and "pro" in norm:
            return self.pricing_table.get(
                "gemini-3.8-pro", DEFAULT_MODEL_PRICING_PER_1M["gemini-3.8-pro"]
            )
        if "haiku" in norm:
            return self.pricing_table.get(
                "claude-3-5-haiku", DEFAULT_MODEL_PRICING_PER_1M["claude-3-5-haiku"]
            )
        if "claude-3-opus" in norm or "claude-3.0-opus" in norm:
            return self.pricing_table.get(
                "claude-3-opus", DEFAULT_MODEL_PRICING_PER_1M["claude-3-opus"]
            )
        if "opus" in norm:
            return self.pricing_table.get(
                "claude-opus-4-6", DEFAULT_MODEL_PRICING_PER_1M["claude-opus-4-6"]
            )
        if "sonnet" in norm or "claude" in norm:
            return self.pricing_table.get(
                "claude-3-7-sonnet", DEFAULT_MODEL_PRICING_PER_1M["claude-3-7-sonnet"]
            )

        return self.pricing_table.get("default", DEFAULT_MODEL_PRICING_PER_1M["default"])

    def calculate_cost(
        self,
        input_tokens: int = 0,
        cached_tokens: int = 0,
        output_tokens: int = 0,
        thought_tokens: int = 0,
        model_name: str = "",
    ) -> dict[str, float]:
        """
        计算单次或累计 Token 的美元与人民币预估成本。
        注意：input_tokens 为包含 cached_tokens 的总输入时，自动拆分为未缓存输入与缓存命中输入计费。
        """
        rates = self.resolve_model_rates(model_name)
        total_in = max(0, int(input_tokens or 0))
        cached_in = max(0, min(total_in, int(cached_tokens or 0)))
        uncached_in = max(0, total_in - cached_in)
        out_tok = max(0, int(output_tokens or 0))
        th_tok = max(0, int(thought_tokens or 0))

        in_rate = float(rates.get("input", 3.0))
        cache_rate = float(rates.get("cached", 0.3))
        out_rate = float(rates.get("output", 15.0))
        th_rate = float(rates.get("thought", out_rate))

        usd_cost = (
            uncached_in * in_rate
            + cached_in * cache_rate
            + out_tok * out_rate
            + th_tok * th_rate
        ) / 1_000_000.0
        cny_cost = usd_cost * self.usd_to_cny
        return {
            "usd": round(usd_cost, 6),
            "cny": round(cny_cost, 6),
        }

    def estimate_session_cost(self, session: dict[str, Any]) -> dict[str, float]:
        """根据会话字典折算累计成本与今日成本。"""
        if not isinstance(session, dict):
            return {"usd": 0.0, "cny": 0.0, "today_usd": 0.0, "today_cny": 0.0}
        model_name = str(session.get("model") or "")
        total_cost = self.calculate_cost(
            input_tokens=int(session.get("input_tokens", 0) or 0),
            cached_tokens=int(session.get("cached_tokens", 0) or 0),
            output_tokens=int(session.get("output_tokens", 0) or 0),
            thought_tokens=int(session.get("thought_tokens", 0) or 0),
            model_name=model_name,
        )
        today_cost = self.calculate_cost(
            input_tokens=int(session.get("today_input_tokens", 0) or 0),
            cached_tokens=int(session.get("today_cached_tokens", 0) or 0),
            output_tokens=int(session.get("today_output_tokens", 0) or 0),
            thought_tokens=int(session.get("today_thought_tokens", 0) or 0),
            model_name=model_name,
        )
        return {
            "usd": total_cost["usd"],
            "cny": total_cost["cny"],
            "today_usd": today_cost["usd"],
            "today_cny": today_cost["cny"],
        }


def format_cost_cny(cny: float) -> str:
    """格式化人民币费用文本（例如 ¥0.00、¥0.0312、¥1.68）。"""
    val = max(0.0, float(cny or 0.0))
    if val == 0.0:
        return "¥0.00"
    if val < 0.01:
        return f"¥{val:.4f}"
    return f"¥{val:.2f}"


def normalize_proxy_url(raw_proxy: Any) -> str:
    """规范化用户或系统代理地址：无协议头但带端口时自动补全 http://。"""
    if not isinstance(raw_proxy, str):
        return ""
    s = raw_proxy.strip()
    if not s:
        return ""
    if "://" not in s:
        s = f"http://{s}"
    return s


def resolve_proxy_settings(custom_proxy: str | None = None) -> dict[str, str]:
    """
    探测并构建代理字典，严格遵循优先级：
    1. 用户配置的 custom_proxy（如 http://127.0.0.1:7890 或 socks5://...）；
    2. 环境变量 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY（兼容大小写）；
    3. Windows 系统注册表代理（urllib.request.getproxies()）；
    4. 直连（返回空字典 {}）。
    """
    user_p = normalize_proxy_url(custom_proxy)
    if user_p:
        return {"http": user_p, "https": user_p}

    env_proxies: dict[str, str] = {}
    env_all = ""
    for k in ("ALL_PROXY", "all_proxy"):
        v = normalize_proxy_url(os.environ.get(k, ""))
        if v:
            env_all = v
            break
    for scheme, keys in (
        ("https", ("HTTPS_PROXY", "https_proxy")),
        ("http", ("HTTP_PROXY", "http_proxy")),
    ):
        for k in keys:
            v = normalize_proxy_url(os.environ.get(k, ""))
            if v:
                env_proxies[scheme] = v
                break
    if env_all:
        env_proxies.setdefault("http", env_all)
        env_proxies.setdefault("https", env_all)
    if "http" in env_proxies and "https" not in env_proxies:
        env_proxies["https"] = env_proxies["http"]
    elif "https" in env_proxies and "http" not in env_proxies:
        env_proxies["http"] = env_proxies["https"]
    if env_proxies:
        return env_proxies

    try:
        import urllib.request

        sys_raw = urllib.request.getproxies()
        if isinstance(sys_raw, dict) and sys_raw:
            sys_proxies: dict[str, str] = {}
            for scheme in ("http", "https", "all"):
                v = normalize_proxy_url(sys_raw.get(scheme, ""))
                if v:
                    if scheme == "all":
                        sys_proxies.setdefault("http", v)
                        sys_proxies.setdefault("https", v)
                    else:
                        sys_proxies[scheme] = v
            if "http" in sys_proxies and "https" not in sys_proxies:
                sys_proxies["https"] = sys_proxies["http"]
            elif "https" in sys_proxies and "http" not in sys_proxies:
                sys_proxies["http"] = sys_proxies["https"]
            if sys_proxies:
                return sys_proxies
    except Exception:
        pass

    return {}


def resolve_effective_proxy(custom_proxy: str | None = None) -> str:
    """
    解析当前生效的单一代理地址字符串（按 用户自定义 -> 环境变量 -> 系统代理 优先级，若直连则返回空字符串 ""）。
    """
    proxies = resolve_proxy_settings(custom_proxy=custom_proxy)
    return str(proxies.get("https") or proxies.get("http") or "").strip()


def safe_fetch_json(
    urls: list[str] | tuple[str, ...] | str,
    proxy: str | None = None,
    timeout: float = 3.0,
) -> dict[str, Any] | None:
    """
    安全非阻塞 JSON 请求（Fail-Safe）：
    - 支持传入主备 URL 列表，主源失败自动尝试备用源；
    - 严格限制单次请求超时（默认 3 秒）；
    - 若配置代理连接失败且未强制锁死，自动回退尝试直连；
    - 遇到断网、代理错误、DNS 解析失败、JSON 损坏等任何异常均静默捕获并返回 None。
    """
    if isinstance(urls, str):
        url_list = [urls] if urls.strip() else []
    elif isinstance(urls, (list, tuple)):
        url_list = [str(u).strip() for u in urls if str(u).strip()]
    else:
        return None
    if not url_list:
        return None

    import urllib.request

    eff_timeout = max(0.2, min(15.0, float(timeout or 3.0)))
    proxies = resolve_proxy_settings(custom_proxy=proxy)

    # 先构建带代理（若有）的 opener，若无代理则直接直连；代理失败时允许降级直连兜底
    opener_configs: list[dict[str, str]] = [proxies] if proxies else [{}]
    if proxies:
        opener_configs.append({})

    headers = {
        "User-Agent": "Antigravity-Token-HUD/1.2 (+https://github.com/GENSHINLY/Antigravity-token-hud)",
        "Accept": "application/json",
    }

    for url in url_list:
        for p_cfg in opener_configs:
            try:
                handler = urllib.request.ProxyHandler(p_cfg)
                opener = urllib.request.build_opener(handler)
                req = urllib.request.Request(url, headers=headers, method="GET")
                with opener.open(req, timeout=eff_timeout) as resp:
                    raw_bytes = resp.read(1024 * 512)
                text = raw_bytes.decode("utf-8", errors="replace")
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return None


def parse_usd_to_cny_payload(payload: Any) -> float | None:
    """从汇率 API 响应字典中提取并校验 USD->CNY 汇率（有效范围 5.0 <= rate <= 10.0）。"""
    if not isinstance(payload, dict):
        return None
    candidate: Any = None
    rates_obj = payload.get("rates")
    if isinstance(rates_obj, dict):
        candidate = rates_obj.get("CNY", rates_obj.get("cny"))
    if candidate is None:
        candidate = payload.get("usd_to_cny_rate")
    if candidate is None or isinstance(candidate, bool):
        return None
    try:
        rate_val = float(candidate)
        if (
            rate_val == rate_val
            and rate_val not in (float("inf"), float("-inf"))
            and MIN_VALID_USD_TO_CNY_RATE <= rate_val <= MAX_VALID_USD_TO_CNY_RATE
        ):
            return round(rate_val, 4)
    except (TypeError, ValueError):
        pass
    return None


def fetch_online_usd_rate(
    proxy: str | None = None,
    timeout: float = 3.0,
    urls: list[str] | tuple[str, ...] | None = None,
) -> float | None:
    """
    在线实时拉取 USD/CNY 汇率：
    - 主源：https://open.er-api.com/v6/latest/USD
    - 备源：https://api.exchangerate-api.com/v4/latest/USD
    - 校验 5.0 <= rate <= 10.0，成功返回四舍五入浮点值，失败返回 None。
    """
    target_urls = urls if urls is not None else USD_RATE_ENDPOINTS
    if isinstance(target_urls, str):
        url_seq: tuple[str, ...] = (target_urls,)
    else:
        url_seq = tuple(target_urls)

    for u in url_seq:
        data = safe_fetch_json([u], proxy=proxy, timeout=timeout)
        rate = parse_usd_to_cny_payload(data)
        if rate is not None:
            return rate
    return None


def normalize_remote_pricing_payload(payload: Any) -> dict[str, dict[str, float]] | None:
    """校验并规范化远端价格表 JSON 数据，提取合法的 {canon_key: {input, cached, output, thought}} 字典。"""
    if not isinstance(payload, dict):
        return None
    raw_table = payload.get("pricing") if isinstance(payload.get("pricing"), dict) else payload
    if not isinstance(raw_table, dict):
        return None

    cleaned: dict[str, dict[str, float]] = {}
    for raw_k, raw_v in raw_table.items():
        if not isinstance(raw_v, dict):
            continue
        key_str = str(raw_k or "").strip()
        if not key_str or key_str in ("version", "updated_at", "usd_to_cny_rate"):
            continue
        canon_k = TokenPricingEngine.canonicalize_model_key(key_str)
        base_rates = dict(
            DEFAULT_MODEL_PRICING_PER_1M.get(
                canon_k, DEFAULT_MODEL_PRICING_PER_1M["default"]
            )
        )
        valid_fields = 0
        for field in ("input", "cached", "output", "thought"):
            if field in raw_v and not isinstance(raw_v[field], bool):
                try:
                    f_val = float(raw_v[field])
                    if f_val == f_val and f_val not in (float("inf"), float("-inf")) and 0.0 <= f_val <= 100000.0:
                        base_rates[field] = round(f_val, 6)
                        valid_fields += 1
                except (TypeError, ValueError):
                    pass
        if valid_fields == 0:
            continue
        if "thought" not in raw_v and "output" in raw_v:
            base_rates["thought"] = base_rates["output"]
        cleaned[canon_k] = base_rates

    return cleaned if cleaned else None


def fetch_remote_pricing_table(
    proxy: str | None = None,
    timeout: float = 3.0,
    urls: list[str] | tuple[str, ...] | None = None,
) -> dict[str, dict[str, float]] | None:
    """
    拉取远程模型价格表（主源 GitHub Raw + 备源 jsDelivr CDN）：
    成功返回规范化后的模型单价字典，失败静默返回 None。
    """
    target_urls = urls if urls is not None else REMOTE_PRICING_ENDPOINTS
    if isinstance(target_urls, str):
        url_seq: tuple[str, ...] = (target_urls,)
    else:
        url_seq = tuple(target_urls)

    for u in url_seq:
        data = safe_fetch_json([u], proxy=proxy, timeout=timeout)
        parsed = normalize_remote_pricing_payload(data)
        if parsed:
            return parsed
    return None


def merge_remote_pricing_into_config(
    local_pricing: dict[str, Any] | None,
    remote_pricing: dict[str, Any] | None,
    default_pricing: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, float]]:
    """
    将远程拉取的价格表安全合并至当前本地配置：
    - 优先保留用户已手动修改的单价（即 local_pricing 中与本地官方默认单价不同的自定义项）；
    - 若本地某模型单价仍为官方默认值（未手动修改），则平滑更新为远端最新官方单价；
    - 自动补全远端新增的模型及其费率。
    """
    base_defaults = (
        {k: dict(v) for k, v in default_pricing.items()}
        if isinstance(default_pricing, dict)
        else get_default_pricing_config()
    )
    merged: dict[str, dict[str, float]] = {k: dict(v) for k, v in base_defaults.items()}

    norm_local: dict[str, dict[str, float]] = {}
    if isinstance(local_pricing, dict):
        for raw_k, raw_v in local_pricing.items():
            if not isinstance(raw_v, dict):
                continue
            canon_k = TokenPricingEngine.canonicalize_model_key(str(raw_k))
            entry = dict(
                merged.get(
                    canon_k,
                    DEFAULT_MODEL_PRICING_PER_1M.get(
                        canon_k, DEFAULT_MODEL_PRICING_PER_1M["default"]
                    ),
                )
            )
            for field in ("input", "cached", "output", "thought"):
                if field in raw_v and not isinstance(raw_v[field], bool):
                    try:
                        val = float(raw_v[field])
                        if val == val and val not in (float("inf"), float("-inf")) and val >= 0.0:
                            entry[field] = round(val, 6)
                    except (TypeError, ValueError):
                        pass
            norm_local[canon_k] = entry
            merged[canon_k] = dict(entry)

    norm_remote = normalize_remote_pricing_payload(remote_pricing)
    if not norm_remote:
        return merged

    for canon_k, remote_rates in norm_remote.items():
        default_entry = base_defaults.get(
            canon_k, DEFAULT_MODEL_PRICING_PER_1M.get(canon_k)
        )
        local_entry = norm_local.get(canon_k)
        if local_entry is None:
            # 本地尚无该模型配置，直接补充远端新模型
            merged[canon_k] = dict(remote_rates)
            continue

        merged_entry = dict(local_entry)
        for field in ("input", "cached", "output", "thought"):
            r_val = round(float(remote_rates.get(field, 0.0)), 6)
            l_val = round(float(local_entry.get(field, 0.0)), 6)
            if default_entry is not None and field in default_entry:
                d_val = round(float(default_entry[field]), 6)
                # 若本地值等于默认预设值（用户未手动改过），采纳远端最新价格；否则保留用户自定义单价
                if abs(l_val - d_val) < 1e-6:
                    merged_entry[field] = r_val
                else:
                    merged_entry[field] = l_val
            else:
                # 针对非预设的扩展模型，保留用户本地已有单价
                merged_entry[field] = l_val
        merged[canon_k] = merged_entry

    return merged



