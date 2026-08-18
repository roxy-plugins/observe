# observe

Akashic 可观测性插件，负责采集 Turn、检索、记忆写入和全局错误遥测。

桌面 Dashboard 必须同时发布 `dashboard_panel.js` 与 `dashboard_panel.css`。CSS 是从
`dashboard_panel.tsx` 使用目标 Core 的 Dashboard Tailwind 配置生成的插件自有产物，
不能依赖宿主构建时偶然扫描到插件源码。

## 移动端

插件自带一个移动端 Observe 入口，并在同一看板内提供两个任务视图：

- `缓存效率`：展示近期 KV Cache 命中率、被动/主动链路差异和 Turn 明细。
- `运行健康`：先回答当前是否需要关注，再展示最近 24 小时的错误次数、新类型和增长项；错误现场只在用户展开时读取。

插件还会通过 `turn.after_answer` 在助手回答尾部显示真实的本轮模型输出 token。移动端核心只负责注册插件资源与转发带会话上下文的 RPC；未启用 `observe` 时不会出现 Observe 入口、运行健康数据或 Turn 统计。

移动看板不照搬桌面排障台：手机只保留状态判断、三项关键指标和可展开的问题列表。颜色只表达稳定、新问题和增长问题，列表层级依靠留白与分隔线，不把每条错误包装成卡片。
