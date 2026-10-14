# 通用 AI 内容平台重构基础层

这次重构把原有小说系统扩展为管理面与执行面分离的通用内容创作平台，支持小说、剧本、文案、短剧、广告与多模态转换。

## 当前边界

- go-control-plane 提供身份/工作区、项目/内容、模型与资源治理、任务 API、事件投影和 Outbox。
- src/agent_runtime 是 Python 执行服务，包含租约 Worker、受控工作流、模型/媒体适配器、Runtime PostgreSQL、检查点和记忆执行。
- migrations/control 与 migrations/runtime 分别管理两套数据库；服务不跨库读写。
- contracts 保存跨语言 Content IR、命令和事件契约；migrations/seeds 保存提示词与策略初始化数据。
- docker-compose.platform.yml 与 deploy/k8s 提供 PostgreSQL/pgvector、RocketMQ 5.x、Redis、RustFS 以及 Go/Python 服务配置。

## 关键不变量

1. 小说、剧本、文案等以通用 project/content/task 模型管理；格式差异由版本化 IR schema、治理资源和 Python 内容适配器表达。
2. Prompt、Skill、Tool 和 MCP Policy 发布后不可原地修改；任务启动时冻结版本到执行快照。
3. Runtime 将命令持久化到 Inbox 后确认 RocketMQ 消息；相同 command_id 和 hash 可安全重放，不同 hash 必须拒绝。
4. 记忆默认项目隔离；跨项目只能通过同一工作区发布并绑定 Knowledge Pack，不能因向量相似而自动共享。
5. ContextPack 按 canon → constraints → current → retrieval 的固定优先级组装，记录来源、hash、token 预算和整体 hash。
6. PostgreSQL 是持久任务和记忆的权威状态；Redis 仅用于缓存/通知；RustFS 保存大对象和不可变执行包。
7. 两个服务共用 agent-artifacts bucket；对象按 workspace/project 前缀隔离。Go 与 Runtime 使用 AWS SDK 各自直接访问 RustFS。
8. Go 与 Runtime 之间不互调 HTTP；业务命令和结果事件只经服务各自 Outbox 与 RocketMQ 5.x Proxy。MQ 只放控制数据和小型引用，大型数据通过 RustFS 对象键与 SHA-256 传递。
9. Runtime 输出对象不可变并绑定任务尝试和 fencing token；事件投影幂等。浏览器签名上传/下载如需使用，由 Go 为终端用户单独签发，不提供 Runtime 调用的 Go 签名或上传 API。

## 落地状态

上述重构已落地：Go 使用通用项目、内容和任务模块；Go 与 Runtime 通过 Outbox、RocketMQ Proxy 和不可变对象快照协作；旧小说 API、状态表和双向业务 HTTP 调用已移除。实现与验收详见[Go 控制面架构](go-control-plane.md)和[重构进度记录](../operations/control-plane-refactor-progress.md)。

