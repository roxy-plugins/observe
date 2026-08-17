from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

from agent.plugins import MobileUiContribution, MobileUiNavigation, Plugin
from agent.plugins.mobile_ui import MobileUiRpcInvalidRequest
from bus.events_lifecycle import ProactiveFinished, TurnCommitted
from core.memory.events import MemoryWritten, RetrievalCompleted

from .collector import GlobalErrorCollector
from .dashboard import ObserveDashboardReader
from .mobile_kvcache import KVCacheDashboardReader
from .retention import run_retention_if_needed
from .usage import NormalizedUsage, normalize_model_usage
from .writer import TraceWriter

logger = logging.getLogger("plugin.observe")


@runtime_checkable
class _ObserveWriter(Protocol):
    def emit(self, event: object) -> None: ...


class ObservePlugin(Plugin):
    api_version = 2

    @classmethod
    def dashboard_module(cls) -> str | None:
        return "dashboard.py"

    @classmethod
    def mobile_ui(cls) -> MobileUiContribution:
        return MobileUiContribution(
            module="mobile_panel.js",
            stylesheet="mobile_panel.css",
            navigation=MobileUiNavigation(
                label="Roxy Observe",
                description="Roxy 缓存效率与运行健康",
            ),
            slots=("turn.after_answer",),
        )

    name = "observe"
    version = "1.3.0"

    def activate(self) -> None:
        workspace = self.context.workspace
        if workspace is None:
            logger.warning("observe 插件缺少 workspace，跳过加载")
            return

        self._writer = TraceWriter(workspace / "observe" / "observe.db")
        self._writer_task = self.context.create_task(
            self._writer.run(),
            name="observe_writer",
        )
        self._retention_task = self.context.create_task(
            run_retention_if_needed(workspace / "observe" / "observe.db"),
            name="observe_retention",
        )
        self._collector = GlobalErrorCollector(
            self._writer,
            create_task=self.context.create_task,
        )
        self._collector.install()
        self.context.event_bus.on(TurnCommitted, self._observe_turn_committed)
        self.context.event_bus.on(ProactiveFinished, self._observe_proactive_finished)
        self.context.event_bus.on(RetrievalCompleted, self._observe_retrieval)
        self.context.event_bus.on(MemoryWritten, self._observe_memory_written)

    async def terminate(self) -> None:
        collector = getattr(self, "_collector", None)
        if collector is not None:
            await collector.uninstall()
        for task in (
            getattr(self, "_retention_task", None),
            getattr(self, "_writer_task", None),
        ):
            if task is None:
                continue
            _ = task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def mobile_ui_query(
        self,
        method: str,
        payload: dict[str, object],
        *,
        session_id: str | None,
        turn_id: str | None,
    ) -> dict[str, object]:
        """返回 Observe 自有的移动端只读投影。"""

        # 1. 在插件 RPC 边界校验方法与查询参数
        if method not in {
            "kvcache.bootstrap",
            "kvcache.message_usage",
            "health.snapshot",
            "health.error_detail",
        }:
            raise MobileUiRpcInvalidRequest(f"未知 observe 移动方法: {method}")
        workspace = self.context.workspace
        if workspace is None:
            raise RuntimeError("observe 移动看板缺少 workspace")
        if method.startswith("health."):
            return self._mobile_health_query(method, payload, workspace)
        reader = KVCacheDashboardReader(workspace)
        if method == "kvcache.bootstrap":
            return cast("dict[str, object]", reader.get_bootstrap())
        if method == "kvcache.message_usage":
            message_id = _required_mobile_string(payload, "message_id")
            if session_id is None:
                raise MobileUiRpcInvalidRequest("kvcache.message_usage 缺少 session_id")
            usage = reader.get_message_usage(
                message_id=message_id,
                session_key=session_id,
            )
            return {"usage": usage}
        raise AssertionError(f"未处理的 observe 移动方法: {method}")

    def _mobile_health_query(
        self,
        method: str,
        payload: dict[str, object],
        workspace: Path,
    ) -> dict[str, object]:
        """把 Observe 错误聚合裁成手机排障所需的只读投影。"""

        # 1. 复用桌面聚合 owner，只在 RPC 边界限制时间范围和载荷体积
        range_token = _mobile_range_value(payload)
        reader = ObserveDashboardReader(workspace)
        if method == "health.snapshot":
            result = reader.get_mobile_global_health(range_token, limit=50)
            raw_items = cast("list[dict[str, object]]", result["items"])
            return {
                "range": range_token,
                "items": cast(
                    "list[object]",
                    [_mobile_error_summary(item) for item in raw_items],
                ),
                "types": int(result["types"]),
                "total": int(result["total"]),
                "new_types": int(result["new_types"]),
                "spiking_types": int(result["spiking_types"]),
            }

        # 2. 详情按用户展开时再读取，列表不搬运 traceback 和 occurrence
        fingerprint = _required_mobile_string(payload, "fingerprint")
        detail = reader.get_mobile_global_detail(fingerprint, range_token)
        if not detail:
            return {"error": None}
        return {"error": _mobile_error_detail(detail)}

    def _observe_turn_committed(self, event: TurnCommitted) -> None:
        writer = getattr(self, "_writer", None)
        if not isinstance(writer, _ObserveWriter):
            return
        _emit_turn_trace(writer, event)

    def _observe_retrieval(self, event: RetrievalCompleted) -> None:
        writer = getattr(self, "_writer", None)
        if not isinstance(writer, _ObserveWriter):
            return
        writer.emit(_to_rag_query_log(event))

    def _observe_proactive_finished(self, event: ProactiveFinished) -> None:
        writer = getattr(self, "_writer", None)
        if not isinstance(writer, _ObserveWriter):
            return
        writer.emit(_to_proactive_turn_trace(event))

    def _observe_memory_written(self, event: MemoryWritten) -> None:
        writer = getattr(self, "_writer", None)
        if not isinstance(writer, _ObserveWriter):
            return
        writer.emit(_to_memory_write_trace(event))


