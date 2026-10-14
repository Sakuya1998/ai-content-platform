# Monorepo 结构与服务边界

## 仓库布局

本仓库包含独立构建和部署的 Go Control Plane 与 Python Runtime。两者共享版本化消息契约、SQL 迁移、部署清单、初始化脚本和集成测试，不共享应用运行时或数据库 schema。

```text
go-control-plane/          Go 模块与 Control Plane
  cmd/                     api、worker、mq-check 入口
  internal/app/            依赖组装与生命周期
  internal/transport/      Chi HTTP 和 RocketMQ 适配器
  internal/modules/        identity、project、content、task 等业务模块
  internal/platform/       config、logger、database、messaging、objectstore
src/agent_runtime/         Python Runtime
  domain/ execution/ infrastructure/ workflows/
contracts/                 Go/Python 共用消息和内容契约
migrations/{control,runtime,seeds}/
deploy/ scripts/ tests/ docs/
```

Go 模块根和 Python 项目根保持原位。根 Python 项目也供迁移、初始化与维护脚本使用；挪动构建根目录需要单独设计 uv workspace。Docker 镜像继续独立构建。

## 服务边界

- Go 管理用户、工作区、项目、文档版本、治理配置、任务请求与 Runtime 事件投影。
- Python 管理执行租约、工作流检查点、模型调用、记忆执行和媒体恢复。
- Go 与 Python 之间只有 Outbox + RocketMQ 的命令/事件通道，不互调 HTTP、不访问对方数据库。
- 两个服务通过 AWS SDK 直接读写共享 RustFS agent-artifacts；MQ 消息只带小型数据和不可变对象引用。对象存储是数据平面，不作为 RPC。
- Runtime 只在消费消息后执行冻结执行包；不得通过 Control API 获取内容、Prompt、模型路由或策略。
- 普通查询由 Go 的事件投影回答；需要 Runtime 计算的操作异步发 MQ 命令，并通过 Go 查询其结果。
- 合同、事件顺序和对象引用定义在根目录 contracts；数据库迁移集中在根目录 migrations，并分别应用到 Control 与 Runtime 数据库。

## 内容类型扩展

小说、剧本、文案、短剧、广告和视频是通用项目与内容资源的类型。Go 的内容、任务、治理和知识模块不按格式复制。Python 仅为实际需要提供内容格式适配器，扩展 IR、规则、节点、评测和导出能力。

## 变更顺序

1. 保持构建根目录与独立镜像稳定。
2. 先冻结 API、Content IR、执行快照和 MQ 事件契约。
3. 迁移 Go 模块边界与 Runtime 消息处理，移除双向 HTTP 服务依赖。
4. 追加数据库前向迁移，将小说数据回填到通用资源后再删除旧表。
5. 最后更新部署配置、操作手册、架构边界检查和仓库贡献指南。

每个阶段验证 Go race/vet、Python Ruff/pytest、契约、迁移、Compose 和独立镜像构建。

