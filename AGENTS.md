# AGENTS.md

## 身份与沟通

- 默认使用中文，代码标识符保持英文。
- 先给结论和实际证据，再说明实现过程。
- 本仓库是 Roxy 的独立 `observe` 插件 canonical source；安装 cache、正式 workspace 和 Core checkout 都不是可编辑副本。

## 固定入口

1. 先读 [`README.md`](README.md) 和 [`docs/metrics-v2.md`](docs/metrics-v2.md)。
2. 再核对目标 Core commit 中的 Plugin API、生命周期事件与 Dashboard SDK。
3. 修改前记录本仓库 base HEAD、目标 Core HEAD、允许路径和回滚点。

## 权威边界

- Core `sessions.db/turns.usage_json` 拥有正式 Turn 终态用量；Observe 只保存插件遥测和派生投影。
- 插件不得直接打开或修改 `sessions.db`，不得把临时图表状态反向写入 Core。
- 插件 ID 固定为 `observe`。仓库地址、展示品牌和插件 ID 是不同身份。
- `window.AkashicDashboard`、`@akashic/dashboard-ui` 等名称是当前宿主 ABI；在 Core 发布兼容别名前不得擅自重命名。
- Dashboard 指标响应只返回计数、状态、时间和稳定 ID，不返回 Prompt、回复正文、工具参数或 traceback。

## 持久化

- `turns` 是 Observe 遥测事实表；聚合表是可由 `turns` 重建的派生投影。
- Schema 演进只做可审阅的增加或显式 supersede。不得为迁移方便删除、覆盖或伪造历史 usage。
- `exact`、`partial`、`unavailable` 必须保留。未知值不得转成零或完整覆盖。
- Retention 只能按版本化策略减少插件遥测，并在同一事务中重建受影响投影。
- 迁移正式 `observe.db` 前必须备份、执行 SQLite 完整性检查并核对关键聚合；旧副本的清理由独立用户操作拥有。

## 实现纪律

- 缓存命中率使用 `sum(cached_input_tokens) / sum(input_tokens)`，不得平均每 Turn 百分比。
- 非平凡函数使用一句短 docstring，按真实阶段添加简短编号注释。
- 边界校验 fail-loud，不使用宽泛异常、空结果或 clamp 掩盖损坏数据。
- `dashboard_panel.tsx` 和 `mobile_panel.js`/CSS 的源文件按现有构建合同维护；生成的 `dashboard_panel.js` 不得手工编辑。
- 只做当前合同和验收必需的修改，不顺手重构错误、记忆或移动端其他领域。

## 验证与发布

- Python 测试使用显式 `AKASHIC_AGENT_ROOT` 指向固定 Core checkout，并记录两边完整 commit SHA。
- 先运行定向单测，再运行全部插件测试、前端测试、静态检查和跨仓库合同 Gate。
- 测试使用一次性 workspace 和 plugin home，不读取或写入正式 Roxy workspace。
- 候选通过固定 revision 安装为 `latest`；只读行为验证通过后才能晋升 `stable`。
- 交付说明必须列出插件 commit、Core commit、测试、未验证项、write set 和回滚点。

