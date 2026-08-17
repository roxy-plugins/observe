from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
import json
import sqlite3
import threading

from fastapi import FastAPI

from .db import open_db

# Observe monitoring dashboard: aggregates the agent-loop telemetry written to
# observe.db (turns table) into Grafana-style metrics — token & KV cache usage,
# ReAct iteration health, and error aggregation. Read-only.

# Range presets -> lookback hours (None = all history).
_RANGES: dict[str, int | None] = {
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
    "90d": 24 * 90,
    "all": None,
}
DashboardRange = Literal["24h", "7d", "30d", "90d", "all"]

_USAGE_AGGREGATE_SQL = """
    COUNT(*) AS turns,
    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
    SUM(usage_input_tokens) AS input_tokens,
    SUM(usage_output_tokens) AS output_tokens,
    SUM(usage_reasoning_output_tokens) AS reasoning_output_tokens,
    SUM(
        CASE
            WHEN usage_input_tokens IS NOT NULL
             AND usage_cached_input_tokens IS NOT NULL
            THEN usage_input_tokens
            WHEN react_cache_prompt_tokens IS NOT NULL
             AND react_cache_hit_tokens IS NOT NULL
            THEN react_cache_prompt_tokens
        END
    ) AS cache_prompt_tokens,
    SUM(
        CASE
            WHEN usage_input_tokens IS NOT NULL
             AND usage_cached_input_tokens IS NOT NULL
            THEN usage_cached_input_tokens
            WHEN react_cache_prompt_tokens IS NOT NULL
             AND react_cache_hit_tokens IS NOT NULL
            THEN react_cache_hit_tokens
        END
    ) AS cache_hit_tokens,
    SUM(usage_request_count) AS request_count,
    SUM(usage_covered_request_count) AS covered_request_count,
    SUM(CASE WHEN usage_coverage = 'exact' THEN 1 ELSE 0 END) AS exact_turns,
    SUM(CASE WHEN usage_coverage = 'partial' THEN 1 ELSE 0 END) AS partial_turns,
    SUM(CASE WHEN usage_coverage = 'unavailable' THEN 1 ELSE 0 END) AS unavailable_turns,
    SUM(CASE WHEN usage_coverage IS NULL THEN 1 ELSE 0 END) AS legacy_turns,
    SUM(
        CASE
            WHEN usage_input_tokens IS NOT NULL
             AND usage_cached_input_tokens IS NOT NULL
            THEN 1
            WHEN react_cache_prompt_tokens IS NOT NULL
             AND react_cache_hit_tokens IS NOT NULL
            THEN 1
            ELSE 0
        END
    ) AS cache_observed_turns,
    SUM(
        CASE
            WHEN usage_input_tokens IS NOT NULL
             AND usage_cached_input_tokens IS NOT NULL
             AND usage_cached_input_tokens > usage_input_tokens
            THEN 1
            WHEN (usage_input_tokens IS NULL OR usage_cached_input_tokens IS NULL)
             AND react_cache_prompt_tokens IS NOT NULL
             AND react_cache_hit_tokens IS NOT NULL
             AND react_cache_hit_tokens > react_cache_prompt_tokens
            THEN 1
            ELSE 0
        END
    ) AS invalid_cache_turns,
    SUM(
        CASE
            WHEN usage_request_count IS NOT NULL
             AND usage_covered_request_count IS NOT NULL
             AND usage_covered_request_count > usage_request_count
            THEN 1 ELSE 0
        END
    ) AS invalid_request_turns,
    SUM(
        CASE
            WHEN usage_input_tokens < 0
              OR usage_cached_input_tokens < 0
              OR usage_output_tokens < 0
              OR usage_reasoning_output_tokens < 0
              OR usage_request_count < 0
              OR usage_covered_request_count < 0
              OR react_cache_prompt_tokens < 0
              OR react_cache_hit_tokens < 0
              OR (
                  usage_coverage IS NOT NULL
                  AND usage_coverage NOT IN ('exact', 'partial', 'unavailable')
              )
              OR (
                  usage_coverage IS NOT NULL
                  AND (
                      usage_request_count IS NULL
                      OR usage_covered_request_count IS NULL
                  )
              )
              OR (
                  usage_coverage IS NULL
                  AND (
                      usage_input_tokens IS NOT NULL
                      OR usage_cached_input_tokens IS NOT NULL
                      OR usage_output_tokens IS NOT NULL
                      OR usage_reasoning_output_tokens IS NOT NULL
                      OR usage_request_count IS NOT NULL
                      OR usage_covered_request_count IS NOT NULL
                  )
              )
              OR (
                  usage_coverage = 'exact'
                  AND (
                      usage_request_count IS NULL
                      OR usage_request_count <= 0
                      OR usage_covered_request_count IS NULL
                      OR usage_covered_request_count != usage_request_count
                      OR usage_input_tokens IS NULL
                      OR usage_output_tokens IS NULL
                  )
              )
              OR (
                  usage_coverage = 'unavailable'
                  AND COALESCE(usage_covered_request_count, 0) != 0
              )
              OR (
                  usage_coverage = 'partial'
                  AND (
                      usage_request_count IS NULL
                      OR usage_request_count <= 0
                      OR (
                          usage_input_tokens IS NULL
                          AND usage_cached_input_tokens IS NULL
                          AND usage_output_tokens IS NULL
                          AND usage_reasoning_output_tokens IS NULL
                      )
                  )
              )
            THEN 1 ELSE 0
        END
    ) AS invalid_usage_turns,
    AVG(react_iteration_count) AS avg_iteration,
    MAX(react_iteration_count) AS max_iteration,
    MAX(ts) AS last_ts
"""


