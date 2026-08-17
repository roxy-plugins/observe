# Observe Metrics V2 实施合同

- 状态：implementing
- capability owner：`observe` 插件
- consumer scope：桌面 Dashboard、Observe 移动面板
- authoritative state owner：Core `SessionStore` 与正式 `TurnUsage`
- baseline Core：`c1841a5de5b2f2d9725270bce2eed2c8a338ad2b`
- baseline Observe：`4d85b9dc64ef0d8d96c5a635586ca17dd94b59cd`

## 1. 目标

Observe 在保持可安装、可禁用和可回滚的前提下，显示可解释的模型用量、缓存效率、请求覆盖和运行健康。第一阶段复用 Core 已发布的 `TurnCommitted.model_usage`，不直接查询 `sessions.db`，不新增 Core 数据库 schema。

```text
Core provider usage
        │ normalize
        ▼
TurnCommitted.model_usage
        │ immutable generation event
        ▼
Observe turns telemetry
        │ read-only projection
        ├── Dashboard metrics
        └── Mobile compact metrics
```

## 2. 指标语义

一轮正式模型用量包含：

- `input_tokens`：全部 prompt tokens，缓存 token 是其子集。
- `cached_input_tokens`：由 provider 明确报告的缓存命中 tokens。
- `output_tokens`：全部模型输出 tokens。
- `reasoning_output_tokens`：输出中 provider 明确报告的 reasoning tokens。
- `request_count`：该 Turn 的模型请求总数。
- `covered_request_count`：同时具有必要 usage 字段的请求数。
- `coverage`：`exact | partial | unavailable`。

聚合规则固定为：

```text
cache_hit_rate = Σ cached_input_tokens / Σ input_tokens
cache_miss_tokens = Σ input_tokens - Σ cached_input_tokens
request_coverage = Σ covered_request_count / Σ request_count
```

不得平均每 Turn 的命中率。`input_tokens` 缺失的记录不进入缓存分母；coverage 仍计入独立分布。`cached_input_tokens > input_tokens` 是损坏数据，查询和写入边界都必须失败。

缓存聚合优先使用同一 Turn 的 `usage_input_tokens + usage_cached_input_tokens`。记录没有完整 V2 usage 对、但具有完整 `react_cache_prompt_tokens + react_cache_hit_tokens` 时，只在查询期把这组既有遥测计入缓存分子和分母。旧记录仍标记为 `legacy`；当前记录保留原有 coverage；两者都不会被回填或改写。两组都不完整时保持未知。Dashboard 趋势会跳过未知点，不把未知值绘制成零。

## 3. 数据增减合同

| 对象 | 正常增加 | 允许更新 | 允许减少 | 恢复证据 |
|---|---|---|---|---|
| `turns` 遥测 | Observe 收到已提交生命周期事件后 INSERT | 不改写既有 usage；旧 schema 只增加新列 | 仅由版本化 retention 删除到期遥测 | SQLite backup、integrity check、聚合对账 |
| usage 查询投影 | Dashboard 请求从 `turns` 即时聚合 | 不持久更新 | 请求结束即释放 | API 测试和固定数据库快照 |
| Dashboard 响应 | 每次请求即时派生 | 无持久更新 | 请求结束即释放 | API 测试和源数据库快照 |
| plugin-data | 插件激活后写入自有目录 | 插件 migration owner 按合同更新 | 普通卸载不得删除 | manifest、artifact SHA、目录备份 |

当前 legacy 路径是 `<workspace>/observe/observe.db`。迁入 `<workspace>/plugin-data/observe-github/observe.db` 属于独立发布步骤：先复制并验证，再切换；旧数据库在用户明确清理前保留，不与 Metrics V2 字段迁移混为一次提交。

## 4. API 与界面

V2 Dashboard API 使用 `/api/dashboard/observe/v2/*` 命名空间，至少提供 overview 和 timeseries。响应包括 schema version、range、usage totals、cache totals、coverage 和按 source 的拆分；不包含用户正文或错误现场。

桌面 Workbench 首屏展示：

1. 加权缓存命中率与命中/未命中 tokens。
2. 输入、输出、reasoning tokens。
3. 请求覆盖率和 exact/partial/unavailable Turn 数。
4. 被动、主动和 Drift 的命中率对比。
5. 时间趋势、ReAct 迭代和错误健康入口。

移动端保持摘要视图。旧 `kvcache.*` RPC 和旧 Dashboard API 在兼容期继续工作；新字段不得改变旧 DTO 的含义。

## 5. 非目标

本阶段不实现 TTFT、decode tok/s、provider/model 维度、费用估算、失败 Turn 全量统计或 Core 历史回灌。这些能力需要新增版本化 Core 事件和独立跨仓库合同，不能从 Turn 总耗时或当前价格推断。

## 6. 验收

- 加权命中率、miss tokens 和请求覆盖率具有独立单测，并能杀死“平均百分比”错误实现。
- exact、partial、unavailable 与 null 在 DB、API 和 UI 三层保持一致。
- 旧缓存遥测只作为只读聚合 fallback，既有行保持逐项不变。
- 旧 Observe schema 能无损增加 V2 列；既有正文、错误、检索和记忆记录逐项不变。
- API 响应不包含 `user_msg`、`llm_output`、`tool_calls` 或 traceback。
- 插件禁用不影响 Core；普通卸载保留 plugin-data。
- Python、Node、Dashboard 构建和固定 Core×Observe 合同 Gate 全部通过。
- 发布报告固定 Core、Observe、合同与场景的完整 SHA；分支名和安装 cache 不能替代 revision。
