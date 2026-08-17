from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

UsageCoverage = Literal["exact", "partial", "unavailable"]
_COVERAGE_VALUES = frozenset({"exact", "partial", "unavailable"})


@dataclass(frozen=True)
class NormalizedUsage:
    """Observe 持久化的规范化模型用量。"""

    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    request_count: int
    covered_request_count: int
    coverage: UsageCoverage


def normalize_model_usage(value: Mapping[str, object]) -> NormalizedUsage:
    """在插件事件边界校验 Core model_usage。"""

    # 1. 保留未知字段的未知语义，不把缺失 token 归零。
    coverage_raw = value.get("coverage", "unavailable")
    if not isinstance(coverage_raw, str) or coverage_raw not in _COVERAGE_VALUES:
        raise ValueError(f"model_usage coverage 无效: {coverage_raw!r}")
    coverage = cast(UsageCoverage, coverage_raw)
    usage = NormalizedUsage(
        input_tokens=_optional_nonnegative_int(value, "input_tokens"),
        cached_input_tokens=_optional_nonnegative_int(
            value,
            "cached_input_tokens",
        ),
        output_tokens=_optional_nonnegative_int(value, "output_tokens"),
        reasoning_output_tokens=_optional_nonnegative_int(
            value,
            "reasoning_output_tokens",
        ),
        request_count=_nonnegative_count(value, "request_count"),
        covered_request_count=_nonnegative_count(
            value,
            "covered_request_count",
        ),
        coverage=coverage,
    )

    # 2. 只接受 Core TurnUsage 能成立的不变量。
    if usage.covered_request_count > usage.request_count:
        raise ValueError("model_usage covered_request_count 不得大于 request_count")
    if (
        usage.input_tokens is not None
        and usage.cached_input_tokens is not None
        and usage.cached_input_tokens > usage.input_tokens
    ):
        raise ValueError("model_usage cached_input_tokens 不得大于 input_tokens")
    if usage.coverage == "exact" and (
        usage.request_count <= 0
        or usage.covered_request_count != usage.request_count
        or usage.input_tokens is None
        or usage.output_tokens is None
    ):
        raise ValueError("exact model_usage 缺少完整请求、输入或输出用量")
    if usage.coverage == "partial" and (
        usage.request_count <= 0
        or all(
            item is None
            for item in (
                usage.input_tokens,
                usage.cached_input_tokens,
                usage.output_tokens,
                usage.reasoning_output_tokens,
            )
        )
    ):
        raise ValueError("partial model_usage 缺少请求或任何已知用量")
    if usage.coverage == "unavailable" and usage.covered_request_count != 0:
        raise ValueError("unavailable model_usage 不得声明已覆盖请求")
    return usage


def _optional_nonnegative_int(
    value: Mapping[str, object],
    name: str,
) -> int | None:
    raw = value.get(name)
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ValueError(f"model_usage {name} 必须是非负整数或 null")
    return raw


def _nonnegative_count(value: Mapping[str, object], name: str) -> int:
    raw = value.get(name, 0)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ValueError(f"model_usage {name} 必须是非负整数")
    return raw