# Resolve a range token to (cutoff_iso, bucket_len). bucket_len is the substring
# length of the ISO ts used to group time buckets: 13 = hour (YYYY-MM-DDTHH),
# 10 = day (YYYY-MM-DD).
def _resolve_range(range_token: str) -> tuple[str | None, int]:
    if range_token not in _RANGES:
        raise ValueError(f"不支持的 Observe 时间范围: {range_token}")
    hours = _RANGES[range_token]
    bucket_len = 13 if (hours is not None and hours <= 24) else 10
    if hours is None:
        return None, bucket_len
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return cutoff.isoformat(), bucket_len


class ObserveDashboardReader:
    def __init__(self, workspace: Path) -> None:
        self.db_path = workspace / "observe" / "observe.db"
        self._lock = threading.RLock()

    # Aggregate the metric-card figures over the selected window.
    def get_overview(self, range_token: str) -> dict[str, Any]:
        cutoff, _ = _resolve_range(range_token)
        if not self.db_path.exists():
            return _empty_overview(range_token)
        where, params = _agent_window(cutoff)
        with self._lock, _connect(self.db_path) as db:
            row = db.execute(
                f"""
                SELECT
                    COUNT(*) AS turns,
                    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                    COALESCE(SUM(COALESCE(react_input_sum_tokens, prompt_tokens, 0)), 0) AS input_tokens,
                    COALESCE(SUM(react_cache_prompt_tokens), 0) AS cache_prompt_tokens,
                    COALESCE(SUM(react_cache_hit_tokens), 0) AS cache_hit_tokens,
                    COALESCE(SUM(CASE WHEN source = 'agent' THEN react_cache_prompt_tokens ELSE 0 END), 0) AS passive_cache_prompt_tokens,
                    COALESCE(SUM(CASE WHEN source = 'agent' THEN react_cache_hit_tokens ELSE 0 END), 0) AS passive_cache_hit_tokens,
                    COALESCE(SUM(CASE WHEN source IN ('proactive', 'drift') THEN react_cache_prompt_tokens ELSE 0 END), 0) AS proactive_cache_prompt_tokens,
                    COALESCE(SUM(CASE WHEN source IN ('proactive', 'drift') THEN react_cache_hit_tokens ELSE 0 END), 0) AS proactive_cache_hit_tokens,
                    AVG(react_iteration_count) AS avg_iteration,
                    MAX(react_iteration_count) AS max_iteration,
                    MAX(ts) AS last_ts
                FROM turns
                WHERE {where}
                """,
                params,
            ).fetchone()
        return _overview_from_row(row, range_token)

    # Bucketed time series for the trend charts.
    def get_timeseries(self, range_token: str) -> dict[str, Any]:
        cutoff, bucket_len = _resolve_range(range_token)
        if not self.db_path.exists():
            return {"range": range_token, "bucket": _bucket_name(bucket_len), "points": []}
        where, params = _agent_window(cutoff)
        with self._lock, _connect(self.db_path) as db:
            rows = db.execute(
                f"""
                SELECT
                    substr(ts, 1, ?) AS bucket,
                    COUNT(*) AS turns,
                    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                    COALESCE(SUM(COALESCE(react_input_sum_tokens, prompt_tokens, 0)), 0) AS input_tokens,
                    COALESCE(SUM(react_cache_prompt_tokens), 0) AS cache_prompt_tokens,
                    COALESCE(SUM(react_cache_hit_tokens), 0) AS cache_hit_tokens,
                    COALESCE(SUM(CASE WHEN source = 'agent' THEN react_cache_prompt_tokens ELSE 0 END), 0) AS passive_cache_prompt_tokens,
                    COALESCE(SUM(CASE WHEN source = 'agent' THEN react_cache_hit_tokens ELSE 0 END), 0) AS passive_cache_hit_tokens,
                    COALESCE(SUM(CASE WHEN source IN ('proactive', 'drift') THEN react_cache_prompt_tokens ELSE 0 END), 0) AS proactive_cache_prompt_tokens,
                    COALESCE(SUM(CASE WHEN source IN ('proactive', 'drift') THEN react_cache_hit_tokens ELSE 0 END), 0) AS proactive_cache_hit_tokens,
                    AVG(react_iteration_count) AS avg_iteration
                FROM turns
                WHERE {where}
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                (bucket_len, *params),
            ).fetchall()
        return {
            "range": range_token,
            "bucket": _bucket_name(bucket_len),
            "points": [_point_from_row(r) for r in rows],
        }

    def get_usage_overview(self, range_token: str) -> dict[str, Any]:
        """在一个 SQLite 快照中返回 V2 usage 总览和 source 拆分。"""

        cutoff, _ = _resolve_range(range_token)
        if not self.db_path.exists():
            return _empty_usage_overview(range_token)
        where, params = _agent_window(cutoff)

        # 1. UNION 让整体与 source 拆分读取同一个 statement snapshot。
        with self._lock, _connect(self.db_path) as db:
            rows = db.execute(
                f"""
                SELECT 'all' AS metric_source, {_USAGE_AGGREGATE_SQL}
                FROM turns
                WHERE {where}
                UNION ALL
                SELECT source AS metric_source, {_USAGE_AGGREGATE_SQL}
                FROM turns
                WHERE {where}
                GROUP BY source
                ORDER BY metric_source
                """,
                (*params, *params),
            ).fetchall()

        overall = next(
            (_usage_summary(row) for row in rows if row["metric_source"] == "all"),
            _empty_usage_summary(),
        )
        sources = {
            str(row["metric_source"]): _usage_summary(row)
            for row in rows
            if row["metric_source"] != "all"
        }
        return {
            "schema_version": 2,
            "range": range_token,
            **overall,
            "sources": sources,
        }

    def get_usage_timeseries(self, range_token: str) -> dict[str, Any]:
        """按小时或日期返回 V2 usage 加权时间序列。"""

        cutoff, bucket_len = _resolve_range(range_token)
        if not self.db_path.exists():
            return {
                "schema_version": 2,
                "range": range_token,
                "bucket": _bucket_name(bucket_len),
                "points": [],
            }
        where, params = _agent_window(cutoff)
        with self._lock, _connect(self.db_path) as db:
            rows = db.execute(
                f"""
                SELECT substr(ts, 1, ?) AS bucket, {_USAGE_AGGREGATE_SQL}
                FROM turns
                WHERE {where}
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                (bucket_len, *params),
            ).fetchall()
        return {
            "schema_version": 2,
            "range": range_token,
            "bucket": _bucket_name(bucket_len),
            "points": [
                {"bucket": row["bucket"], **_usage_summary(row)}
                for row in rows
            ],
        }

    # Error rows plus a top-N aggregation by normalized error signature.
    def get_errors(self, range_token: str, *, page: int, page_size: int) -> dict[str, Any]:
        cutoff, _ = _resolve_range(range_token)
        if not self.db_path.exists():
            return {"range": range_token, "items": [], "total": 0, "page": 1, "page_size": page_size, "groups": []}
        safe_page = max(1, page)
        safe_size = max(1, min(page_size, 100))
        offset = (safe_page - 1) * safe_size
        where, params = _agent_window(cutoff)
        err_where = f"{where} AND error IS NOT NULL"
        with self._lock, _connect(self.db_path) as db:
            total = int(
                (db.execute(f"SELECT COUNT(*) AS c FROM turns WHERE {err_where}", params).fetchone() or {"c": 0})["c"]
                or 0
            )
            rows = db.execute(
                f"""
                SELECT id, ts, session_key, user_msg, error
                FROM turns
                WHERE {err_where}
                ORDER BY ts DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, safe_size, offset),
            ).fetchall()
            group_rows = db.execute(
                f"""
                SELECT error, COUNT(*) AS count, MAX(ts) AS last_ts
                FROM turns
                WHERE {err_where}
                GROUP BY substr(error, 1, 80)
                ORDER BY count DESC, last_ts DESC
                LIMIT 8
                """,
                params,
            ).fetchall()
        return {
            "range": range_token,
            "items": [_error_row(r) for r in rows],
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "groups": [_error_group(r) for r in group_rows],
        }


    # ── 全局错误（global_errors 表）─────────────────────────────────

    # KPI + 排障台头部：总数、错误种类、新类型、爆发类型、最近时间、整体 spark。
    def get_global_overview(self, range_token: str) -> dict[str, Any]:
        cutoff, _ = _resolve_range(range_token)
        groups = self._global_groups(cutoff, include_traceback=False)
        total = sum(g["count"] for g in groups)
        last_ts = max((g["last_ts"] for g in groups), default=None)
        spark = _merge_buckets(groups)
        return {
            "range": range_token,
            "total": total,
            "types": len(groups),
            "new_types": sum(1 for g in groups if g["is_new"]),
            "spiking_types": sum(1 for g in groups if g["is_spiking"]),
            "last_ts": last_ts,
            "spark": [p["value"] for p in spark],
        }

    # 排障台左栏：按指纹聚合的群组，可按 facet 分组、按 q 过滤。
    def get_global_list(self, range_token: str, *, facet: str, q: str) -> dict[str, Any]:
        cutoff, _ = _resolve_range(range_token)
        groups = self._global_groups(
            cutoff,
            include_ignored=False,
            include_traceback=False,
        )
        if q:
            needle = q.lower()
            groups = [
                g for g in groups
                if needle in g["error_type"].lower()
                or needle in g["message"].lower()
                or needle in g["logger_name"].lower()
            ]
        groups.sort(key=lambda g: g["count"], reverse=True)
        sections = _facet_sections(groups, facet)
        for g in groups:
            g.pop("_buckets", None)
        return {
            "range": range_token,
            "facet": facet,
            "total": sum(g["count"] for g in groups),
            "sections": sections,
        }

    def get_mobile_global_health(
        self,
        range_token: str,
        *,
        limit: int,
    ) -> dict[str, Any]:
        """一次读取并返回手机所需的错误状态与摘要列表。"""

        # 1. 同一快照排除 ignored，避免状态头与可操作列表互相矛盾
        cutoff, _ = _resolve_range(range_token)
        groups = self._global_groups(
            cutoff,
            include_ignored=False,
            include_traceback=False,
        )
        groups.sort(
            key=lambda group: (
                bool(group["is_spiking"]),
                bool(group["is_new"]),
                int(group["count"]),
                str(group["last_ts"]),
            ),
            reverse=True,
        )
        total = sum(int(group["count"]) for group in groups)

        # 2. 手机先看增长项，只返回有限摘要，不读取 traceback 与现场会话
        for group in groups:
            group.pop("_buckets", None)
        return {
            "range": range_token,
            "total": total,
            "types": len(groups),
            "new_types": sum(1 for group in groups if group["is_new"]),
            "spiking_types": sum(1 for group in groups if group["is_spiking"]),
            "items": groups[:limit],
        }

    def get_mobile_global_detail(
        self,
        fingerprint: str,
        range_token: str,
    ) -> dict[str, Any]:
        """只读取手机展开项需要的错误详情。"""

        cutoff, _ = _resolve_range(range_token)
        if not self.db_path.exists():
            return {}
        where = "fingerprint = ?"
        params: tuple[Any, ...] = (fingerprint,)
        if cutoff is not None:
            where += " AND last_ts >= ?"
            params += (cutoff,)
        with self._lock, _connect(self.db_path) as db:
            rows = db.execute(
                f"SELECT * FROM global_errors WHERE {where} ORDER BY bucket ASC",
                params,
            ).fetchall()
        if not rows:
            return {}
        detail = _aggregate_fingerprint(rows)
        detail.pop("_buckets", None)
        return detail

    # 排障台右栏详情：完整 message、趋势、变体（同类型其他指纹）、现场 occurrences。
    def get_global_detail(self, fingerprint: str, range_token: str) -> dict[str, Any]:
        if not self.db_path.exists():
            return {}
        with self._lock, _connect(self.db_path) as db:
            rows = db.execute(
                "SELECT * FROM global_errors WHERE fingerprint = ? ORDER BY bucket ASC",
                (fingerprint,),
            ).fetchall()
            if not rows:
                return {}
            agg = _aggregate_fingerprint(rows)
            siblings = db.execute(
                """
                SELECT fingerprint, traceback_text, SUM(count) AS count, MAX(last_ts) AS last_ts
                FROM global_errors WHERE error_type = ? GROUP BY fingerprint ORDER BY count DESC
                """,
                (agg["error_type"],),
            ).fetchall()
            occurrences = self._global_occurrences(db, agg["session_keys"])
        agg["trend"] = [{"bucket": b, "count": c} for b, c in sorted(agg["_buckets"].items())]
        agg["variants"] = [
            {
                "fingerprint": s["fingerprint"],
                "count": int(s["count"] or 0),
                "traceback_text": s["traceback_text"] or "",
            }
            for s in siblings
        ]
        agg["occurrences"] = occurrences
        agg.pop("_buckets", None)
        return agg

    def set_global_status(self, fingerprint: str, status: str) -> dict[str, Any]:
        if status not in ("active", "acknowledged", "ignored") or not self.db_path.exists():
            return {"ok": False}
        with self._lock, _connect(self.db_path) as db:
            with db:
                db.execute(
                    "UPDATE global_errors SET status = ? WHERE fingerprint = ?",
                    (status, fingerprint),
                )
        return {"ok": True, "fingerprint": fingerprint, "status": status}

    # 取窗口内 global_errors 全部行，按指纹在 Python 侧聚合成群组列表。
    def _global_groups(
        self,
        cutoff: str | None,
        *,
        include_ignored: bool = True,
        include_traceback: bool = True,
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        where = "1=1" if cutoff is None else "last_ts >= ?"
        params: tuple[Any, ...] = () if cutoff is None else (cutoff,)
        traceback_column = "traceback_text" if include_traceback else "'' AS traceback_text"
        with self._lock, _connect(self.db_path) as db:
            rows = db.execute(
                f"""
                SELECT fingerprint, bucket, source, logger_name, error_type,
                       message, {traceback_column}, level, first_ts, last_ts,
                       count, session_keys, status
                FROM global_errors
                WHERE {where}
                ORDER BY fingerprint, bucket ASC
                """,
                params,
            ).fetchall()
        by_fp: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_fp.setdefault(row["fingerprint"], []).append(row)
        groups = [_aggregate_fingerprint(rows) for rows in by_fp.values()]
        if not include_ignored:
            groups = [g for g in groups if g["status"] != "ignored"]
        return groups

    # 现场：对每个 session_key 反查 turns 最近一条，取 user_msg 作触发上下文。
    def _global_occurrences(
        self, db: sqlite3.Connection, session_keys: list[str]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key in session_keys[:8]:
            row = db.execute(
                "SELECT ts, user_msg FROM turns WHERE session_key = ? ORDER BY ts DESC LIMIT 1",
                (key,),
            ).fetchone()
            out.append({
                "session_key": key,
                "ts": row["ts"] if row else None,
                "user_preview": _preview(row["user_msg"], 80) if row else "",
            })
        return out


def register(app: FastAPI, plugin_dir: Path, workspace: Path) -> None:
    reader = ObserveDashboardReader(workspace)

    @app.get("/api/dashboard/observe/overview")
    def observe_overview(range: DashboardRange = "24h") -> dict[str, Any]:
        return reader.get_overview(range)

    @app.get("/api/dashboard/observe/timeseries")
    def observe_timeseries(range: DashboardRange = "24h") -> dict[str, Any]:
        return reader.get_timeseries(range)

    @app.get("/api/dashboard/observe/v2/overview")
    def observe_usage_overview(range: DashboardRange = "24h") -> dict[str, Any]:
        return reader.get_usage_overview(range)

    @app.get("/api/dashboard/observe/v2/timeseries")
    def observe_usage_timeseries(range: DashboardRange = "24h") -> dict[str, Any]:
        return reader.get_usage_timeseries(range)

    @app.get("/api/dashboard/observe/errors")
    def observe_errors(range: DashboardRange = "24h", page: int = 1, page_size: int = 25) -> dict[str, Any]:
        return reader.get_errors(range, page=page, page_size=page_size)

    @app.get("/api/dashboard/observe/global_errors/overview")
    def global_errors_overview(range: DashboardRange = "24h") -> dict[str, Any]:
        return reader.get_global_overview(range)

    @app.get("/api/dashboard/observe/global_errors")
    def global_errors_list(range: DashboardRange = "24h", facet: str = "type", q: str = "") -> dict[str, Any]:
        return reader.get_global_list(range, facet=facet, q=q)

    @app.get("/api/dashboard/observe/global_errors/{fingerprint}")
    def global_errors_detail(fingerprint: str, range: DashboardRange = "7d") -> dict[str, Any]:
        return reader.get_global_detail(fingerprint, range)

    @app.post("/api/dashboard/observe/global_errors/{fingerprint}/status")
    def global_errors_status(fingerprint: str, value: str = "acknowledged") -> dict[str, Any]:
        return reader.set_global_status(fingerprint, value)


# Build the shared WHERE clause: LLM-driven flows, optionally bounded by cutoff.
def _agent_window(cutoff: str | None) -> tuple[str, tuple[Any, ...]]:
    sources = "source IN ('agent', 'proactive', 'drift')"
    if cutoff is None:
        return sources, ()
    return f"{sources} AND ts >= ?", (cutoff,)


def _bucket_name(bucket_len: int) -> str:
    return "hour" if bucket_len == 13 else "day"


def _rate(hit: int, total: int) -> float | None:
    return (hit / total) if total > 0 else None


def _usage_summary(row: sqlite3.Row) -> dict[str, Any]:
    """把 SQL usage 聚合映射为不伪造未知值的 API DTO。"""

    input_tokens = _optional_row_int(row, "input_tokens")
    output_tokens = _optional_row_int(row, "output_tokens")
    reasoning_tokens = _optional_row_int(row, "reasoning_output_tokens")
    cache_prompt = _optional_row_int(row, "cache_prompt_tokens")
    cache_hit = _optional_row_int(row, "cache_hit_tokens")
    request_count = _optional_row_int(row, "request_count")
    covered_request_count = _optional_row_int(row, "covered_request_count")
    if int(row["invalid_usage_turns"] or 0) > 0:
        raise ValueError("Observe usage 存在字段、coverage 或完整性损坏记录")
    if int(row["invalid_cache_turns"] or 0) > 0:
        raise ValueError("Observe usage 存在 cache hit 大于 input 的损坏记录")
    if int(row["invalid_request_turns"] or 0) > 0:
        raise ValueError("Observe usage 存在 covered request 大于 request 的损坏记录")
    if cache_prompt is not None and cache_hit is not None and cache_hit > cache_prompt:
        raise ValueError("Observe usage 聚合中 cache hit 大于 cache prompt")
    if (
        request_count is not None
        and covered_request_count is not None
        and covered_request_count > request_count
    ):
        raise ValueError("Observe usage 聚合中 covered request 大于 request")
    return {
        "turns": int(row["turns"] or 0),
        "errors": int(row["errors"] or 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "cache_prompt_tokens": cache_prompt,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": (
            cache_prompt - cache_hit
            if cache_prompt is not None and cache_hit is not None
            else None
        ),
        "cache_hit_rate": (
            _rate(cache_hit, cache_prompt)
            if cache_prompt is not None and cache_hit is not None
            else None
        ),
        "cache_observed_turns": int(row["cache_observed_turns"] or 0),
        "request_count": request_count,
        "covered_request_count": covered_request_count,
        "request_coverage_rate": (
            _rate(covered_request_count, request_count)
            if request_count is not None
            and covered_request_count is not None
            else None
        ),
        "coverage": {
            "exact": int(row["exact_turns"] or 0),
            "partial": int(row["partial_turns"] or 0),
            "unavailable": int(row["unavailable_turns"] or 0),
            "legacy": int(row["legacy_turns"] or 0),
        },
        "avg_iteration": (
            float(row["avg_iteration"])
            if row["avg_iteration"] is not None
            else None
        ),
        "max_iteration": int(row["max_iteration"] or 0),
        "last_ts": row["last_ts"],
    }


def _optional_row_int(row: sqlite3.Row, name: str) -> int | None:
    value = row[name]
    return int(value) if value is not None else None


def _empty_usage_summary() -> dict[str, Any]:
    return {
        "turns": 0,
        "errors": 0,
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "cache_prompt_tokens": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "cache_hit_rate": None,
        "cache_observed_turns": 0,
        "request_count": None,
        "covered_request_count": None,
        "request_coverage_rate": None,
        "coverage": {
            "exact": 0,
            "partial": 0,
            "unavailable": 0,
            "legacy": 0,
        },
        "avg_iteration": None,
        "max_iteration": 0,
        "last_ts": None,
    }


def _empty_usage_overview(range_token: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "range": range_token,
        **_empty_usage_summary(),
        "sources": {},
    }


def _overview_from_row(row: sqlite3.Row | None, range_token: str) -> dict[str, Any]:
    if row is None:
        return _empty_overview(range_token)
    turns = int(row["turns"] or 0)
    errors = int(row["errors"] or 0)
    cache_prompt = int(row["cache_prompt_tokens"] or 0)
    cache_hit = int(row["cache_hit_tokens"] or 0)
    passive_cache_prompt = int(row["passive_cache_prompt_tokens"] or 0)
    passive_cache_hit = int(row["passive_cache_hit_tokens"] or 0)
    proactive_cache_prompt = int(row["proactive_cache_prompt_tokens"] or 0)
    proactive_cache_hit = int(row["proactive_cache_hit_tokens"] or 0)
    return {
        "range": range_token,
        "turns": turns,
        "errors": errors,
        "error_rate": _rate(errors, turns),
        "input_tokens": int(row["input_tokens"] or 0),
        "cache_prompt_tokens": cache_prompt,
        "cache_hit_tokens": cache_hit,
        "cache_hit_rate": _rate(cache_hit, cache_prompt),
        "passive_cache_prompt_tokens": passive_cache_prompt,
        "passive_cache_hit_tokens": passive_cache_hit,
        "passive_cache_hit_rate": _rate(passive_cache_hit, passive_cache_prompt),
        "proactive_cache_prompt_tokens": proactive_cache_prompt,
        "proactive_cache_hit_tokens": proactive_cache_hit,
        "proactive_cache_hit_rate": _rate(proactive_cache_hit, proactive_cache_prompt),
        "avg_iteration": float(row["avg_iteration"]) if row["avg_iteration"] is not None else None,
        "max_iteration": int(row["max_iteration"] or 0),
        "last_ts": row["last_ts"],
    }


def _empty_overview(range_token: str) -> dict[str, Any]:
    return {
        "range": range_token,
        "turns": 0,
        "errors": 0,
        "error_rate": None,
        "input_tokens": 0,
        "cache_prompt_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_hit_rate": None,
        "passive_cache_prompt_tokens": 0,
        "passive_cache_hit_tokens": 0,
        "passive_cache_hit_rate": None,
        "proactive_cache_prompt_tokens": 0,
        "proactive_cache_hit_tokens": 0,
        "proactive_cache_hit_rate": None,
        "avg_iteration": None,
        "max_iteration": 0,
        "last_ts": None,
    }


def _point_from_row(row: sqlite3.Row) -> dict[str, Any]:
    cache_prompt = int(row["cache_prompt_tokens"] or 0)
    cache_hit = int(row["cache_hit_tokens"] or 0)
    passive_cache_prompt = int(row["passive_cache_prompt_tokens"] or 0)
    passive_cache_hit = int(row["passive_cache_hit_tokens"] or 0)
    proactive_cache_prompt = int(row["proactive_cache_prompt_tokens"] or 0)
    proactive_cache_hit = int(row["proactive_cache_hit_tokens"] or 0)
    return {
        "bucket": row["bucket"],
        "turns": int(row["turns"] or 0),
        "errors": int(row["errors"] or 0),
        "input_tokens": int(row["input_tokens"] or 0),
        "cache_hit_rate": _rate(cache_hit, cache_prompt),
        "passive_cache_hit_rate": _rate(passive_cache_hit, passive_cache_prompt),
        "proactive_cache_hit_rate": _rate(proactive_cache_hit, proactive_cache_prompt),
        "avg_iteration": float(row["avg_iteration"]) if row["avg_iteration"] is not None else None,
    }


def _error_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "ts": row["ts"],
        "session_key": row["session_key"],
        "user_preview": _preview(row["user_msg"], 80),
        "error": _preview(row["error"], 200),
    }


def _error_group(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "signature": _preview(row["error"], 80),
        "count": int(row["count"] or 0),
        "last_ts": row["last_ts"],
    }


def _preview(value: Any, limit: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


# ── 全局错误聚合辅助 ──────────────────────────────────────────────────────────


def _channel_of(session_keys: list[str]) -> str:
    for key in session_keys:
        head = key.split(":", 1)[0] if ":" in key else ""
        if head:
            return head
    return "—"


# 把同一指纹的多个小时桶行聚合成一个群组 dict（含派生的 spark / is_new / is_spiking）。
def _aggregate_fingerprint(rows: list[sqlite3.Row]) -> dict[str, Any]:
    rep = max(rows, key=lambda r: str(r["last_ts"] or ""))
    buckets: dict[str, int] = {}
    sessions: list[str] = []
    for row in rows:
        buckets[row["bucket"]] = buckets.get(row["bucket"], 0) + int(row["count"] or 0)
        for key in _parse_keys(row["session_keys"]):
            if key not in sessions and len(sessions) < 20:
                sessions.append(key)
    count = sum(buckets.values())
    first_ts = min(str(r["first_ts"] or "") for r in rows)
    last_ts = max(str(r["last_ts"] or "") for r in rows)
    spark = [buckets[b] for b in sorted(buckets)]
    return {
        "fingerprint": rep["fingerprint"],
        "error_type": rep["error_type"] or "Error",
        "logger_name": rep["logger_name"] or "",
        "source": rep["source"] or "log",
        "level": rep["level"] or "ERROR",
        "status": rep["status"] or "active",
        "message": _preview(rep["message"], 200),
        "traceback_text": rep["traceback_text"] or "",
        "count": count,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "session_keys": sessions,
        "sessions": len(sessions),
        "channel": _channel_of(sessions),
        "is_new": _is_new(first_ts),
        "is_spiking": _is_spiking(spark),
        "spark": spark,
        "_buckets": buckets,
    }


def _parse_keys(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(str(raw))
    except (ValueError, TypeError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


# NEW = 指纹首次出现在最近 24h 内。
def _is_new(first_ts: str) -> bool:
    if not first_ts:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return first_ts >= cutoff


# 爆发 = 最近一个桶的计数显著高于此前桶的均值。
def _is_spiking(spark: list[int]) -> bool:
    if len(spark) < 3:
        return False
    last = spark[-1]
    prev = spark[:-1]
    avg = sum(prev) / len(prev) if prev else 0
    return last >= 3 and last >= 2 * max(avg, 1)


# 按 facet 把群组切成带小标题的 section；type 走单段平铺。
def _facet_sections(groups: list[dict[str, Any]], facet: str) -> list[dict[str, Any]]:
    if facet not in ("source", "channel"):
        return [{"key": "all", "label": "", "count": sum(g["count"] for g in groups), "items": groups}]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for g in groups:
        buckets.setdefault(str(g[facet]), []).append(g)
    sections = [
        {
            "key": key,
            "label": _SOURCE_LABEL.get(key, key) if facet == "source" else key,
            "count": sum(g["count"] for g in items),
            "items": items,
        }
        for key, items in buckets.items()
    ]
    sections.sort(key=lambda s: s["count"], reverse=True)
    return sections


# 合并多个群组的小时桶 → 整体 spark 点序列。
def _merge_buckets(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, int] = {}
    for g in groups:
        for bucket, count in g.get("_buckets", {}).items():
            merged[bucket] = merged.get(bucket, 0) + count
    return [{"bucket": b, "value": merged[b]} for b in sorted(merged)]


_SOURCE_LABEL: dict[str, str] = {
    "log": "主动日志",
    "uncaught": "未捕获异常",
    "asyncio": "asyncio 任务",
    "thread": "子线程",
}


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
