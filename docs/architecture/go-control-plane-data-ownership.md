# Control 数据所有权

本文记录 Go Control 数据库的业务写入者。所有模块共享 PostgreSQL 实例时，表和字段仍由单一模块拥有；跨模块用事务端口协作，不直接导入兄弟 `postgres` 包。

## 表与字段

| 表或字段 | 写入所有者 | 说明 |
|---|---|---|
| `users`、`workspaces`、`identity_sessions`、`audit_logs` | identity | 审计写入也统一进入 identity Postgres 适配器；其他模块通过组合根注入的事务回调写审计 |
| `projects`、`project_request_keys`、`project_projections.input/brief_version` | project | 初始 Content 文档经 content writer 在同一项目创建事务中创建 |
| `project_projections.state/state_hash`、`project_records`、Job/Command Outbox/Runtime Inbox | execution | Runtime 事件按 workspace、job 和 sequence 校验后写入；project stale 变更和 task candidate 检查通过 execution 事务端口完成 |
| `documents`、`document_versions`、`content_blocks`、`derivations` | content | 初始文档、任务版本号、Runtime 生成版本及 derivations 都通过 content Postgres 事务端口执行 |
| `workflow_templates` | workflow | Workflow 目录 API 和冻结版本读取 |
| 模型、Prompt、能力资源、Skills、Tools、MCP 与创作策略目录 | governance | 所有目录按不可变版本治理，审计走 identity writer |
| `evaluation_benchmark_runs` | evaluation | execution 在成功事件投影事务中调用 evaluation writer，HTTP 查询走 evaluation service |
| Usage、trace、memory 与 knowledge pack 投影 | execution 写 `project_records` | knowledge 读取知识包/绑定；observability 读取 usage、trace 与运行指标，不写该表 |
| `production_runs`、`production_children` | production | 生产进度、暂停恢复和恢复 Worker；子任务 Job/Outbox 通过 execution 命令端口写入同一事务 |
| `project_transfers` | project/transfer | 项目备份、导入、导出和任务恢复 |
| Runtime 数据库的 checkpoints、leases、memories、Runtime Inbox/Outbox | Python Runtime | Go 不跨库读写，查询投影由事件返回 Control |

## 原子事务

- 项目创建：项目、输入 projection、幂等键、简报和初始文档同事务创建；审计也在该事务内。
- 简报更新：项目锁、简报版本、输入 revision、execution 提供的候选稿失效操作和审计同事务提交。
- 任务提交：任务用例锁定项目并冻结输入；execution adapter 在相同事务写 Job、命令 Outbox 和能力快照。RustFS 对象预写失败后由孤儿对象生命周期清理。
- Runtime 事件：Inbox 去重、sequence 折叠、Job 状态、Content 版本提交、Evaluation 报告和 execution 记录投影共用一个事务；任一端口失败则整笔回滚并保留重试。
- 项目转移导入：项目、简报、Content IR、execution 记录和导入幂等键由 transfer 适配器在一个事务提交。
- 自动生产推进：Run/Child 与子任务 Job/Outbox 同事务提交，避免只写生产状态或只发执行命令。

## 事件与读模型

| 事件 | 写入责任 | 面向 API 的读模型 |
|---|---|---|
| Go 任务命令 | execution `command_outbox` | `jobs` 状态和 SSE 事件 |
| Runtime Job/Content/Project 事件 | execution Inbox 与 projection | Job、project state、Content 版本和 `project_records` |
| `project.usage.recorded`、`project.trace.recorded` | execution `project_records` | observability 汇总 token/cost 并查询 traces |
| knowledge 操作完成 | execution `project_records` | knowledge packs 和 project bindings |
| benchmark 操作完成 | evaluation `evaluation_benchmark_runs` writer | evaluation benchmark API |

所有权规则不会拆分 Control/Runtime 数据库，不增加 schema，也不改变 HTTP、MQ、RustFS 契约。
