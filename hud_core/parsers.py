# -*- coding: utf-8 -*-
"""
hud_core.parsers - PARSER_REGISTRY 及 Antigravity / Gemini CLI / Claude Code 日志发现与增量字节流解析器。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import _probe_antigravity_brain_dir
from .pricing import TokenPricingEngine

_PRICING_ENGINE = TokenPricingEngine()


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


def _extract_user_prompt_first_line(content: Any) -> str:
    """从 Gemini CLI / Claude Code user 消息的 content 中提取首行有效文本（剥离 <session_context> 等头部标签）。"""
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


def _update_session_pricing(info: dict[str, Any]) -> None:
    """根据当前会话累计与今日累计 Token 刷新折算成本字段。"""
    model_name = str(info.get("model") or "")
    tot_cost = _PRICING_ENGINE.calculate_cost(
        input_tokens=int(info.get("input_tokens", 0) or 0),
        cached_tokens=int(info.get("cached_tokens", 0) or 0),
        output_tokens=int(info.get("output_tokens", 0) or 0),
        thought_tokens=int(info.get("thought_tokens", 0) or 0),
        model_name=model_name,
    )
    info["cost_usd"] = tot_cost["usd"]
    info["cost_cny"] = tot_cost["cny"]
    info["total_cost_usd"] = tot_cost["usd"]
    info["total_cost_cny"] = tot_cost["cny"]

    today_cost = _PRICING_ENGINE.calculate_cost(
        input_tokens=int(info.get("today_input_tokens", 0) or 0),
        cached_tokens=int(info.get("today_cached_tokens", 0) or 0),
        output_tokens=int(info.get("today_output_tokens", 0) or 0),
        thought_tokens=int(info.get("today_thought_tokens", 0) or 0),
        model_name=model_name,
    )
    info["today_cost_usd"] = today_cost["usd"]
    info["today_cost_cny"] = today_cost["cny"]


def reset_session_today_stats(
    info: dict[str, Any],
    state: dict[str, Any] | None = None,
    today_date: datetime.date | None = None,
) -> dict[str, Any]:
    """
    跨天（00:00）零 I/O 内存归零：
    将指定会话字典及增量缓存状态中的 today_* 统计与今日折算成本直接重置为 0，不触发磁盘读取。
    """
    target_date = today_date if today_date is not None else datetime.date.today()
    today_int_keys = (
        "today_steps",
        "today_input_tokens",
        "today_cached_tokens",
        "today_output_tokens",
        "today_thought_tokens",
        "today_tool_tokens",
        "today_total_tokens",
    )
    for k in today_int_keys:
        if k in info or k != "today_tool_tokens":
            info[k] = 0
    info["today_cost_usd"] = 0.0
    info["today_cost_cny"] = 0.0

    if isinstance(state, dict):
        state["today_date"] = target_date
        state["bytes_read_last"] = 0
        state_info = state.get("info")
        if isinstance(state_info, dict) and state_info is not info:
            for k in today_int_keys:
                if k in state_info or k != "today_tool_tokens":
                    state_info[k] = 0
            state_info["today_cost_usd"] = 0.0
            state_info["today_cost_cny"] = 0.0
    return info


def _read_jsonl_incremental_records(
    file_path: Path,
    start_offset: int = 0,
) -> tuple[list[dict[str, Any]], int, int]:
    """
    基于字节偏移量（Byte-Offset Tailing）读取 JSONL 新增记录：
    打开文件后通过 file.seek(start_offset) 仅读取末尾新增字节流并反序列化为 JSON 对象列表。
    返回: (records, new_byte_offset, bytes_read)
    """
    safe_offset = max(0, int(start_offset or 0))
    records: list[dict[str, Any]] = []
    consumed_offset = safe_offset
    bytes_read = 0

    with file_path.open("rb") as f:
        if safe_offset > 0:
            f.seek(safe_offset)
        chunk = f.read()
        bytes_read = len(chunk)
        if not chunk:
            return records, consumed_offset, 0

        raw_lines = chunk.splitlines(keepends=True)
        total_lines = len(raw_lines)
        for idx, raw_bytes in enumerate(raw_lines):
            line = raw_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                consumed_offset += len(raw_bytes)
                continue
            if not line.startswith("{"):
                if idx == total_lines - 1 and not raw_bytes.endswith((b"\n", b"\r")):
                    break
                consumed_offset += len(raw_bytes)
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                if idx == total_lines - 1 and not raw_bytes.endswith((b"\n", b"\r")):
                    # 末尾追加行尚未刷盘完毕，保留偏移量待下次读取
                    break
                consumed_offset += len(raw_bytes)
                continue

            consumed_offset += len(raw_bytes)
            if isinstance(obj, dict):
                records.append(obj)

    return records, consumed_offset, bytes_read


class AntigravityBrainParser:
    """主 Agent (Antigravity) transcript.jsonl 增量字节偏移解析器。"""

    parser_type = "antigravity_transcript"
    label = "Antigravity Transcript"

    @staticmethod
    def discover(base_path: Path) -> list[Path]:
        return _discover_antigravity_files(base_path)

    @staticmethod
    def _build_empty_info(file_path: Path) -> dict[str, Any]:
        cid = "unknown"
        try:
            cid = file_path.parent.parent.parent.name
        except Exception:
            pass
        short_cid = cid[:8] if len(cid) > 8 else cid
        return {
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
            "cost_usd": 0.0,
            "cost_cny": 0.0,
            "total_cost_usd": 0.0,
            "total_cost_cny": 0.0,
            "today_steps": 0,
            "today_input_tokens": 0,
            "today_cached_tokens": 0,
            "today_output_tokens": 0,
            "today_thought_tokens": 0,
            "today_total_tokens": 0,
            "today_cost_usd": 0.0,
            "today_cost_cny": 0.0,
            "last_byte_offset": 0,
        }

    @classmethod
    def create_state(
        cls,
        file_path: Path,
        today_date: datetime.date | None = None,
    ) -> dict[str, Any]:
        t_date = today_date if today_date is not None else datetime.date.today()
        return {
            "parser_type": cls.parser_type,
            "file_path": str(file_path),
            "last_byte_offset": 0,
            "mtime": 0.0,
            "size": 0,
            "today_date": t_date,
            "last_record_status": "DONE",
            "last_record_type": "",
            "has_pending_tool": False,
            "extracted_session_name": "",
            "steps_records": [],
            "info": cls._build_empty_info(file_path),
            "incremental_reads": 0,
            "full_reads": 0,
            "bytes_read_last": 0,
            "bytes_read_total": 0,
        }

    @classmethod
    def reset_state(
        cls,
        state: dict[str, Any],
        file_path: Path,
        today_date: datetime.date | None = None,
    ) -> dict[str, Any]:
        inc_reads = int(state.get("incremental_reads", 0) or 0)
        full_reads = int(state.get("full_reads", 0) or 0)
        bytes_total = int(state.get("bytes_read_total", 0) or 0)
        fresh = cls.create_state(file_path, today_date=today_date)
        fresh["incremental_reads"] = inc_reads
        fresh["full_reads"] = full_reads
        fresh["bytes_read_total"] = bytes_total
        state.clear()
        state.update(fresh)
        return state

    @classmethod
    def refresh_status(
        cls,
        state: dict[str, Any],
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        info = state["info"]
        now = now_ts if now_ts is not None else time.time()
        age = now - float(info.get("last_updated") or 0.0)
        last_record_status = str(state.get("last_record_status") or "DONE")
        last_record_type = str(state.get("last_record_type") or "")
        has_pending_tool = bool(state.get("has_pending_tool", False))

        if last_record_status == "RUNNING" and age < 120.0:
            info["status"] = "running"
        elif age < 30.0 and (last_record_type == "USER_INPUT" or has_pending_tool):
            info["status"] = "running"
        elif age < 10.0:
            info["status"] = "active"
        else:
            info["status"] = "completed"
        _update_session_pricing(info)
        return dict(info)

    @classmethod
    def parse(
        cls,
        file_path: Path,
        state: dict[str, Any] | None = None,
        mtime: float | None = None,
        size: int | None = None,
        today_date: datetime.date | None = None,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        t_date = today_date if today_date is not None else datetime.date.today()
        if state is None:
            state = cls.create_state(file_path, today_date=t_date)

        try:
            if mtime is None or size is None:
                st = file_path.stat()
                mtime = st.st_mtime
                size = st.st_size
        except OSError:
            return dict(state["info"])

        last_offset = int(state.get("last_byte_offset", 0) or 0)
        # 若文件变小（截断或重写），自动重置 last_byte_offset = 0 回退为全量重新解析
        if size < last_offset or last_offset < 0:
            cls.reset_state(state, file_path, today_date=t_date)
            last_offset = 0

        if state.get("today_date") != t_date:
            reset_session_today_stats(state["info"], state, today_date=t_date)

        info = state["info"]
        if last_offset == 0:
            info["last_updated"] = float(mtime)
        elif float(mtime) > float(info.get("last_updated") or 0.0):
            info["last_updated"] = float(mtime)

        try:
            records, new_offset, bytes_read = _read_jsonl_incremental_records(
                file_path, start_offset=last_offset
            )
        except Exception:
            return dict(info)

        if last_offset == 0:
            state["full_reads"] = int(state.get("full_reads", 0) or 0) + 1
        else:
            state["incremental_reads"] = int(state.get("incremental_reads", 0) or 0) + 1

        state["last_byte_offset"] = new_offset
        state["bytes_read_last"] = bytes_read
        state["bytes_read_total"] = int(state.get("bytes_read_total", 0) or 0) + bytes_read
        state["mtime"] = float(mtime)
        state["size"] = int(size)
        state["today_date"] = t_date

        for obj in records:
            created_ts = parse_iso_timestamp(str(obj.get("created_at") or ""))
            if created_ts > 0:
                if info["start_time"] <= 0:
                    info["start_time"] = created_ts
                if created_ts > info["last_updated"]:
                    info["last_updated"] = created_ts

            etype = str(obj.get("type") or "")
            state["last_record_type"] = etype
            state["last_record_status"] = str(obj.get("status") or "DONE")

            if etype == "USER_INPUT":
                state["has_pending_tool"] = False
                content_str = str(obj.get("content") or "")
                if not state["extracted_session_name"] and content_str:
                    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content_str, re.DOTALL)
                    req_body = m.group(1) if m else content_str
                    for req_line in req_body.splitlines():
                        s_line = req_line.strip()
                        if s_line and not s_line.startswith("<"):
                            state["extracted_session_name"] = _truncate_session_name(s_line, 30)
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

                rec_ts = created_ts if created_ts > 0 else float(mtime)
                is_today = bool(
                    rec_ts > 0 and datetime.datetime.fromtimestamp(rec_ts).date() == t_date
                )
                if is_today:
                    info["today_steps"] += 1
                    info["today_input_tokens"] += step_total_in
                    info["today_cached_tokens"] += cache_read
                    info["today_output_tokens"] += out_tok
                    info["today_thought_tokens"] += thought_tok
                    info["today_total_tokens"] += step_total

                state["steps_records"].append(
                    {
                        "timestamp": rec_ts,
                        "input_tokens": step_total_in,
                        "cached_tokens": cache_read,
                        "output_tokens": out_tok,
                        "thought_tokens": thought_tok,
                        "total_tokens": step_total,
                    }
                )

                t_calls = obj.get("tool_calls")
                if isinstance(t_calls, list) and t_calls:
                    info["tool_calls_count"] += len(t_calls)
                    state["has_pending_tool"] = True
                    last_tc = t_calls[-1]
                    if isinstance(last_tc, dict) and last_tc.get("name"):
                        info["last_tool"] = str(last_tc["name"])
                else:
                    state["has_pending_tool"] = False

        if state["extracted_session_name"]:
            info["session_name"] = state["extracted_session_name"]

        info["last_byte_offset"] = new_offset
        _update_session_pricing(info)
        return cls.refresh_status(state, now_ts=now_ts)


class GeminiCliSessionParser:
    """子 Agent (Gemini CLI) session-*.jsonl 增量字节偏移解析器。"""

    parser_type = "gemini_cli_session"
    label = "Gemini CLI Session"

    @staticmethod
    def discover(base_path: Path) -> list[Path]:
        return _discover_gemini_cli_files(base_path)

    @staticmethod
    def _build_empty_info(file_path: Path) -> dict[str, Any]:
        project_name = file_path.parent.parent.name if file_path.parent.name == "chats" else "unknown"
        return {
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
            "cost_usd": 0.0,
            "cost_cny": 0.0,
            "total_cost_usd": 0.0,
            "total_cost_cny": 0.0,
            "today_steps": 0,
            "today_input_tokens": 0,
            "today_cached_tokens": 0,
            "today_output_tokens": 0,
            "today_thought_tokens": 0,
            "today_tool_tokens": 0,
            "today_total_tokens": 0,
            "today_cost_usd": 0.0,
            "today_cost_cny": 0.0,
            "last_byte_offset": 0,
        }

    @classmethod
    def create_state(
        cls,
        file_path: Path,
        today_date: datetime.date | None = None,
    ) -> dict[str, Any]:
        t_date = today_date if today_date is not None else datetime.date.today()
        return {
            "parser_type": cls.parser_type,
            "file_path": str(file_path),
            "last_byte_offset": 0,
            "mtime": 0.0,
            "size": 0,
            "today_date": t_date,
            "turns_by_id": {},
            "last_event_type": "",
            "has_active_tool": False,
            "topic_title": "",
            "first_user_line": "",
            "info": cls._build_empty_info(file_path),
            "incremental_reads": 0,
            "full_reads": 0,
            "bytes_read_last": 0,
            "bytes_read_total": 0,
        }

    @classmethod
    def reset_state(
        cls,
        state: dict[str, Any],
        file_path: Path,
        today_date: datetime.date | None = None,
    ) -> dict[str, Any]:
        inc_reads = int(state.get("incremental_reads", 0) or 0)
        full_reads = int(state.get("full_reads", 0) or 0)
        bytes_total = int(state.get("bytes_read_total", 0) or 0)
        fresh = cls.create_state(file_path, today_date=today_date)
        fresh["incremental_reads"] = inc_reads
        fresh["full_reads"] = full_reads
        fresh["bytes_read_total"] = bytes_total
        state.clear()
        state.update(fresh)
        return state

    @staticmethod
    def _apply_turn_delta(
        info: dict[str, Any],
        turn_entry: dict[str, Any],
        sign: int,
        today_date: datetime.date,
    ) -> None:
        if bool(turn_entry.get("has_step")):
            in_t = int(turn_entry.get("in_t", 0))
            cached_t = int(turn_entry.get("cached_t", 0))
            out_t = int(turn_entry.get("out_t", 0))
            thought_t = int(turn_entry.get("thought_t", 0))
            tool_t = int(turn_entry.get("tool_t", 0))
            tot_t = int(turn_entry.get("tot_t", 0))

            info["steps"] += sign * 1
            info["input_tokens"] += sign * in_t
            info["cached_tokens"] += sign * cached_t
            info["output_tokens"] += sign * out_t
            info["thought_tokens"] += sign * thought_t
            info["tool_tokens"] += sign * tool_t
            info["total_tokens"] += sign * tot_t

            counted_date = turn_entry.get("today_date_counted")
            if counted_date == today_date:
                info["today_steps"] = max(0, info["today_steps"] + sign * 1)
                info["today_input_tokens"] = max(0, info["today_input_tokens"] + sign * in_t)
                info["today_cached_tokens"] = max(0, info["today_cached_tokens"] + sign * cached_t)
                info["today_output_tokens"] = max(0, info["today_output_tokens"] + sign * out_t)
                info["today_thought_tokens"] = max(0, info["today_thought_tokens"] + sign * thought_t)
                info["today_tool_tokens"] = max(0, info["today_tool_tokens"] + sign * tool_t)
                info["today_total_tokens"] = max(0, info["today_total_tokens"] + sign * tot_t)

        info["tool_calls_count"] += sign * int(turn_entry.get("tool_calls", 0))

    @classmethod
    def refresh_status(
        cls,
        state: dict[str, Any],
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        info = state["info"]
        now = now_ts if now_ts is not None else time.time()
        age = now - float(info.get("last_updated") or 0.0)
        last_event_type = str(state.get("last_event_type") or "")
        has_active_tool = bool(state.get("has_active_tool", False))

        if age < 30.0 and (last_event_type == "user" or has_active_tool):
            info["status"] = "running"
        elif age < 8.0:
            info["status"] = "active"
        else:
            info["status"] = "completed"
        _update_session_pricing(info)
        return dict(info)

    @classmethod
    def parse(
        cls,
        file_path: Path,
        state: dict[str, Any] | None = None,
        mtime: float | None = None,
        size: int | None = None,
        today_date: datetime.date | None = None,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        t_date = today_date if today_date is not None else datetime.date.today()
        if state is None:
            state = cls.create_state(file_path, today_date=t_date)

        try:
            if mtime is None or size is None:
                st = file_path.stat()
                mtime = st.st_mtime
                size = st.st_size
        except OSError:
            return dict(state["info"])

        last_offset = int(state.get("last_byte_offset", 0) or 0)
        if size < last_offset or last_offset < 0:
            cls.reset_state(state, file_path, today_date=t_date)
            last_offset = 0

        if state.get("today_date") != t_date:
            reset_session_today_stats(state["info"], state, today_date=t_date)

        info = state["info"]
        if last_offset == 0:
            info["last_updated"] = float(mtime)
        elif float(mtime) > float(info.get("last_updated") or 0.0):
            info["last_updated"] = float(mtime)

        try:
            records, new_offset, bytes_read = _read_jsonl_incremental_records(
                file_path, start_offset=last_offset
            )
        except Exception:
            return dict(info)

        if last_offset == 0:
            state["full_reads"] = int(state.get("full_reads", 0) or 0) + 1
        else:
            state["incremental_reads"] = int(state.get("incremental_reads", 0) or 0) + 1

        state["last_byte_offset"] = new_offset
        state["bytes_read_last"] = bytes_read
        state["bytes_read_total"] = int(state.get("bytes_read_total", 0) or 0) + bytes_read
        state["mtime"] = float(mtime)
        state["size"] = int(size)
        state["today_date"] = t_date

        turns_by_id: dict[str, dict[str, Any]] = state["turns_by_id"]

        for obj in records:
            if "sessionId" in obj and not info["session_id"]:
                info["session_id"] = str(obj.get("sessionId", ""))
                st_ts = parse_iso_timestamp(str(obj.get("startTime", "")))
                if st_ts > 0:
                    info["start_time"] = st_ts

            if "$set" in obj and isinstance(obj["$set"], dict):
                lu = parse_iso_timestamp(str(obj["$set"].get("lastUpdated", "")))
                if lu > info["last_updated"]:
                    info["last_updated"] = lu
                continue

            rec_ts = parse_iso_timestamp(str(obj.get("timestamp") or obj.get("created_at") or ""))
            if rec_ts > 0:
                if info["start_time"] <= 0:
                    info["start_time"] = rec_ts
                if rec_ts > info["last_updated"]:
                    info["last_updated"] = rec_ts

            etype = obj.get("type") or obj.get("role")
            if etype == "user":
                state["last_event_type"] = "user"
                state["has_active_tool"] = False
                if not state["first_user_line"]:
                    state["first_user_line"] = _extract_user_prompt_first_line(obj.get("content"))
            elif etype == "gemini":
                state["last_event_type"] = "gemini"
                msg_id = str(obj.get("id") or f"anon_{len(turns_by_id)}")
                model_name = str(obj.get("model") or "")
                if model_name:
                    info["model"] = model_name

                tok_obj = obj.get("tokens")
                t_calls = obj.get("toolCalls")
                if isinstance(t_calls, list) and t_calls:
                    state["has_active_tool"] = True
                    last_tc = t_calls[-1]
                    if isinstance(last_tc, dict) and last_tc.get("name"):
                        info["last_tool"] = str(last_tc["name"])
                    for tc in t_calls:
                        if isinstance(tc, dict) and tc.get("name") == "update_topic":
                            args_obj = tc.get("args")
                            if isinstance(args_obj, dict) and args_obj.get("title"):
                                state["topic_title"] = _truncate_session_name(
                                    str(args_obj["title"]), 30
                                )
                else:
                    state["has_active_tool"] = False

                prev = turns_by_id.get(msg_id)
                if prev is not None:
                    cls._apply_turn_delta(info, prev, -1, t_date)

                eff_tokens = (
                    tok_obj
                    if isinstance(tok_obj, dict)
                    else (prev.get("tokens", {}) if prev else {})
                )
                eff_tool_calls = (
                    len(t_calls)
                    if isinstance(t_calls, list)
                    else int(prev.get("tool_calls", 0) if prev else 0)
                )
                eff_ts = (
                    rec_ts
                    if rec_ts > 0
                    else float(prev.get("timestamp", mtime) if prev else mtime)
                )

                in_t = int(eff_tokens.get("input", 0) or 0)
                out_t = int(eff_tokens.get("output", 0) or 0)
                cached_t = int(eff_tokens.get("cached", 0) or 0)
                thought_t = int(eff_tokens.get("thoughts", 0) or 0)
                tool_t = int(eff_tokens.get("tool", 0) or 0)
                tot_t = int(eff_tokens.get("total", 0) or (in_t + out_t + thought_t + tool_t))
                has_step = bool(tot_t > 0 or in_t > 0 or out_t > 0)
                is_today = bool(
                    eff_ts > 0 and datetime.datetime.fromtimestamp(eff_ts).date() == t_date
                )

                new_turn = {
                    "tokens": eff_tokens,
                    "tool_calls": eff_tool_calls,
                    "timestamp": eff_ts,
                    "in_t": in_t,
                    "out_t": out_t,
                    "cached_t": cached_t,
                    "thought_t": thought_t,
                    "tool_t": tool_t,
                    "tot_t": tot_t,
                    "has_step": has_step,
                    "today_date_counted": t_date if is_today else None,
                }
                turns_by_id[msg_id] = new_turn
                cls._apply_turn_delta(info, new_turn, +1, t_date)
                if has_step:
                    info["context_tokens"] = in_t

        project_name = info.get("project") or "unknown"
        short_sid = info["session_id"][:8] if info["session_id"] else ""
        if state["topic_title"]:
            info["session_name"] = state["topic_title"]
        elif state["first_user_line"]:
            info["session_name"] = state["first_user_line"]
        elif project_name and project_name != "unknown":
            info["session_name"] = project_name
        elif short_sid:
            info["session_name"] = short_sid

        info["last_byte_offset"] = new_offset
        _update_session_pricing(info)
        return cls.refresh_status(state, now_ts=now_ts)


class ClaudeCodeSessionParser:
    """Claude Code 会话 *.jsonl 增量字节偏移解析器。"""

    parser_type = "claude_code_session"
    label = "Claude Code Session"

    @staticmethod
    def discover(base_path: Path) -> list[Path]:
        return _discover_claude_code_files(base_path)

    @staticmethod
    def _build_empty_info(file_path: Path) -> dict[str, Any]:
        default_sid = file_path.stem or "unknown"
        project_name = file_path.parent.name if file_path.parent.name else "Claude Code"
        short_default = default_sid[:8] if len(default_sid) > 8 else default_sid
        return {
            "file_path": str(file_path),
            "project": project_name,
            "session_id": default_sid,
            "session_name": short_default,
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
            "cost_usd": 0.0,
            "cost_cny": 0.0,
            "total_cost_usd": 0.0,
            "total_cost_cny": 0.0,
            "today_steps": 0,
            "today_input_tokens": 0,
            "today_cached_tokens": 0,
            "today_output_tokens": 0,
            "today_thought_tokens": 0,
            "today_tool_tokens": 0,
            "today_total_tokens": 0,
            "today_cost_usd": 0.0,
            "today_cost_cny": 0.0,
            "last_byte_offset": 0,
        }

    @classmethod
    def create_state(
        cls,
        file_path: Path,
        today_date: datetime.date | None = None,
    ) -> dict[str, Any]:
        t_date = today_date if today_date is not None else datetime.date.today()
        return {
            "parser_type": cls.parser_type,
            "file_path": str(file_path),
            "last_byte_offset": 0,
            "mtime": 0.0,
            "size": 0,
            "today_date": t_date,
            "turns_list": [],
            "turns_by_id_idx": {},
            "explicit_session_id": "",
            "explicit_session_name": "",
            "first_user_line": "",
            "last_event_type": "",
            "has_active_tool": False,
            "info": cls._build_empty_info(file_path),
            "incremental_reads": 0,
            "full_reads": 0,
            "bytes_read_last": 0,
            "bytes_read_total": 0,
        }

    @classmethod
    def reset_state(
        cls,
        state: dict[str, Any],
        file_path: Path,
        today_date: datetime.date | None = None,
    ) -> dict[str, Any]:
        inc_reads = int(state.get("incremental_reads", 0) or 0)
        full_reads = int(state.get("full_reads", 0) or 0)
        bytes_total = int(state.get("bytes_read_total", 0) or 0)
        fresh = cls.create_state(file_path, today_date=today_date)
        fresh["incremental_reads"] = inc_reads
        fresh["full_reads"] = full_reads
        fresh["bytes_read_total"] = bytes_total
        state.clear()
        state.update(fresh)
        return state

    @staticmethod
    def _apply_step_delta(
        info: dict[str, Any],
        step: dict[str, Any],
        sign: int,
        today_date: datetime.date,
    ) -> None:
        in_t = int(step["input_tokens"])
        cached_t = int(step["cached_tokens"])
        out_t = int(step["output_tokens"])
        thought_t = int(step["thought_tokens"])
        tool_t = int(step["tool_tokens"])
        tot_t = int(step["total_tokens"])

        info["steps"] += sign * 1
        info["input_tokens"] += sign * in_t
        info["cached_tokens"] += sign * cached_t
        info["output_tokens"] += sign * out_t
        info["thought_tokens"] += sign * thought_t
        info["tool_tokens"] += sign * tool_t
        info["total_tokens"] += sign * tot_t
        info["tool_calls_count"] += sign * int(step.get("tool_calls", 0))

        if step.get("today_date_counted") == today_date:
            info["today_steps"] = max(0, info["today_steps"] + sign * 1)
            info["today_input_tokens"] = max(0, info["today_input_tokens"] + sign * in_t)
            info["today_cached_tokens"] = max(0, info["today_cached_tokens"] + sign * cached_t)
            info["today_output_tokens"] = max(0, info["today_output_tokens"] + sign * out_t)
            info["today_thought_tokens"] = max(0, info["today_thought_tokens"] + sign * thought_t)
            info["today_tool_tokens"] = max(0, info["today_tool_tokens"] + sign * tool_t)
            info["today_total_tokens"] = max(0, info["today_total_tokens"] + sign * tot_t)

    @classmethod
    def refresh_status(
        cls,
        state: dict[str, Any],
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        info = state["info"]
        now = now_ts if now_ts is not None else time.time()
        age = now - float(info.get("last_updated") or 0.0)
        last_event_type = str(state.get("last_event_type") or "")
        has_active_tool = bool(state.get("has_active_tool", False))

        if age < 30.0 and (last_event_type == "user" or has_active_tool):
            info["status"] = "running"
        elif age < 8.0:
            info["status"] = "active"
        else:
            info["status"] = "completed"
        _update_session_pricing(info)
        return dict(info)

    @classmethod
    def parse(
        cls,
        file_path: Path,
        state: dict[str, Any] | None = None,
        mtime: float | None = None,
        size: int | None = None,
        today_date: datetime.date | None = None,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        t_date = today_date if today_date is not None else datetime.date.today()
        if state is None:
            state = cls.create_state(file_path, today_date=t_date)

        try:
            if mtime is None or size is None:
                st = file_path.stat()
                mtime = st.st_mtime
                size = st.st_size
        except OSError:
            return dict(state["info"])

        last_offset = int(state.get("last_byte_offset", 0) or 0)
        if size < last_offset or last_offset < 0:
            cls.reset_state(state, file_path, today_date=t_date)
            last_offset = 0

        if state.get("today_date") != t_date:
            reset_session_today_stats(state["info"], state, today_date=t_date)

        info = state["info"]
        if last_offset == 0:
            info["last_updated"] = float(mtime)
        elif float(mtime) > float(info.get("last_updated") or 0.0):
            info["last_updated"] = float(mtime)

        try:
            records, new_offset, bytes_read = _read_jsonl_incremental_records(
                file_path, start_offset=last_offset
            )
        except Exception:
            return dict(info)

        if last_offset == 0:
            state["full_reads"] = int(state.get("full_reads", 0) or 0) + 1
        else:
            state["incremental_reads"] = int(state.get("incremental_reads", 0) or 0) + 1

        state["last_byte_offset"] = new_offset
        state["bytes_read_last"] = bytes_read
        state["bytes_read_total"] = int(state.get("bytes_read_total", 0) or 0) + bytes_read
        state["mtime"] = float(mtime)
        state["size"] = int(size)
        state["today_date"] = t_date

        turns_list: list[dict[str, Any]] = state["turns_list"]
        turns_by_id_idx: dict[str, int] = state["turns_by_id_idx"]

        for obj in records:
            sid_cand = str(obj.get("sessionId") or obj.get("session_id") or "").strip()
            if sid_cand and not state["explicit_session_id"]:
                state["explicit_session_id"] = sid_cand

            sname_cand = str(
                obj.get("session_name")
                or obj.get("sessionName")
                or obj.get("summary")
                or obj.get("title")
                or ""
            ).strip()
            if sname_cand and not state["explicit_session_name"]:
                state["explicit_session_name"] = _truncate_session_name(sname_cand, 30)

            st_val = obj.get("startTime") or obj.get("start_time")
            if st_val is not None:
                st_ts = (
                    float(st_val)
                    if isinstance(st_val, (int, float))
                    else parse_iso_timestamp(str(st_val))
                )
                if st_ts > 0 and info["start_time"] <= 0:
                    info["start_time"] = st_ts

            raw_ts = (
                obj.get("timestamp")
                or obj.get("created_at")
                or obj.get("createdAt")
                or obj.get("time")
            )
            rec_ts = 0.0
            if isinstance(raw_ts, (int, float)):
                rec_ts = float(raw_ts)
                if rec_ts > 1e12:
                    rec_ts /= 1000.0
            elif raw_ts:
                rec_ts = parse_iso_timestamp(str(raw_ts))

            if rec_ts > 0:
                if info["start_time"] <= 0:
                    info["start_time"] = rec_ts
                if rec_ts > info["last_updated"]:
                    info["last_updated"] = rec_ts

            msg_obj = obj.get("message") if isinstance(obj.get("message"), dict) else None
            etype = str(
                obj.get("type")
                or obj.get("role")
                or (msg_obj.get("role") if msg_obj else "")
                or ""
            ).lower()

            model_cand = str(
                obj.get("model") or (msg_obj.get("model") if msg_obj else "") or ""
            ).strip()
            if model_cand and model_cand != "<synthetic>":
                info["model"] = model_cand

            content_val = (
                msg_obj.get("content")
                if (msg_obj and "content" in msg_obj)
                else obj.get("content")
            )
            if etype in ("user", "human"):
                state["last_event_type"] = "user"
                state["has_active_tool"] = False
                if not state["first_user_line"] and content_val is not None:
                    state["first_user_line"] = _extract_user_prompt_first_line(content_val)
            elif etype in ("assistant", "model", "claude"):
                state["last_event_type"] = "assistant"

            tool_calls_in_step = 0
            last_tool_in_step = ""
            if isinstance(content_val, list):
                for part in content_val:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        tool_calls_in_step += 1
                        if part.get("name"):
                            last_tool_in_step = str(part["name"])
            t_calls = obj.get("toolCalls") or obj.get("tool_calls")
            if isinstance(t_calls, list) and t_calls:
                tool_calls_in_step += len(t_calls)
                last_tc = t_calls[-1]
                if isinstance(last_tc, dict) and last_tc.get("name"):
                    last_tool_in_step = str(last_tc["name"])

            if tool_calls_in_step > 0:
                state["has_active_tool"] = True
                if last_tool_in_step:
                    info["last_tool"] = last_tool_in_step
            elif etype in ("assistant", "model", "claude"):
                state["has_active_tool"] = False

            usage_src: dict[str, Any] | None = None
            if msg_obj and isinstance(msg_obj.get("usage"), dict):
                usage_src = msg_obj["usage"]
            elif isinstance(obj.get("usage"), dict):
                usage_src = obj["usage"]
            elif isinstance(obj.get("tokens"), dict):
                usage_src = obj["tokens"]
            elif any(
                k in obj
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cached_tokens",
                    "total_tokens",
                )
            ):
                usage_src = obj

            if usage_src is not None:
                in_raw = int(
                    usage_src.get("input_tokens", usage_src.get("input", 0)) or 0
                )
                cache_read = int(
                    usage_src.get(
                        "cache_read_input_tokens",
                        usage_src.get(
                            "cached_tokens",
                            usage_src.get(
                                "cache_read_tokens", usage_src.get("cached", 0)
                            ),
                        ),
                    )
                    or 0
                )
                cache_create = int(
                    usage_src.get("cache_creation_input_tokens", 0) or 0
                )
                out_t = int(
                    usage_src.get("output_tokens", usage_src.get("output", 0)) or 0
                )
                thought_t = int(
                    usage_src.get(
                        "thinking_tokens",
                        usage_src.get(
                            "thought_tokens", usage_src.get("thoughts", 0)
                        ),
                    )
                    or 0
                )
                tool_t = int(
                    usage_src.get("tool_tokens", usage_src.get("tool", 0)) or 0
                )

                if "cache_read_input_tokens" in usage_src or "cache_creation_input_tokens" in usage_src:
                    in_t = in_raw + cache_read + cache_create
                else:
                    in_t = in_raw

                explicit_tot = int(
                    usage_src.get("total_tokens", usage_src.get("total", 0)) or 0
                )
                tot_t = (
                    explicit_tot
                    if explicit_tot > 0
                    else (in_t + out_t + thought_t + tool_t)
                )

                if tot_t > 0 or in_t > 0 or out_t > 0 or cache_read > 0:
                    msg_id = str(
                        (msg_obj.get("id") if msg_obj else "")
                        or obj.get("messageId")
                        or obj.get("id")
                        or ""
                    ).strip()
                    step_ts = rec_ts if rec_ts > 0 else float(mtime)
                    is_today = bool(
                        step_ts > 0 and datetime.datetime.fromtimestamp(step_ts).date() == t_date
                    )
                    step_record = {
                        "input_tokens": in_t,
                        "cached_tokens": cache_read,
                        "output_tokens": out_t,
                        "thought_tokens": thought_t,
                        "tool_tokens": tool_t,
                        "total_tokens": tot_t,
                        "tool_calls": tool_calls_in_step,
                        "timestamp": step_ts,
                        "today_date_counted": t_date if is_today else None,
                    }
                    if msg_id and msg_id in turns_by_id_idx:
                        idx = turns_by_id_idx[msg_id]
                        cls._apply_step_delta(info, turns_list[idx], -1, t_date)
                        turns_list[idx] = step_record
                    else:
                        if msg_id:
                            turns_by_id_idx[msg_id] = len(turns_list)
                        turns_list.append(step_record)
                    cls._apply_step_delta(info, step_record, +1, t_date)
                    info["context_tokens"] = int(turns_list[-1]["input_tokens"])
            elif tool_calls_in_step > 0:
                info["tool_calls_count"] += tool_calls_in_step

        default_sid = file_path.stem or "unknown"
        short_default = default_sid[:8] if len(default_sid) > 8 else default_sid
        if state["explicit_session_id"]:
            info["session_id"] = state["explicit_session_id"]
        short_sid = info["session_id"][:8] if info["session_id"] else short_default

        if state["explicit_session_name"]:
            info["session_name"] = state["explicit_session_name"]
        elif state["first_user_line"]:
            info["session_name"] = state["first_user_line"]
        elif short_sid:
            info["session_name"] = short_sid

        if not info["model"]:
            info["model"] = "claude-code"
        if info["start_time"] <= 0:
            info["start_time"] = info["last_updated"]

        info["last_byte_offset"] = new_offset
        _update_session_pricing(info)
        return cls.refresh_status(state, now_ts=now_ts)


def parse_antigravity_transcript(
    file_path: Path,
    state: dict[str, Any] | None = None,
    mtime: float | None = None,
    size: int | None = None,
    today_date: datetime.date | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """解析主 Agent (Antigravity) 的 transcript.jsonl 日志文件（支持增量字节偏移状态）。"""
    return AntigravityBrainParser.parse(
        file_path,
        state=state,
        mtime=mtime,
        size=size,
        today_date=today_date,
        now_ts=now_ts,
    )


def parse_session_jsonl(
    file_path: Path,
    state: dict[str, Any] | None = None,
    mtime: float | None = None,
    size: int | None = None,
    today_date: datetime.date | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """解析单个子 Agent (Gemini CLI) session-*.jsonl 文件（支持增量字节偏移状态）。"""
    return GeminiCliSessionParser.parse(
        file_path,
        state=state,
        mtime=mtime,
        size=size,
        today_date=today_date,
        now_ts=now_ts,
    )


def parse_claude_code_session(
    file_path: Path,
    state: dict[str, Any] | None = None,
    mtime: float | None = None,
    size: int | None = None,
    today_date: datetime.date | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """解析单个 Claude Code 会话 *.jsonl 文件（支持增量字节偏移状态）。"""
    return ClaudeCodeSessionParser.parse(
        file_path,
        state=state,
        mtime=mtime,
        size=size,
        today_date=today_date,
        now_ts=now_ts,
    )


def _discover_antigravity_files(base_path: Path) -> list[Path]:
    """发现 Antigravity 会话 transcript.jsonl 日志文件列表。"""
    resolved, _ = _probe_antigravity_brain_dir(base_path)
    target_dir = resolved if resolved is not None else base_path
    if (target_dir / ".system_generated" / "logs" / "transcript.jsonl").is_file():
        return [target_dir / ".system_generated" / "logs" / "transcript.jsonl"]
    found = list(target_dir.glob("*/.system_generated/logs/transcript.jsonl"))
    if found:
        return found
    return list(target_dir.glob("**/transcript.jsonl"))


def _discover_gemini_cli_files(base_path: Path) -> list[Path]:
    """发现 Gemini CLI 会话 session-*.jsonl 日志文件列表。"""
    p = base_path / "tmp" if (base_path / "tmp").is_dir() else base_path
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


def _discover_claude_code_files(base_path: Path) -> list[Path]:
    """发现 Claude Code 会话 *.jsonl 日志文件列表。"""
    p = base_path.expanduser()
    if p.is_file():
        return [p]
    if not p.is_dir():
        return []
    found: list[Path] = []
    seen: set[str] = set()

    def _collect(paths: list[Path]) -> None:
        for item in paths:
            if item.is_file():
                key = os.path.normcase(os.path.normpath(str(item)))
                if key not in seen:
                    seen.add(key)
                    found.append(item)

    if (p / "projects").is_dir():
        _collect(list((p / "projects").glob("**/*.jsonl")))
    if (p / "history").is_dir():
        _collect(list((p / "history").glob("*.jsonl")))
    _collect(list(p.glob("*.jsonl")))
    if not found:
        _collect(list(p.glob("**/*.jsonl")))
    return found


PARSER_REGISTRY: dict[str, dict[str, Any]] = {
    "antigravity_transcript": {
        "label": "Antigravity Transcript",
        "discover": _discover_antigravity_files,
        "parse": parse_antigravity_transcript,
        "parser_cls": AntigravityBrainParser,
    },
    "gemini_cli_session": {
        "label": "Gemini CLI Session",
        "discover": _discover_gemini_cli_files,
        "parse": parse_session_jsonl,
        "parser_cls": GeminiCliSessionParser,
    },
    "claude_code_session": {
        "label": "Claude Code Session",
        "discover": _discover_claude_code_files,
        "parse": parse_claude_code_session,
        "parser_cls": ClaudeCodeSessionParser,
    },
}