def _emit_turn_trace(writer: _ObserveWriter, event: TurnCommitted) -> None:
    from .events import TurnTrace as TurnTraceEvent

    post_reply_budget = event.post_reply_budget
    react_stats = event.react_stats
    usage = normalize_model_usage(event.model_usage)
    tool_chain = event.tool_chain_raw
    tool_chain_json = (
        json.dumps(_slim_tool_chain(tool_chain), ensure_ascii=False)
        if tool_chain
        else None
    )
    tool_calls = _slim_tool_calls(tool_chain)
    writer.emit(
        TurnTraceEvent(
            source="agent",
            session_key=event.session_key,
            channel=event.channel,
            turn_id=event.turn_id or None,
            assistant_message_id=event.assistant_message_id,
            user_msg=event.persisted_user_message,
            llm_output=event.assistant_response,
            raw_llm_output=event.raw_reply,
            meme_tag=event.meme_tag,
            meme_media_count=event.meme_media_count,
            tool_calls=tool_calls,
            tool_chain_json=tool_chain_json,
            history_window=post_reply_budget.get("history_window"),
            history_messages=post_reply_budget.get("history_messages"),
            history_chars=post_reply_budget.get("history_chars"),
            history_tokens=post_reply_budget.get("history_tokens"),
            prompt_tokens=post_reply_budget.get("prompt_tokens"),
            next_turn_baseline_tokens=post_reply_budget.get(
                "next_turn_baseline_tokens"
            ),
            react_iteration_count=react_stats.get("iteration_count"),
            react_input_sum_tokens=react_stats.get("turn_input_sum_tokens"),
            react_input_peak_tokens=react_stats.get("turn_input_peak_tokens"),
            react_final_input_tokens=react_stats.get("final_call_input_tokens"),
            model_output_tokens=(
                usage.output_tokens if usage.coverage == "exact" else None
            ),
            usage_input_tokens=usage.input_tokens,
            usage_cached_input_tokens=usage.cached_input_tokens,
            usage_output_tokens=usage.output_tokens,
            usage_reasoning_output_tokens=usage.reasoning_output_tokens,
            usage_request_count=usage.request_count,
            usage_covered_request_count=usage.covered_request_count,
            usage_coverage=usage.coverage,
            react_cache_prompt_tokens=react_stats.get("cache_prompt_tokens"),
            react_cache_hit_tokens=react_stats.get("cache_hit_tokens"),
        )
    )
    logger.info(
        "[observe] turn_trace 已入队 session=%s tool_calls=%d",
        event.session_key,
        len(tool_calls),
    )


def _mobile_range_value(payload: dict[str, object]) -> Literal["24h", "7d"]:
    value = payload.get("range", "24h")
    if not isinstance(value, str) or value not in {"24h", "7d"}:
        raise MobileUiRpcInvalidRequest("range 只支持 24h 或 7d")
    return cast("Literal['24h', '7d']", value)


def _mobile_error_summary(item: dict[str, object]) -> dict[str, object]:
    return {
        "fingerprint": str(item["fingerprint"]),
        "error_type": str(item["error_type"]),
        "message": str(item["message"]),
        "source": str(item["source"]),
        "logger_name": str(item["logger_name"]),
        "status": str(item["status"]),
        "count": int(cast("int", item["count"])),
        "last_ts": str(item["last_ts"]),
        "sessions": int(cast("int", item["sessions"])),
        "is_new": bool(item["is_new"]),
        "is_spiking": bool(item["is_spiking"]),
    }


