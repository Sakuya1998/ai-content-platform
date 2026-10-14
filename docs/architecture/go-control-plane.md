# Go 控制面架构说明

## 目标与边界

Go Control Plane 与 Python Runtime 是独立构建、部署的服务。浏览器和外部客户端通过 HTTP 访问 Go；Go 与 Python 之间不做同步 HTTP 调用，业务命令和结果事件只通过各自的 Outbox 与 RocketMQ 5.x Proxy 传递。模型供应商 HTTP、用户到 Go 的 HTTP、部署平台分别探测服务健康端点，不属于 Control 与 Runtime 的业务协议。

小型命令和事件使用 MQ。任务所需的完整执行快照及大型产物存入共享 RustFS agent-artifacts bucket；消息只携带对象键、版本、大小和 SHA-256。Go 与 Runtime 分别使用 S3 SDK 读写授权前缀。RustFS 是数据平面，不承载服务间 RPC。

Go 拥有身份、工作区、项目、内容版本、治理配置、任务请求和 Runtime 事件投影。Python 拥有执行租约、尝试、检查点、模型调用、记忆执行和媒体恢复。两边不读写对方数据库。

## 重构前的问题

重构前，Go 将多个职责放在 internal/controlplane，并使用路径条件分发和可变构造器组装；小说接口承担通用任务与内容管理。Go Operations 与 Python ControlClient 形成双向 HTTP 耦合。当前实现已删除过渡包和内部业务 HTTP 接口，任务与产物通过消息和对象快照传递。

Control 已有通用 projects、documents、document_versions、jobs、idempotency_keys；小说表在 Control 006 与 Runtime 006 中重复保存项目状态、记录、检查点和记忆。重构以通用模型为目标，不保留小说专属 API 与状态表。

## 服务拓扑

```
客户端 --HTTP/SSE--> Go Control
                       | Control PostgreSQL + Outbox
                       | RocketMQ Proxy 命令
                       v
                  Python Runtime
                       | Runtime PostgreSQL + Outbox
                       | RocketMQ Proxy 事件
                       v
                  Go 事件投影
                       |
             共享 RustFS 对象数据
```

Go 提交任务时先生成包含输入 IR、工作流版本、模型路由版本、Prompt、策略与能力版本的不可变执行包。Go 将执行包写入 RustFS，再在同一个 Control 数据库事务中保存任务、幂等键和带对象引用的 Outbox 命令。对象上传成功而数据库事务失败时会留下可由生命周期规则清理的孤儿对象。

Runtime 先将命令持久化到 Inbox，再确认 RocketMQ 消息；执行前下载执行包并核对 hash，不回调 Go 拉取内容或治理配置。Runtime 以租约和 fencing token 执行任务，将结果对象写入 RustFS，并在 Runtime 事务中写入事件 Outbox。Go 消费事件时先持久化 inbox，再幂等更新任务投影、文档版本和产物引用，之后确认消息。

取消、恢复、记忆重建、评测和需 Runtime 计算的搜索均使用命令/事件。普通列表与状态查询由 Go 投影提供；需要实时向量计算的查询返回异步操作 ID，调用方轮询 Go 的操作结果。Go 不通过 HTTP 代理 Runtime。

## Go 模块与依赖方向

```text
go-control-plane/
  cmd/{api,mq-check}/
  internal/
    app/                         # 显式组装、生命周期
    contracts/                   # Go/Python 消息 DTO 与执行快照契约
    transport/http/             # Chi 路由、DTO、中间件
    modules/
      identity/ content/ evaluation/ observability/
      workflow/ project/ (transfer/)
      task/ execution/ governance/ knowledge/ production/
    platform/
      config/ database/ logger/ messaging/
      objectstorage/ password/ security/ modelprovider/
```

模块按平台业务能力划分，不按输出格式或数据库表划分。小说、剧本、文案、短剧和广告使用相同的 project、content、task、knowledge 与 workflow 能力。小说章节、人物规则和评测准则由版本化内容类型资源及 Python 格式适配器提供；可保留 novel.write 作为工作流目录中的版本化实现标识，但不能有 /novels API、NovelAPI 或小说状态体系。

HTTP 与 MQ 适配器调用应用用例；应用层依赖领域模型和消费方定义的接口；PostgreSQL、RocketMQ、RustFS 等适配器实现这些接口。领域层不依赖传输和具体基础设施。internal/app 显式连接依赖，不引入 DI 容器，也不要求每个小模块都机械复制完整目录层级。

当前模块职责、主要依赖边与分阶段迁移提案见 [Go 模块职责与依赖边界设计](../superpowers/specs/2026-10-13-go-module-boundaries-design.md)。

## Go 工程约定

- HTTP 使用 Chi v5 和 net/http；配置只通过 Viper 加载并校验；结构化日志使用注入式 log/slog。
- `platform/database` 统一创建、探活 PostgreSQL 连接池，并提供事务级 workspace RLS 上下文；`app` 负责组装和注入连接池，各业务模块的 PostgreSQL 适配器持有本模块 SQL 及提交/回滚边界。
- Command Outbox 与任务请求、幂等键在同一事务提交；Runtime Event Inbox 与状态投影按事件序号幂等处理。
- 任务对外统一为 queued、running、waiting_review、cancel_requested、completed、failed、cancelled。Runtime 内部 accepted 映射为 queued；章节阶段等只能是工作流步骤数据。
- Go 的运行状态是 Runtime 事件的查询投影，不是第二个执行状态机。
- API、Worker 可先由显式构造器运行在同一进程；当需要独立扩缩或故障隔离时再启用 cmd/worker。

## 数据收敛

Control 的 novels 映射回 projects；简报成为版本化 document；章节与正式内容成为 IR 文档版本/内容块；候选稿、评测、审计和追踪分别归入通用任务产物、评测或事件记录。novel_request_keys 迁入 idempotency_keys；novel_transfers 改为通用导入导出操作与 RustFS 产物引用。

Runtime 的 novel_projects 状态并入通用任务事件和执行计划；novel_records 改为通用任务产物；novel_checkpoints 与写入表改为工作流检查点；novel_memories 迁入 memory_nodes、canonical_facts 和 memory_embeddings。SQL 迁移只追加前向版本，回填和校验完成后再单独删除旧表。

## 验收

- 服务代码中不存在 Go 到 Runtime 或 Runtime 到 Go 的业务 HTTP 客户端、内部路由和运行时 URL 配置。
- RocketMQ 命令与事件丢失、重复、乱序及进程重启均可通过 Outbox/Inbox、序号和幂等键恢复。
- 快照、产物引用通过 hash 校验；RLS、租约 fencing 和对象键作用域保持有效。
- novel、screenplay、copy 等类型使用同一项目、文档、任务 API 和状态机；最终无小说专属公开 API 与运行时状态表。
- 完成 Go race/vet/build、Python 测试、契约检查、迁移回填校验、Compose 和真实 MQ/RustFS 联调。

