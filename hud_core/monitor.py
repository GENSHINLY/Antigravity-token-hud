# -*- coding: utf-8 -*-
"""
hud_core.monitor - TokenMonitorCache、增量字节偏移缓存、跨天零 I/O 归零、历史记录归档与 CSV 报表导出逻辑。
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import (
    ANTIGRAVITY_BRAIN_ROOT,
    GEMINI_ROOT,
    GEMINI_TMP_ROOT,
    LIVE_STATE_FILE,
    ConfigManager,
    auto_detect_antigravity_path,
)
from .parsers import (
    PARSER_REGISTRY,
    AntigravityBrainParser,
    _PRICING_ENGINE,
    reset_session_today_stats,
)
from .pricing import DEFAULT_USD_TO_CNY_RATE, TokenPricingEngine

DEFAULT_HISTORY_FILE = GEMINI_ROOT / "token_hud_history.json"
CSV_FIELDNAMES = [
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


class TokenMonitorCache:
    """增量缓存主 Agent (Antigravity) 与子 Agent (Gemini CLI / Antigravity / Claude Code) 日志解析结果（支持多线程安全读取）。"""

    IDLE_BACKOFF_THRESHOLD_SEC: float = 30.0
    STANDBY_POLL_INTERVAL_MS: int = 3000

    def __init__(
        self,
        config_mgr: ConfigManager | None = None,
        tmp_root: Path = GEMINI_TMP_ROOT,
        brain_root: Path = ANTIGRAVITY_BRAIN_ROOT,
        scan_ttl_sec: float | None = None,
        history_path: Path | str | None = None,
    ):
        self.config_mgr = config_mgr if config_mgr is not None else ConfigManager()
        self.tmp_root = tmp_root
        self.brain_root = brain_root
        if scan_ttl_sec is not None:
            self.scan_ttl_sec = float(scan_ttl_sec)
        else:
            self.scan_ttl_sec = float(self.config_mgr.get("scan_ttl_sec", 2.5) or 2.5)
        self._explicit_history_path = (
            Path(history_path).expanduser() if history_path is not None else None
        )
        self.history_path: Path = self._resolve_history_path()
        self._lock = threading.RLock()
        self._sub_cache: dict[str, dict[str, Any]] = {}
        self._main_cache: dict[str, dict[str, Any]] = {}
        self._main_paths_cache: list[Path] = []
        self._sub_entries_cache: list[tuple[dict[str, str], Path]] = []
        self._main_agent_valid: bool = False
        self._last_dir_scan_ts: float = 0.0
        self._last_scan_config_sig: str = ""
        self._initial_scan_done: bool = False
        self.last_activity_time: float = time.time()
        self.idle_backoff_sec: float = self.IDLE_BACKOFF_THRESHOLD_SEC
        self.standby_poll_ms: int = self.STANDBY_POLL_INTERVAL_MS
        self.midnight_reset_count: int = 0
        self._current_date: datetime.date | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self.pricing_engine: TokenPricingEngine = _PRICING_ENGINE
        self.sync_pricing_from_config()

    def sync_pricing_from_config(self) -> None:
        """从 ConfigManager 同步自定义模型费率与汇率至全局与实例定价引擎。"""
        pricing_cfg = self.config_mgr.get("pricing")
        raw_rate = self.config_mgr.get("usd_to_cny_rate", DEFAULT_USD_TO_CNY_RATE)
        try:
            rate_val = float(raw_rate or DEFAULT_USD_TO_CNY_RATE)
        except (TypeError, ValueError):
            rate_val = DEFAULT_USD_TO_CNY_RATE
        _PRICING_ENGINE.update_pricing(
            pricing_table=pricing_cfg if isinstance(pricing_cfg, dict) else None,
            usd_to_cny=rate_val,
        )
        self.pricing_engine = _PRICING_ENGINE

    def _resolve_history_path(self) -> Path:
        if self._explicit_history_path is not None:
            return self._explicit_history_path
        cfg_hist = str(self.config_mgr.get("history_file_path", "") or "").strip()
        if cfg_hist:
            return Path(cfg_hist).expanduser()
        if getattr(self.config_mgr, "_explicit_path", None) is not None:
            return Path(self.config_mgr.config_path).parent / "token_hud_history.json"
        return DEFAULT_HISTORY_FILE

    def get_file_cache_state(
        self,
        file_path: str | Path,
        parser_type: str | None = None,
    ) -> dict[str, Any] | None:
        """获取指定监控文件的内部增量缓存状态（含 last_byte_offset、incremental_reads 等）。"""
        p_str = str(Path(file_path))
        norm_target = os.path.normcase(os.path.normpath(p_str))
        if parser_type:
            direct_key = f"{parser_type}:{p_str}"
            if direct_key in self._sub_cache:
                return self._sub_cache[direct_key]
        if p_str in self._main_cache:
            return self._main_cache[p_str]
        for k, v in self._main_cache.items():
            if os.path.normcase(os.path.normpath(k)) == norm_target:
                return v
        for k, v in self._sub_cache.items():
            fp = str(v.get("file_path") or "")
            if os.path.normcase(os.path.normpath(fp)) == norm_target:
                if parser_type is None or v.get("parser_type") == parser_type:
                    return v
        return None

    def get_recommended_poll_interval_ms(
        self,
        base_interval_ms: int = 1000,
        now: float | None = None,
    ) -> int:
        """根据最近一次磁盘活动时间返回自适应轮询周期（活跃期返回原始周期，连续空闲超阈值退避至待机周期）。"""
        cur_now = now if now is not None else time.time()
        if (cur_now - self.last_activity_time) >= self.idle_backoff_sec:
            return max(int(base_interval_ms), int(self.standby_poll_ms))
        return int(base_interval_ms)

    def _get_enabled_sub_agent_configs(self) -> list[dict[str, Any]]:
        """获取当前配置中已启用且路径非空的子 Agent 列表（确保每个配置项含唯一 id）。"""
        raw_subs = self.config_mgr.get("sub_agents", [])
        if not isinstance(raw_subs, list):
            return []
        enabled_list: list[dict[str, Any]] = []
        seen_ids: set[str] = {"main"}
        for idx, item in enumerate(raw_subs):
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("id") or "").strip()
            agent_id = raw_id if (raw_id and raw_id not in seen_ids) else f"sub_{idx}"
            while agent_id in seen_ids:
                idx += 1
                agent_id = f"sub_{idx}"
            seen_ids.add(agent_id)
            if not bool(item.get("enabled", True)):
                continue
            if not str(item.get("path") or "").strip():
                continue
            cfg_copy = dict(item)
            cfg_copy["id"] = agent_id
            enabled_list.append(cfg_copy)
        return enabled_list

    def read_live_state(self, live_state_path: str | Path | None = None) -> dict[str, Any]:
        """读取实时流式状态文件（若存在）。"""
        candidates: list[Path] = []
        if live_state_path:
            p_str = str(live_state_path).strip()
            if p_str:
                candidates.append(Path(p_str).expanduser())
        else:
            sub_agent_configs = self._get_enabled_sub_agent_configs()
            for item in sub_agent_configs:
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

            if parser_type not in PARSER_REGISTRY:
                parser_type = "gemini_cli_session"
            return PARSER_REGISTRY[parser_type]["discover"](p)
        except Exception:
            return []

    def _get_session_files(
        self, now: float, force_scan: bool = False
    ) -> tuple[list[Path], list[tuple[dict[str, str], Path]], bool]:
        """缓存目录枚举结果，避免每次刷新都全量 glob 遍历数百个子目录。"""
        self.scan_ttl_sec = float(self.config_mgr.get("scan_ttl_sec", self.scan_ttl_sec) or 2.5)
        main_cfg_path = str(self.config_mgr.get("main_agent_path", "") or "").strip()
        sub_agent_configs = self._get_enabled_sub_agent_configs()

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
                for idx, raw_cfg in enumerate(sub_agent_configs):
                    if not isinstance(raw_cfg, dict):
                        continue
                    path_str = str(raw_cfg.get("path") or "").strip()
                    if not path_str:
                        continue
                    agent_id = str(raw_cfg.get("id") or f"sub_{idx}").strip() or f"sub_{idx}"
                    agent_name = ConfigManager._resolve_sub_agent_display_name(
                        raw_cfg.get("name"), path_str
                    )
                    parser_type = str(raw_cfg.get("parser_type") or "gemini_cli_session").strip()
                    if parser_type not in PARSER_REGISTRY:
                        parser_type = "gemini_cli_session"
                    live_state_path = str(raw_cfg.get("live_state_path") or "").strip()
                    norm_cfg = {
                        "id": agent_id,
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

    def _resolve_session_with_cache(
        self,
        cache_store: dict[str, dict[str, Any]],
        cache_key: str,
        file_path: Path,
        parser_type: str,
        mtime: float,
        size: int,
        today_date: datetime.date,
        today_midnight_ts: float,
        now: float,
        full_history: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """
        核心缓存状态机：
        1. 若命中跨天（today_date 变化）且文件 mtime 早于今日 00:00（或未发生写入）：
           直接在内存中将 today_* 重置为 0，零磁盘 I/O。
        2. 若文件 mtime 与 size 均未改变且日期未变：
           直接返回内存缓存并刷新状态（running/active/completed），零磁盘 I/O。
        3. 若文件发生修改（mtime 改变或 size 增加，且 size >= last_byte_offset）：
           通过 file.seek(last_byte_offset) 执行字节流增量解析并合并新增 steps。
        4. 若文件变小（size < last_byte_offset）：
           自动重置 last_byte_offset = 0 并全量重新解析。
        """
        reg_entry = PARSER_REGISTRY.get(parser_type) or PARSER_REGISTRY["gemini_cli_session"]
        parser_cls = reg_entry.get("parser_cls", AntigravityBrainParser)
        state = cache_store.get(cache_key)

        if state is not None:
            if bool(state.get("is_lazy_loaded")) and full_history:
                state["last_byte_offset"] = 0
                parsed = parser_cls.parse(
                    file_path,
                    state=state,
                    mtime=mtime,
                    size=size,
                    today_date=today_date,
                    now_ts=now,
                )
                state["is_lazy_loaded"] = False
                state["info"]["is_lazy_loaded"] = False
                parsed["is_lazy_loaded"] = False
                return parsed, True

            cached_mtime = float(state.get("mtime") or 0.0)
            cached_size = int(state.get("size") or 0)
            cached_date = state.get("today_date")
            unchanged_on_disk = (mtime == cached_mtime and size == cached_size)

            if cached_date != today_date and (mtime < today_midnight_ts or unchanged_on_disk):
                # 跨天（00:00）零 I/O 内存归零
                reset_session_today_stats(state["info"], state, today_date=today_date)
                self.midnight_reset_count += 1
                if unchanged_on_disk:
                    parsed = parser_cls.refresh_status(state, now_ts=now)
                    return parsed, False

            if unchanged_on_disk and state.get("today_date") == today_date:
                state["bytes_read_last"] = 0
                parsed = parser_cls.refresh_status(state, now_ts=now)
                return parsed, False

            # 文件发生修改（追加或截断重写）：执行增量字节偏移读取或回退全量读取
            parsed = parser_cls.parse(
                file_path,
                state=state,
                mtime=mtime,
                size=size,
                today_date=today_date,
                now_ts=now,
            )
            return parsed, True

        # 首次发现该文件：按需活跃窗口过滤与历史日志懒加载
        state = parser_cls.create_state(file_path, today_date=today_date)
        if mtime < today_midnight_ts and not full_history:
            # 历史文件今日无新写入，跳过全量磁盘读取，设置尾部偏移供后续追加写入时增量 seek
            state["mtime"] = float(mtime)
            state["size"] = int(size)
            state["last_byte_offset"] = int(size)
            state["today_date"] = today_date
            state["is_lazy_loaded"] = True
            info = state["info"]
            info["file_path"] = str(file_path)
            info["start_time"] = float(mtime)
            info["last_updated"] = float(mtime)
            info["last_byte_offset"] = int(size)
            info["status"] = "completed"
            info["is_lazy_loaded"] = True
            cache_store[cache_key] = state
            return dict(info), False

        parsed = parser_cls.parse(
            file_path,
            state=state,
            mtime=mtime,
            size=size,
            today_date=today_date,
            now_ts=now,
        )
        state["is_lazy_loaded"] = False
        state["info"]["is_lazy_loaded"] = False
        parsed["is_lazy_loaded"] = False
        cache_store[cache_key] = state
        return parsed, True

    def resolve_single_session_full(
        self,
        file_path: str | Path,
        parser_type: str | None = None,
    ) -> dict[str, Any] | None:
        """按需对单个指定的历史会话文件执行全量解析（用于用户在侧边栏点击历史懒加载条目时）。"""
        with self._lock:
            target_path = Path(file_path).resolve()
            cache_state = self.get_file_cache_state(target_path, parser_type=parser_type)
            if not cache_state:
                cache_state = self.get_file_cache_state(file_path, parser_type=parser_type)
            if not cache_state:
                # 若缓存中不存在，则尝试在主缓存或子缓存中解析
                return None

            # 如果已经不是懒加载状态，直接返回其 info
            if not bool(cache_state.get("is_lazy_loaded")):
                return dict(cache_state.get("info", {}))

            p_type = str(cache_state.get("parser_type") or parser_type or "gemini_cli_session")
            reg_entry = PARSER_REGISTRY.get(p_type) or PARSER_REGISTRY["gemini_cli_session"]
            parser_cls = reg_entry.get("parser_cls", AntigravityBrainParser)
            try:
                st = target_path.stat()
                now = time.time()
                today = self._current_date or datetime.date.today()
                # 重置偏移量从头全量解析单文件
                cache_state["last_byte_offset"] = 0
                parsed = parser_cls.parse(
                    target_path,
                    state=cache_state,
                    mtime=st.st_mtime,
                    size=st.st_size,
                    today_date=today,
                    now_ts=now,
                )
                cache_state["is_lazy_loaded"] = False
                if isinstance(cache_state.get("info"), dict):
                    cache_state["info"]["is_lazy_loaded"] = False
                parsed["is_lazy_loaded"] = False
                return parsed
            except Exception:
                return dict(cache_state.get("info", {}))

    # ------------------------------------------------------------------
    # 历史数据归档与 CSV 报表导出
    # ------------------------------------------------------------------
    @staticmethod
    def _build_daily_history_entry_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
        """根据快照构造单日归档记录（含日期、总体 Token 与成本、按 Agent 及按 Model 细分统计）。"""
        date_str = str(snap.get("today_date") or datetime.date.today().isoformat())
        today = snap.get("today_stats") or {}

        main_sessions = snap.get("all_main_sessions") or []
        sub_sessions = snap.get("all_sub_sessions") or []

        by_agent: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        records_map: dict[tuple[str, str], dict[str, Any]] = {}

        def _acc_session(sess: dict[str, Any], default_agent: str, default_model: str) -> None:
            t_steps = int(sess.get("today_steps", 0) or 0)
            t_tot = int(sess.get("today_total_tokens", 0) or 0)
            if t_steps <= 0 and t_tot <= 0:
                return
            agent_label = str(
                sess.get("sub_agent_name")
                or sess.get("agent_name")
                or default_agent
            ).strip() or default_agent
            model_label = str(sess.get("model") or default_model).strip() or default_model
            in_t = int(sess.get("today_input_tokens", 0) or 0)
            cache_t = int(sess.get("today_cached_tokens", 0) or 0)
            out_t = int(sess.get("today_output_tokens", 0) or 0)
            th_t = int(sess.get("today_thought_tokens", 0) or 0)
            cost_u = float(sess.get("today_cost_usd", 0.0) or 0.0)

            for grp_dict, grp_key in ((by_agent, agent_label), (by_model, model_label)):
                if grp_key not in grp_dict:
                    grp_dict[grp_key] = {
                        "sessions_count": 0,
                        "steps_count": 0,
                        "input_tokens": 0,
                        "cached_tokens": 0,
                        "output_tokens": 0,
                        "thought_tokens": 0,
                        "total_tokens": 0,
                        "cost_usd": 0.0,
                    }
                g = grp_dict[grp_key]
                g["sessions_count"] += 1
                g["steps_count"] += t_steps
                g["input_tokens"] += in_t
                g["cached_tokens"] += cache_t
                g["output_tokens"] += out_t
                g["thought_tokens"] += th_t
                g["total_tokens"] += t_tot
                g["cost_usd"] = round(float(g["cost_usd"]) + cost_u, 6)

            r_key = (agent_label, model_label)
            if r_key not in records_map:
                records_map[r_key] = {
                    "agent": agent_label,
                    "model": model_label,
                    "sessions_count": 0,
                    "steps_count": 0,
                    "input_tokens": 0,
                    "cached_tokens": 0,
                    "output_tokens": 0,
                    "thought_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                }
            rec = records_map[r_key]
            rec["sessions_count"] += 1
            rec["steps_count"] += t_steps
            rec["input_tokens"] += in_t
            rec["cached_tokens"] += cache_t
            rec["output_tokens"] += out_t
            rec["thought_tokens"] += th_t
            rec["total_tokens"] += t_tot
            rec["cost_usd"] = round(float(rec["cost_usd"]) + cost_u, 6)

        for ms in main_sessions:
            _acc_session(ms, "Antigravity", "Antigravity (Main)")
        for ss in sub_sessions:
            _acc_session(ss, "SubAgent", "gemini-3.8-pro")

        in_tok = int(today.get("input_tokens", 0) or 0)
        cache_tok = int(today.get("cached_tokens", 0) or 0)
        out_tok = int(today.get("output_tokens", 0) or 0)
        th_tok = int(today.get("thought_tokens", 0) or 0)
        tot_tok = int(today.get("total_tokens", 0) or 0)
        cost_usd = round(
            float(today.get("today_cost_usd", today.get("cost_usd", 0.0)) or 0.0), 6
        )

        # 若快照僅传入 today_stats 而无会话明细列表，自动生成一条汇总明细以供 CSV 导出
        if not records_map and (tot_tok > 0 or in_tok > 0 or out_tok > 0):
            records_map[("All", "All")] = {
                "agent": "All",
                "model": "All",
                "sessions_count": int(today.get("sessions_count", 0) or 0),
                "steps_count": int(today.get("steps_count", 0) or 0),
                "input_tokens": in_tok,
                "cached_tokens": cache_tok,
                "output_tokens": out_tok,
                "thought_tokens": th_tok,
                "total_tokens": tot_tok,
                "cost_usd": cost_usd,
            }

        return {
            "date": date_str,
            "updated_at": float(snap.get("timestamp", time.time()) or time.time()),
            "sessions_count": int(today.get("sessions_count", 0) or 0),
            "steps_count": int(today.get("steps_count", 0) or 0),
            "input_tokens": in_tok,
            "cached_tokens": cache_tok,
            "output_tokens": out_tok,
            "thought_tokens": th_tok,
            "total_tokens": tot_tok,
            "cost_usd": cost_usd,
            "by_agent": by_agent,
            "by_model": by_model,
            "records": list(records_map.values()),
        }

    def load_history(self, history_path: Path | str | None = None) -> dict[str, Any]:
        """读取本地历史 JSON 文件，返回结构化字典 {"version": "1.2", "days": {...}}。"""
        target_path = Path(history_path).expanduser() if history_path else self._resolve_history_path()
        if not target_path.is_file():
            return {"version": "1.2", "days": {}}
        try:
            raw = json.loads(target_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                days = raw.get("days")
                if isinstance(days, dict):
                    return {"version": str(raw.get("version") or "1.2"), "days": dict(days)}
            elif isinstance(raw, list):
                days_map: dict[str, Any] = {}
                for item in raw:
                    if isinstance(item, dict) and item.get("date"):
                        days_map[str(item["date"])] = item
                return {"version": "1.2", "days": days_map}
        except Exception:
            pass
        return {"version": "1.2", "days": {}}

    def archive_daily_snapshot(
        self,
        snap: dict[str, Any] | None = None,
        history_path: Path | str | None = None,
    ) -> Path:
        """
        将当日 Token 聚合数据（日期、输入/缓存/输出/思考、折算成本、Agent/模型细分）
        按日期去重更新保存至本地历史 JSON 文件。
        """
        with self._lock:
            target_snap = snap if isinstance(snap, dict) else self._last_snapshot
            if not isinstance(target_snap, dict):
                target_snap = {
                    "timestamp": time.time(),
                    "today_date": (
                        self._current_date.isoformat()
                        if self._current_date is not None
                        else datetime.date.today().isoformat()
                    ),
                    "today_stats": {},
                    "all_main_sessions": [],
                    "all_sub_sessions": [],
                }
            target_path = (
                Path(history_path).expanduser()
                if history_path is not None
                else self._resolve_history_path()
            )
            self.history_path = target_path

            entry = self._build_daily_history_entry_from_snapshot(target_snap)
            date_key = entry["date"]

            hist_data = self.load_history(target_path)
            days = hist_data.setdefault("days", {})
            days[date_key] = entry

            # 按日期升序重排键顺序
            sorted_days = {k: days[k] for k in sorted(days.keys())}
            hist_data["days"] = sorted_days

            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(
                    json.dumps(hist_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass
            return target_path

    def save_history(
        self,
        snap: dict[str, Any] | None = None,
        history_path: Path | str | None = None,
    ) -> Path:
        """持久化当日快照归档数据至本地历史 JSON 文件（archive_daily_snapshot 的别名）。"""
        return self.archive_daily_snapshot(snap=snap, history_path=history_path)

    def export_history_csv(
        self,
        csv_path: Path | str,
        history_path: Path | str | None = None,
        include_current: bool = True,
    ) -> Path:
        """
        将历史归档数据（可选包含当前内存最新快照）导出为标准 CSV 报表。
        CSV 表头: Date, Agent, Model, InputTokens, CachedTokens, OutputTokens, ThoughtTokens, TotalTokens, CostUSD
        """
        with self._lock:
            if include_current and isinstance(self._last_snapshot, dict):
                self.archive_daily_snapshot(self._last_snapshot, history_path=history_path)

            hist_data = self.load_history(history_path)
            days: dict[str, Any] = hist_data.get("days", {})

            out_path = Path(csv_path).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)

            rows_to_write: list[dict[str, Any]] = []
            for date_str in sorted(days.keys()):
                day_entry = days[date_str]
                if not isinstance(day_entry, dict):
                    continue
                records = day_entry.get("records")
                if isinstance(records, list) and records:
                    for rec in records:
                        if not isinstance(rec, dict):
                            continue
                        rows_to_write.append(
                            {
                                "Date": date_str,
                                "Agent": str(rec.get("agent") or "All"),
                                "Model": str(rec.get("model") or "All"),
                                "InputTokens": int(rec.get("input_tokens", 0) or 0),
                                "CachedTokens": int(rec.get("cached_tokens", 0) or 0),
                                "OutputTokens": int(rec.get("output_tokens", 0) or 0),
                                "ThoughtTokens": int(rec.get("thought_tokens", 0) or 0),
                                "TotalTokens": int(rec.get("total_tokens", 0) or 0),
                                "CostUSD": f"{float(rec.get('cost_usd', 0.0) or 0.0):.6f}",
                            }
                        )
                else:
                    rows_to_write.append(
                        {
                            "Date": date_str,
                            "Agent": "All",
                            "Model": "All",
                            "InputTokens": int(day_entry.get("input_tokens", 0) or 0),
                            "CachedTokens": int(day_entry.get("cached_tokens", 0) or 0),
                            "OutputTokens": int(day_entry.get("output_tokens", 0) or 0),
                            "ThoughtTokens": int(day_entry.get("thought_tokens", 0) or 0),
                            "TotalTokens": int(day_entry.get("total_tokens", 0) or 0),
                            "CostUSD": f"{float(day_entry.get('cost_usd', 0.0) or 0.0):.6f}",
                        }
                    )

            with out_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
                writer.writeheader()
                for row in rows_to_write:
                    writer.writerow(row)

            return out_path

    def export_history_to_csv(
        self,
        output_path: Path | str,
        history_path: Path | str | None = None,
        include_current: bool = True,
    ) -> Path:
        """导出历史归档数据至指定 CSV 文件的别名方法。"""
        return self.export_history_csv(
            csv_path=output_path,
            history_path=history_path,
            include_current=include_current,
        )

    def collect_snapshot(
        self,
        target_session: str | None = None,
        target_project: str | None = None,
        force_scan: bool = False,
        now_ts: float | None = None,
        today_date: datetime.date | None = None,
        full_history: bool = False,
    ) -> dict[str, Any]:
        """采集主 Agent、子 Agent 当前活跃会话数据以及今日双引擎全局统计快照。"""
        with self._lock:
            self.sync_pricing_from_config()
            now = float(now_ts) if now_ts is not None else time.time()
            t_date = today_date if today_date is not None else datetime.date.today()
            today_midnight_ts = datetime.datetime.combine(t_date, datetime.time.min).timestamp()

            # 跨天自动归档前一日快照数据
            if (
                self._current_date is not None
                and self._current_date != t_date
                and isinstance(self._last_snapshot, dict)
            ):
                try:
                    self.archive_daily_snapshot(self._last_snapshot)
                except Exception:
                    pass

            self._current_date = t_date

            main_paths, sub_entries, main_agent_valid = self._get_session_files(
                now, force_scan=force_scan
            )

            disk_modified = False

            # 1. 扫描主 Agent (Antigravity) transcript.jsonl
            main_sessions: list[dict[str, Any]] = []
            for tr_path in main_paths:
                key = str(tr_path)
                try:
                    st = tr_path.stat()
                    mtime, size = st.st_mtime, st.st_size
                except OSError:
                    continue

                parsed, changed = self._resolve_session_with_cache(
                    cache_store=self._main_cache,
                    cache_key=key,
                    file_path=tr_path,
                    parser_type="antigravity_transcript",
                    mtime=mtime,
                    size=size,
                    today_date=t_date,
                    today_midnight_ts=today_midnight_ts,
                    now=now,
                    full_history=full_history,
                )
                if changed:
                    disk_modified = True

                parsed["agent_id"] = "main"
                main_sessions.append(parsed)

            main_active: dict[str, Any] | None = None
            if main_sessions:
                main_sessions.sort(key=lambda x: x.get("last_updated", 0.0), reverse=True)
                main_active = dict(main_sessions[0])

            # 2. 扫描子 Agent（按已启用的 sub_agents 配置列表解析，无启用项时不扫描）
            sub_agent_configs = self._get_enabled_sub_agent_configs()

            sub_sessions: list[dict[str, Any]] = []
            if sub_agent_configs:
                for agent_cfg, jsonl_path in sub_entries:
                    parser_type = agent_cfg.get("parser_type", "gemini_cli_session")
                    if parser_type not in PARSER_REGISTRY:
                        parser_type = "gemini_cli_session"
                    agent_id = str(agent_cfg.get("id") or "sub_0")
                    agent_name = agent_cfg.get("name", "SubAgent")
                    live_state_path = agent_cfg.get("live_state_path", "")
                    key = f"{parser_type}:{jsonl_path}"
                    try:
                        st = jsonl_path.stat()
                        mtime, size = st.st_mtime, st.st_size
                    except OSError:
                        continue

                    parsed, changed = self._resolve_session_with_cache(
                        cache_store=self._sub_cache,
                        cache_key=key,
                        file_path=jsonl_path,
                        parser_type=parser_type,
                        mtime=mtime,
                        size=size,
                        today_date=t_date,
                        today_midnight_ts=today_midnight_ts,
                        now=now,
                        full_history=full_history,
                    )
                    if changed:
                        disk_modified = True

                    if parser_type == "antigravity_transcript":
                        if not parsed.get("session_id"):
                            parsed["session_id"] = str(parsed.get("cid") or "")
                        if not parsed.get("project"):
                            parsed["project"] = agent_name

                    parsed["agent_id"] = agent_id
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
            if live_is_fresh and live_updated > self.last_activity_time:
                disk_modified = True

            if not self._initial_scan_done:
                self._initial_scan_done = True
                if disk_modified:
                    self.last_activity_time = now
            elif disk_modified:
                self.last_activity_time = now

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
                first_sub_id = str(sub_agent_configs[0].get("id") or "sub_0") if sub_agent_configs else "sub_0"
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
                    "cost_usd": 0.0,
                    "cost_cny": 0.0,
                    "total_cost_usd": 0.0,
                    "total_cost_cny": 0.0,
                    "today_cost_usd": 0.0,
                    "today_cost_cny": 0.0,
                    "stream_est_tokens": int(live_state.get("stream_est_output_tokens") or 0),
                    "agent_id": first_sub_id,
                    "agent_name": first_sub_name,
                    "sub_agent_name": first_sub_name,
                    "parser_type": first_parser,
                }

            # 3. 汇总按 Agent 粒度的会话分组与今日统计，以及双引擎全局统计
            def _empty_agent_today() -> dict[str, Any]:
                return {
                    "sessions_count": 0,
                    "steps_count": 0,
                    "input_tokens": 0,
                    "cached_tokens": 0,
                    "output_tokens": 0,
                    "thought_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "cost_cny": 0.0,
                    "today_cost_usd": 0.0,
                    "total_cost_usd": 0.0,
                }

            sessions_by_agent: dict[str, list[dict[str, Any]]] = {
                "main": [dict(s) for s in main_sessions]
            }
            today_by_agent: dict[str, dict[str, Any]] = {
                "main": _empty_agent_today()
            }
            for idx, scfg in enumerate(sub_agent_configs):
                aid = str(scfg.get("id") or f"sub_{idx}").strip() or f"sub_{idx}"
                if aid not in sessions_by_agent:
                    sessions_by_agent[aid] = []
                if aid not in today_by_agent:
                    today_by_agent[aid] = _empty_agent_today()

            main_today = today_by_agent["main"]
            main_all_cost_usd = 0.0
            main_all_cost_cny = 0.0
            for ms in main_sessions:
                m_tot_u = float(ms.get("total_cost_usd", ms.get("cost_usd", 0.0)) or 0.0)
                m_tot_c = float(ms.get("total_cost_cny", ms.get("cost_cny", 0.0)) or 0.0)
                main_all_cost_usd = round(main_all_cost_usd + m_tot_u, 6)
                main_all_cost_cny = round(main_all_cost_cny + m_tot_c, 6)
                main_today["total_cost_usd"] = main_all_cost_usd

                t_steps = int(ms.get("today_steps", 0))
                if t_steps > 0:
                    main_today["sessions_count"] += 1
                    main_today["steps_count"] += t_steps
                    main_today["input_tokens"] += int(ms.get("today_input_tokens", 0))
                    main_today["cached_tokens"] += int(ms.get("today_cached_tokens", 0))
                    main_today["output_tokens"] += int(ms.get("today_output_tokens", 0))
                    main_today["thought_tokens"] += int(ms.get("today_thought_tokens", 0))
                    main_today["total_tokens"] += int(ms.get("today_total_tokens", 0))
                    new_u = round(
                        float(main_today["cost_usd"]) + float(ms.get("today_cost_usd", 0.0)), 6
                    )
                    main_today["cost_usd"] = new_u
                    main_today["today_cost_usd"] = new_u
                    main_today["cost_cny"] = round(
                        float(main_today["cost_cny"]) + float(ms.get("today_cost_cny", 0.0)), 6
                    )

            sub_today = _empty_agent_today()
            sub_all_cost_usd = 0.0
            sub_all_cost_cny = 0.0
            for ss in sub_sessions:
                aid = str(ss.get("agent_id") or "sub_0")
                if aid not in sessions_by_agent:
                    sessions_by_agent[aid] = []
                if aid not in today_by_agent:
                    today_by_agent[aid] = _empty_agent_today()
                sessions_by_agent[aid].append(dict(ss))

                s_tot_u = float(ss.get("total_cost_usd", ss.get("cost_usd", 0.0)) or 0.0)
                s_tot_c = float(ss.get("total_cost_cny", ss.get("cost_cny", 0.0)) or 0.0)
                sub_all_cost_usd = round(sub_all_cost_usd + s_tot_u, 6)
                sub_all_cost_cny = round(sub_all_cost_cny + s_tot_c, 6)
                sub_today["total_cost_usd"] = sub_all_cost_usd

                agent_stat = today_by_agent[aid]
                agent_stat["total_cost_usd"] = round(
                    float(agent_stat.get("total_cost_usd", 0.0)) + s_tot_u, 6
                )

                t_steps = int(ss.get("today_steps", 0))
                if t_steps > 0:
                    sub_today["sessions_count"] += 1
                    sub_today["steps_count"] += t_steps
                    sub_today["input_tokens"] += int(ss.get("today_input_tokens", 0))
                    sub_today["cached_tokens"] += int(ss.get("today_cached_tokens", 0))
                    sub_today["output_tokens"] += int(ss.get("today_output_tokens", 0))
                    sub_today["thought_tokens"] += int(ss.get("today_thought_tokens", 0))
                    sub_today["total_tokens"] += int(ss.get("today_total_tokens", 0))
                    new_sub_u = round(
                        float(sub_today["cost_usd"]) + float(ss.get("today_cost_usd", 0.0)), 6
                    )
                    sub_today["cost_usd"] = new_sub_u
                    sub_today["today_cost_usd"] = new_sub_u
                    sub_today["cost_cny"] = round(
                        float(sub_today["cost_cny"]) + float(ss.get("today_cost_cny", 0.0)), 6
                    )

                    agent_stat["sessions_count"] += 1
                    agent_stat["steps_count"] += t_steps
                    agent_stat["input_tokens"] += int(ss.get("today_input_tokens", 0))
                    agent_stat["cached_tokens"] += int(ss.get("today_cached_tokens", 0))
                    agent_stat["output_tokens"] += int(ss.get("today_output_tokens", 0))
                    agent_stat["thought_tokens"] += int(ss.get("today_thought_tokens", 0))
                    agent_stat["total_tokens"] += int(ss.get("today_total_tokens", 0))
                    new_ag_u = round(
                        float(agent_stat["cost_usd"]) + float(ss.get("today_cost_usd", 0.0)), 6
                    )
                    agent_stat["cost_usd"] = new_ag_u
                    agent_stat["today_cost_usd"] = new_ag_u
                    agent_stat["cost_cny"] = round(
                        float(agent_stat["cost_cny"]) + float(ss.get("today_cost_cny", 0.0)), 6
                    )

            for aid_list in sessions_by_agent.values():
                aid_list.sort(key=lambda x: x.get("last_updated", 0.0), reverse=True)

            combined_today_cost_usd = round(
                float(main_today["cost_usd"]) + float(sub_today["cost_usd"]), 6
            )
            combined_today_cost_cny = round(
                float(main_today["cost_cny"]) + float(sub_today["cost_cny"]), 6
            )
            combined_total_cost_usd = round(main_all_cost_usd + sub_all_cost_usd, 6)
            combined_total_cost_cny = round(main_all_cost_cny + sub_all_cost_cny, 6)

            today_stats = {
                "main_sessions_count": main_today["sessions_count"],
                "sub_sessions_count": sub_today["sessions_count"],
                "sessions_count": main_today["sessions_count"] + sub_today["sessions_count"],
                "main_steps_count": main_today["steps_count"],
                "sub_steps_count": sub_today["steps_count"],
                "steps_count": main_today["steps_count"] + sub_today["steps_count"],
                "main_total_tokens": main_today["total_tokens"],
                "sub_total_tokens": sub_today["total_tokens"],
                "main_cost_usd": round(float(main_today["cost_usd"]), 6),
                "sub_cost_usd": round(float(sub_today["cost_usd"]), 6),
                "main_total_cost_usd": main_all_cost_usd,
                "sub_total_cost_usd": sub_all_cost_usd,
                "input_tokens": main_today["input_tokens"] + sub_today["input_tokens"],
                "cached_tokens": main_today["cached_tokens"] + sub_today["cached_tokens"],
                "output_tokens": main_today["output_tokens"] + sub_today["output_tokens"],
                "thought_tokens": main_today["thought_tokens"] + sub_today["thought_tokens"],
                "total_tokens": main_today["total_tokens"] + sub_today["total_tokens"],
                "cost_usd": combined_today_cost_usd,
                "cost_cny": combined_today_cost_cny,
                "today_cost_usd": combined_today_cost_usd,
                "today_cost_cny": combined_today_cost_cny,
                "total_cost_usd": combined_total_cost_usd,
                "total_cost_cny": combined_total_cost_cny,
            }

            idle_sec = max(0.0, now - self.last_activity_time)
            is_backoff = idle_sec >= self.idle_backoff_sec

            snapshot = {
                "timestamp": now,
                "today_date": t_date.isoformat(),
                "last_activity_time": self.last_activity_time,
                "idle_seconds": idle_sec,
                "is_standby_backoff": is_backoff,
                "disk_modified": disk_modified,
                "midnight_reset_count": self.midnight_reset_count,
                "main_agent_valid": main_agent_valid,
                "main_agent_path": str(self.brain_root) if self.brain_root and str(self.brain_root) != "." else "",
                "main_session": main_active,
                "active_session": active_session,
                "all_main_sessions": [dict(s) for s in main_sessions],
                "all_sub_sessions": [dict(s) for s in sub_sessions],
                "sessions_by_agent": sessions_by_agent,
                "today_by_agent": today_by_agent,
                "today_stats": today_stats,
                "cost_usd": combined_today_cost_usd,
                "cost_cny": combined_today_cost_cny,
                "today_cost_usd": combined_today_cost_usd,
                "today_cost_cny": combined_today_cost_cny,
                "total_cost_usd": combined_total_cost_usd,
                "total_cost_cny": combined_total_cost_cny,
                "live_state": live_state if live_is_fresh else {},
            }
            self._last_snapshot = snapshot
            return snapshot


def export_history_to_csv(
    output_path: Path | str,
    history_path: Path | str | None = None,
    monitor: TokenMonitorCache | None = None,
    include_current: bool = True,
) -> Path:
    """
    模块级历史 CSV 导出入口函数：
    表头：Date, Agent, Model, InputTokens, CachedTokens, OutputTokens, ThoughtTokens, TotalTokens, CostUSD
    """
    mon = monitor if monitor is not None else TokenMonitorCache(history_path=history_path)
    return mon.export_history_csv(
        csv_path=output_path,
        history_path=history_path,
        include_current=include_current,
    )