def _mobile_error_detail(item: dict[str, object]) -> dict[str, object]:
    result = _mobile_error_summary(item)
    traceback_text = str(item.get("traceback_text") or "")
    result.update(
        {
            "first_ts": str(item["first_ts"]),
            "traceback": traceback_text[:4000],
        }
    )
    return result


def _required_mobile_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > 512:
        raise MobileUiRpcInvalidRequest(f"{name} 必须是 1 到 512 字符的字符串")
    return value


def _to_proactive_turn_trace(event: ProactiveFinished):
    from .events import TurnTrace as TurnTraceEvent

    summary = event.final_message or event.skip_reason or event.gate_exit or ""
    usage = _proactive_usage(event)
    return TurnTraceEvent(
        source=event.mode,
        session_key=event.session_key,
        channel=_session_channel(event.session_key),
        user_msg=None,
        llm_output=summary,
        raw_llm_output=None,
        react_iteration_count=event.llm_call_count,
        react_input_sum_tokens=None,
        react_input_peak_tokens=None,
        react_final_input_tokens=None,
        usage_input_tokens=usage.input_tokens,
        usage_cached_input_tokens=usage.cached_input_tokens,
        usage_output_tokens=usage.output_tokens,
        usage_reasoning_output_tokens=usage.reasoning_output_tokens,
        usage_request_count=usage.request_count,
        usage_covered_request_count=usage.covered_request_count,
        usage_coverage=usage.coverage,
        react_cache_prompt_tokens=event.cache_prompt_tokens,
        react_cache_hit_tokens=event.cache_hit_tokens,
    )


def _proactive_usage(event: ProactiveFinished) -> NormalizedUsage:
    """把主动链路已报告的缓存数据保留为 partial usage。"""

    has_cache = (
        event.cache_prompt_tokens is not None
        or event.cache_hit_tokens is not None
    )
    return normalize_model_usage(
        {
            "input_tokens": event.cache_prompt_tokens,
            "cached_input_tokens": event.cache_hit_tokens,
            "request_count": event.llm_call_count,
            "covered_request_count": 0,
            "coverage": "partial" if has_cache else "unavailable",
        }
    )


def _session_channel(session_key: str) -> str | None:
    head, separator, _ = session_key.partition(":")
    return head if separator and head else None


def _to_rag_query_log(event: RetrievalCompleted):
    from .events import RagHitLog, RagQueryLog

    return RagQueryLog(
        caller="passive",
        session_key=event.session_key,
        query=event.query,
        orig_query=event.orig_query,
        aux_queries=list(event.aux_queries),
        hits=[
            RagHitLog(
                item_id=hit.item_id,
                memory_type=hit.memory_type,
                score=hit.score,
                summary=hit.summary[:120],
                injected=hit.injected,
                confidence_label=hit.confidence_label,
                forced=hit.forced,
            )
            for hit in event.hits
        ],
        injected_count=event.injected_count,
        route_decision=event.route_decision,
        error=event.error,
    )


def _to_memory_write_trace(event: MemoryWritten):
    from .events import MemoryWriteTrace

    return MemoryWriteTrace(
        session_key=event.session_key,
        source_ref=event.source_ref,
        action=event.action,
        memory_type=event.memory_type,
        item_id=event.item_id,
        summary=event.summary,
        superseded_ids=list(event.superseded_ids),
        error=event.error,
    )


def _slim_tool_calls(tool_chain: list[dict[str, object]]) -> list[dict[str, str]]:
    return [
        {
            "name": str(call.get("name", "")),
            "args": str(call.get("arguments", ""))[:300],
            "result": str(call.get("result", ""))[:500],
        }
        for group in tool_chain
        for call in _group_calls(group)
    ]


def _slim_tool_chain(tool_chain: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "text": str(group.get("text") or ""),
            "calls": [
                {
                    "name": str(call.get("name", "")),
                    "args": str(call.get("arguments", ""))[:800],
                    "result": str(call.get("result", ""))[:1200],
                }
                for call in _group_calls(group)
            ],
        }
        for group in tool_chain
    ]


def _group_calls(group: dict[str, object]) -> list[dict[str, object]]:
    calls = group.get("calls")
    if not isinstance(calls, list):
        return []
    raw_calls = cast(list[object], calls)
    out: list[dict[str, object]] = []
    for call in raw_calls:
        if isinstance(call, Mapping):
            mapping = cast(Mapping[object, object], call)
            out.append(
                {
                    str(key): value
                    for key, value in mapping.items()
                    if isinstance(key, str)
                }
            )
    return out
