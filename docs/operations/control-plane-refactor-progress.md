# 控制面重构进度

本文件记录已批准的 `docs/superpowers/plans/2026-10-12-go-control-plane-refactor.md` 执行情况，防止将阶段性验证误认为全部完成。

## 已实施

- Command/执行快照跨语言模型、对象引用及 schema，hash、scope、版本验证。
- Go 向 RustFS 写冻结输入与资源，MQ Relay 只传引用；Runtime 直接读取并校验对象。
- Runtime 删除 ControlClient 与业务 HTTP API；Go Operations 使用异步命令和事件投影。
- Provider 凭据通过独立执行密钥 AES-GCM 封装，明文不进入 MQ/RustFS；两端配置各自加载同一执行密钥。
- Chi、Viper、slog、显式 app 组装、Go API 入口和优雅退出。
- 通用项目、内容、任务入口；旧小说 API 不保留兼容窗口。
- Control 017..019、Runtime 013..016 前向迁移：回填校验、源数据归档和旧状态表删除。
- Runtime 通用工作流状态、记录、检查点和记忆；真实 LangGraph 恢复测试。
- Go 各业务模块及消费方接口；SQL 适配器与 HTTP 分离。
- PostgreSQL 业务仓储归各业务模块的 `postgres` 子包；`internal/platform/database` 提供共享连接池初始化、探活与 workspace RLS 事务辅助，不承载业务 SQL。组合根负责初始化并注入连接池。
- Go 模块边界阶段 0–4 已完成：项目 transfer、production、knowledge/evaluation/observability 职责归位；Postgres 端口按事务消费方注入；关键共享表字段写入权与原子事务见 `docs/architecture/go-control-plane-data-ownership.md`。API、MQ contracts 和 schema 未变化。
- Go 共享消息契约移至 `internal/contracts`；RocketMQ、S3 与对象键实现移至 `internal/platform`；项目实体归 `internal/modules/project`。Go 模块根目录不再保留 `bridge`、`ir`、`storage` 包。
- 模块边界阶段 2–4：production 仓储归入 `production/postgres`；knowledge、evaluation、observability 分离；Content 文档写入、Evaluation 报告、审计、候选记录和 Runtime 状态通过事务端口按所有权模块写入；架构检查阻止兄弟 Postgres imports 与共享表写入回退。
- 删除 `internal/controlplane` 和生产可变组装；CI 检查模块依赖、服务间 URL、旧路由、状态 SQL 与 HTTP 层数据库依赖。
- 受控工作流目录发布、禁用和不可变版本；执行快照包含目录版本 ID、定义与 SHA-256，两端校验。
- 取消及过期租约不提交知识发布/绑定副作用；知识变更、任务完成与 Outbox 原子提交。
- Control 020..022：状态任务并发隔离、工作流目录和文档标题身份修正。
- 迁移管理员执行跨工作区回填、自动授予应用表/序列权限；每个版本独立事务提交。
- 历史 Control/Runtime 备份分别恢复到独立数据库，非空来源迁移、hash/count、归档与 RLS 验证。

## 完成状态

- 实施计划八项任务和模块边界阶段已完成。Go 仅传递通用输入、简报版本与冻结资源，小说内部状态仅由 Python 格式适配器构造。
- 最后输入调整后再次通过小说真实执行链路、Go 全量 race/vet/build 和 Python 全套回归。
- 本地隔离验证使用的临时 Go 进程、容器、数据卷和网络已清理；外部 staging 已按废弃数据授权完成前向迁移并保留运行中的 Compose 服务。未创建数据库备份，也没有执行 Git 提交。

## 已验证

- 实际应用角色下 Python 全套：830 passed、14 skipped；跳过项为需要其他可选配置的测试。
- Go race、vet、build，Ruff、架构检查、契约示例、Compose 两种配置与两个独立镜像构建。
- Go 模块边界整理后的真实 Control PostgreSQL race：使用 `control_test` 应用角色，Control migrations 1–22 与 Prompt/Policy seed 已应用；execution、production、workflow、governance、operations、resources 集成测试通过。
- 实际 PostgreSQL/pgvector、RocketMQ 5.3.1 Proxy、RustFS 和 FFmpeg 下六类端到端流程与五项辅助测试：11 passed；包含冻结版本、隔离、暂停/恢复、媒体、记忆及七子任务成片。
- AI 供应商协议使用本机确定性服务，未调用收费模型；外部 staging 本轮使用占位模型路由和诊断处理器，真实 Worker 尚未启用。

## 本地验证环境

模块边界收口使用一次性 PostgreSQL 17 容器 `module-boundaries-pg-test`，数据库 `control_test`、应用角色 `control_test`，映射 `127.0.0.1:55439`。Control migrations 1–22 和 Prompt/Policy seed 均成功应用；测试完成后容器和数据卷已删除。未修改外部托管基础设施。删除旧状态表前的 PostgreSQL custom-format 备份位于 `/tmp/agent-refactor-backup/control-before-retirement.dump` 与 `runtime-before-retirement.dump`。

## 决策记录

- 保留用户要求的分层数据库 addr/db/username 配置，URL 作为唯一环境变量连接覆盖，不再引入重复 URL 别名。
- 旧小说 API 直接下线；历史数据仍迁移和备份。
- 执行凭据使用独立共享密钥封装，后续可替换为外部密钥服务；不通过数据库主加密密钥给 Runtime 解密权限。
- 已应用 SQL 不改写；Control 017 在临时库直接执行后的账本记录经核验补齐。
- `AGENTS.md` 遵循用户最初要求保持原文件，不自动更新。
