from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from agent.core.runtime_support import TurnRunResult
from agent.lifecycle.phase import Phase
from agent.lifecycle.phases.after_reasoning import (
    AfterReasoningFrame,
    default_after_reasoning_modules,
)
from agent.lifecycle.phases.after_turn import (
    AfterTurnFrame,
    default_after_turn_modules,
)
from agent.lifecycle.types import AfterReasoningInput, TurnSnapshot, TurnState
from agent.looping.ports import SessionServices
from agent.plugins.context import PluginContext, PluginKVStore
from agent.plugins.scope import PluginScope, ScopedEventBus
from bus.event_bus import EventBus
from bus.events import InboundMessage
from bus.events_lifecycle import TurnCommitted
from session.manager import SessionManager


def _load_plugin_module():
    path = Path(__file__).parents[1] / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "test_observe_plugin",
        path,
        submodule_search_locations=[str(path.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = _load_plugin_module()
ObservePlugin = module.ObservePlugin
GlobalErrorCollector = module.GlobalErrorCollector


class _Emitter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


class _DiscardOutbound:
    async def dispatch(self, outbound: object) -> bool:
        return True


async def _run_mobile_turn_observe_seam(
    root: Path,
    *,
    clear_assistant_message_id: bool,
) -> dict[str, object]:
    """运行真实 mobile 持久化与 Observe 查询缝隙。"""

    workspace = root / "workspace"
    event_bus = EventBus()
    scope = PluginScope("observe")
    plugin = ObservePlugin()
    plugin.context = PluginContext(
        event_bus=ScopedEventBus(event_bus, scope),
        tool_registry=None,
        plugin_id="observe",
        plugin_dir=Path(__file__).parents[1],
        data_dir=workspace / "plugin-data/observe-builtin",
        kv_store=PluginKVStore(workspace / "plugin-data/observe-builtin/.kv.json"),
        workspace=workspace,
        scope=scope,
        _can_start_tasks=lambda: True,
    )
    manager = SessionManager(workspace)
    committed: list[TurnCommitted] = []
    event_bus.on(TurnCommitted, committed.append)
    try:
        # 1. 用真实 after-reasoning 持久化生成 assistant message ID
        plugin.activate()
        session = manager.get_or_create("mobile:observe-seam")
        message = InboundMessage(
            channel="mobile",
            sender="device:observe-seam",
            chat_id="observe-seam",
            content="hi",
            metadata={"client_message_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        )
        state = TurnState(
            msg=message,
            session_key=session.key,
            dispatch_outbound=False,
            session=session,
        )
        after_reasoning = Phase(
            default_after_reasoning_modules(
                event_bus,
                SessionServices(session_manager=manager),
            ),
            frame_factory=AfterReasoningFrame,
        )
        reasoning_result = await after_reasoning.run(
            AfterReasoningInput(
                state=state,
                turn_result=TurnRunResult(
                    reply="hello",
                    context_retry={
                        "react_stats": {
                            "model_usage": {
                                "coverage": "exact",
                                "output_tokens": 321,
                            }
                        }
                    },
                ),
            )
        )
        message_id = reasoning_result.outbound.session_message_id
        assert message_id is not None
        outbound = (
            replace(reasoning_result.outbound, session_message_id=None)
            if clear_assistant_message_id
            else reasoning_result.outbound
        )

        # 2. 用真实 after-turn 发布 TurnCommitted，等待 Observe 落库
        context = Mock()
        context.render = Mock(return_value=SimpleNamespace(messages=[]))
        context.last_debug_breakdown = []
        after_turn = Phase(
            default_after_turn_modules(
                event_bus,
                _DiscardOutbound(),
                context,
            ),
            frame_factory=AfterTurnFrame,
        )
        await after_turn.run(
            TurnSnapshot(
                state=state,
                outbound=outbound,
                ctx=reasoning_result.ctx,
            )
        )
        await plugin._writer.drain()

        # 3. 从移动端公开查询读取同一条 assistant 消息
        result = plugin.mobile_ui_query(
            "kvcache.message_usage",
            {"message_id": message_id},
            session_id=session.key,
            turn_id=None,
        )
        assert len(committed) == 1
        return {
            "persisted_message_id": message_id,
            "committed_message_id": committed[0].assistant_message_id,
            "query": result,
        }
    finally:
        await plugin.terminate()
        _ = await scope.aclose()
        manager.close()


@pytest.mark.asyncio
async def test_observe_plugin_activate_and_terminate(tmp_path: Path) -> None:
    plugin = ObservePlugin()
    scope = PluginScope("observe")
    plugin.context = PluginContext(
        event_bus=ScopedEventBus(EventBus(), scope),
        tool_registry=None,
        plugin_id="observe",
        plugin_dir=tmp_path,
        data_dir=tmp_path,
        kv_store=PluginKVStore(tmp_path / ".kv.json"),
        workspace=tmp_path,
        scope=scope,
        _can_start_tasks=lambda: True,
    )
    plugin.activate()
    await asyncio.sleep(0.05)
    await plugin.terminate()
    assert await scope.aclose() == []
    db_path = tmp_path / "observe" / "observe.db"
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    finally:
        conn.close()
    assert "turns" in tables


@pytest.mark.asyncio
async def test_global_hooks_survive_overlapping_generations() -> None:
    loop = asyncio.get_running_loop()
    original_sys = sys.excepthook
    original_thread = threading.excepthook
    original_loop = loop.get_exception_handler()
    first = GlobalErrorCollector(_Emitter())
    second = GlobalErrorCollector(_Emitter())
    try:
        first.install()
        second.install()
        await first.uninstall()

        assert sys.excepthook == second._on_sys_except
        assert threading.excepthook == second._on_thread_except
        assert loop.get_exception_handler() == second._on_loop_except

        await second.uninstall()
        assert sys.excepthook == original_sys
        assert threading.excepthook == original_thread
        assert loop.get_exception_handler() == original_loop
    finally:
        await second.uninstall()
        await first.uninstall()


def test_turn_trace_keeps_message_identity_and_output_tokens(tmp_path: Path) -> None:
    emitter = _Emitter()
    module._emit_turn_trace(
        emitter,
        TurnCommitted(
            session_key="mobile:demo",
            channel="mobile",
            chat_id="demo",
            input_message="hi",
            persisted_user_message="hi",
            assistant_response="hello",
            tools_used=[],
            turn_id="turn-1",
            assistant_message_id="mobile:demo:2",
            model_usage={"output_tokens": 321, "coverage": "exact"},
        ),
    )

    trace = emitter.events[0]
    assert trace.turn_id == "turn-1"
    assert trace.assistant_message_id == "mobile:demo:2"
    assert trace.model_output_tokens == 321
    db_module = sys.modules[f"{module.__name__}.db"]
    conn = db_module.open_db(tmp_path / "observe.db")
    try:
        module.TraceWriter(tmp_path / "observe.db")._write_one(conn, trace)
        row = conn.execute(
            "SELECT turn_id, assistant_message_id, model_output_tokens FROM turns"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("turn-1", "mobile:demo:2", 321)


@pytest.mark.asyncio
async def test_mobile_turn_identity_reaches_observe_usage_query(tmp_path: Path) -> None:
    result = await _run_mobile_turn_observe_seam(
        tmp_path / "healthy",
        clear_assistant_message_id=False,
    )

    assert result == {
        "persisted_message_id": "mobile:observe-seam:1",
        "committed_message_id": "mobile:observe-seam:1",
        "query": {"usage": {"output_tokens": 321}},
    }


@pytest.mark.asyncio
async def test_mobile_turn_identity_mutant_is_killed(tmp_path: Path) -> None:
    result = await _run_mobile_turn_observe_seam(
        tmp_path / "mutant",
        clear_assistant_message_id=True,
    )

    with pytest.raises(AssertionError):
        assert result == {
            "persisted_message_id": "mobile:observe-seam:1",
            "committed_message_id": "mobile:observe-seam:1",
            "query": {"usage": {"output_tokens": 321}},
        }
    assert result["committed_message_id"] is None
    assert result["query"] == {"usage": None}


def test_partial_usage_and_empty_turn_ids_do_not_claim_complete_output(
    tmp_path: Path,
) -> None:
    emitter = _Emitter()
    for index in range(2):
        module._emit_turn_trace(
            emitter,
            TurnCommitted(
                session_key="mobile:demo",
                channel="mobile",
                chat_id="demo",
                input_message="hi",
                persisted_user_message="hi",
                assistant_response="hello",
                tools_used=[],
                assistant_message_id=f"mobile:demo:{index + 2}",
                model_usage={"output_tokens": 100, "coverage": "partial"},
            ),
        )

    assert all(event.turn_id is None for event in emitter.events)
    assert all(event.model_output_tokens is None for event in emitter.events)
    db_module = sys.modules[f"{module.__name__}.db"]
    conn = db_module.open_db(tmp_path / "observe.db")
    try:
        writer = module.TraceWriter(tmp_path / "observe.db")
        for event in emitter.events:
            writer._write_one(conn, event)
        count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    finally:
        conn.close()
    assert count == 2


def test_mobile_message_usage_returns_true_output_tokens(tmp_path: Path) -> None:
    plugin = ObservePlugin()
    plugin.context = SimpleNamespace(workspace=tmp_path)
    db_module = sys.modules[f"{module.__name__}.db"]
    conn = db_module.open_db(tmp_path / "observe" / "observe.db")
    try:
        conn.execute(
            """
            INSERT INTO turns(
                ts, source, session_key, turn_id, assistant_message_id,
                user_msg, llm_output, model_output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-07-17T00:00:00+00:00",
                "agent",
                "mobile:demo",
                "turn-1",
                "mobile:demo:2",
                "hi",
                "hello",
                321,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = plugin.mobile_ui_query(
        "kvcache.message_usage",
        {"message_id": "mobile:demo:2"},
        session_id="mobile:demo",
        turn_id=None,
    )

    assert result == {"usage": {"output_tokens": 321}}
    contribution = ObservePlugin.mobile_ui()
    assert contribution.module == "mobile_panel.js"
    assert contribution.stylesheet == "mobile_panel.css"
    assert contribution.navigation.label == "Observe"
    assert contribution.slots == ("turn.after_answer",)


def test_mobile_health_reuses_global_error_projection(tmp_path: Path) -> None:
    plugin = ObservePlugin()
    plugin.context = SimpleNamespace(workspace=tmp_path)
    db_module = sys.modules[f"{module.__name__}.db"]
    conn = db_module.open_db(tmp_path / "observe" / "observe.db")
    now = datetime.now(timezone.utc)
    traceback_text = "Traceback\n" + ("failure detail\n" * 500)
    try:
        for offset, count in ((2, 1), (1, 1), (0, 5)):
            moment = now - timedelta(hours=offset)
            conn.execute(
                """
                INSERT INTO global_errors(
                    fingerprint, bucket, source, logger_name, error_type,
                    message, traceback_text, level, first_ts, last_ts,
                    count, session_keys, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "fp-mobile-health",
                    moment.isoformat()[:13],
                    "asyncio",
                    "agent.worker",
                    "RuntimeError",
                    "background task failed",
                    traceback_text,
                    "ERROR",
                    (now - timedelta(hours=2)).isoformat(),
                    moment.isoformat(),
                    count,
                    '["mobile:demo"]',
                    "active",
                ),
            )
        conn.execute(
            """
            INSERT INTO global_errors(
                fingerprint, bucket, source, logger_name, error_type,
                message, traceback_text, level, first_ts, last_ts,
                count, session_keys, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fp-high-count",
                now.isoformat()[:13],
                "log",
                "agent.runtime",
                "LogError",
                "frequent but stable",
                "not returned to the mobile list",
                "ERROR",
                now.isoformat(),
                now.isoformat(),
                20,
                '[]',
                "active",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    snapshot = plugin.mobile_ui_query(
        "health.snapshot",
        {"range": "24h"},
        session_id=None,
        turn_id=None,
    )
    detail = plugin.mobile_ui_query(
        "health.error_detail",
        {"range": "24h", "fingerprint": "fp-mobile-health"},
        session_id=None,
        turn_id=None,
    )

    assert snapshot["total"] == 27
    assert snapshot["types"] == 2
    assert snapshot["spiking_types"] == 1
    item = snapshot["items"][0]
    assert item["fingerprint"] == "fp-mobile-health"
    assert item["error_type"] == "RuntimeError"
    assert "traceback" not in item
    assert detail["error"]["traceback"].startswith("Traceback")
    assert len(detail["error"]["traceback"]) == 4000
    assert "occurrences" not in detail["error"]

    conn = db_module.open_db(tmp_path / "observe" / "observe.db")
    try:
        conn.execute("UPDATE global_errors SET status = 'ignored'")
        conn.commit()
    finally:
        conn.close()
    ignored = plugin.mobile_ui_query(
        "health.snapshot",
        {"range": "24h"},
        session_id=None,
        turn_id=None,
    )
    assert ignored == {
        "range": "24h",
        "items": [],
        "types": 0,
        "total": 0,
        "new_types": 0,
        "spiking_types": 0,
    }

    for invalid_range in ("all", [], {}, True, None):
        with pytest.raises(ValueError, match="range 只支持"):
            plugin.mobile_ui_query(
                "health.snapshot",
                {"range": invalid_range},
                session_id=None,
                turn_id=None,
            )


def test_open_db_removes_legacy_unique_turn_id_index(tmp_path: Path) -> None:
    db_path = tmp_path / "observe.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE turns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT NOT NULL,
                session_key TEXT NOT NULL,
                turn_id TEXT
            );
            CREATE UNIQUE INDEX ux_turns_turn_id
            ON turns (turn_id) WHERE turn_id IS NOT NULL;
            """
        )
        conn.commit()
    finally:
        conn.close()

    db_module = sys.modules[f"{module.__name__}.db"]
    migrated = db_module.open_db(db_path)
    try:
        legacy_index = migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_turns_turn_id'"
        ).fetchone()
    finally:
        migrated.close()
    assert legacy_index is None


def test_kvcache_bootstrap_uses_incremental_projection_and_one_snapshot(
    tmp_path: Path,
) -> None:
    db_module = sys.modules[f"{module.__name__}.db"]
    events_module = sys.modules[f"{module.__name__}.events"]
    db_path = tmp_path / "observe" / "observe.db"
    conn = db_module.open_db(db_path)
    writer = module.TraceWriter(db_path)
    try:
        writer._write_one(
            conn,
            events_module.TurnTrace(
                source="agent",
                session_key="mobile:demo",
                user_msg="passive",
                llm_output="ok",
                react_cache_prompt_tokens=100,
                react_cache_hit_tokens=80,
            ),
        )
        writer._write_one(
            conn,
            events_module.TurnTrace(
                source="proactive",
                session_key="proactive:demo",
                user_msg="proactive",
                llm_output="ok",
                react_cache_prompt_tokens=50,
                react_cache_hit_tokens=20,
            ),
        )
    finally:
        conn.close()

    plugin = ObservePlugin()
    plugin.context = SimpleNamespace(workspace=tmp_path)
    bootstrap = plugin.mobile_ui_query(
        "kvcache.bootstrap",
        {},
        session_id=None,
        turn_id=None,
    )

    assert bootstrap["snapshot_turn_id"] == 2
    assert bootstrap["projection_through_turn_id"] == 2
    assert bootstrap["overview"]["tracked_turn_count"] == 2
    assert bootstrap["overview"]["hit_tokens"] == 100
    assert bootstrap["recent"]["total"] == 2
    assert bootstrap["recent_agent"]["total"] == 1


def test_kvcache_bootstrap_fails_loudly_on_projection_drift(tmp_path: Path) -> None:
    db_module = sys.modules[f"{module.__name__}.db"]
    db_path = tmp_path / "observe" / "observe.db"
    conn = db_module.open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO turns(ts, source, session_key, llm_output) VALUES (?, ?, ?, ?)",
            ("2026-07-17T00:00:00+00:00", "agent", "mobile:demo", "ok"),
        )
        conn.commit()
    finally:
        conn.close()

    plugin = ObservePlugin()
    plugin.context = SimpleNamespace(workspace=tmp_path)
    with pytest.raises(RuntimeError, match="投影水位不一致"):
        plugin.mobile_ui_query(
            "kvcache.bootstrap",
            {},
            session_id=None,
            turn_id=None,
        )


def test_turn_and_projection_update_roll_back_together(tmp_path: Path) -> None:
    db_module = sys.modules[f"{module.__name__}.db"]
    events_module = sys.modules[f"{module.__name__}.events"]
    db_path = tmp_path / "observe.db"
    conn = db_module.open_db(db_path)
    try:
        conn.execute("DELETE FROM kv_cache_totals")
        conn.commit()
        with pytest.raises(RuntimeError, match="singleton"):
            module.TraceWriter(db_path)._write_one(
                conn,
                events_module.TurnTrace(
                    source="agent",
                    session_key="mobile:demo",
                    user_msg="must roll back",
                    llm_output="ok",
                    react_cache_prompt_tokens=10,
                    react_cache_hit_tokens=5,
                ),
            )
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    finally:
        conn.close()


def test_retention_rebuilds_projection_in_the_delete_transaction(tmp_path: Path) -> None:
    db_module = sys.modules[f"{module.__name__}.db"]
    retention_module = sys.modules[f"{module.__name__}.retention"]
    db_path = tmp_path / "observe" / "observe.db"
    conn = db_module.open_db(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO turns(
                ts, source, session_key, user_msg, llm_output,
                react_cache_prompt_tokens, react_cache_hit_tokens
            ) VALUES (?, 'agent', 'mobile:demo', ?, 'ok', ?, ?)
            """,
            [
                ("2020-01-01T00:00:00+00:00", "expired", 100, 80),
                (datetime.now(timezone.utc).isoformat(), "current", 50, 20),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    migrated = db_module.open_db(db_path)
    migrated.close()

    retention_module._run_cleanup(db_path)

    bootstrap = sys.modules[f"{module.__name__}.mobile_kvcache"].KVCacheDashboardReader(
        tmp_path
    ).get_bootstrap()
    assert bootstrap["overview"]["tracked_turn_count"] == 1
    assert bootstrap["overview"]["prompt_tokens"] == 50
    assert bootstrap["recent"]["items"][0]["user_preview"] == "current"
    assert bootstrap["snapshot_turn_id"] == bootstrap["projection_through_turn_id"] == 2
