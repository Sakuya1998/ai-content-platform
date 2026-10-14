# Python Runtime 代码结构

## 目录职责

Python Runtime 是独立部署的执行服务。它消费 RocketMQ 命令、写入 Runtime PostgreSQL 与自己的 Outbox，并通过 RocketMQ 发布事件。它不请求 Go Control 的 HTTP API，也不连接 Control 数据库。

```text
src/agent_runtime/
  settings.py              # Pydantic RuntimeSettings，RUNTIME_* 配置
  domain/                  # Content IR、能力快照、协议、ContextPack、记忆对象
  execution/               # 任务上下文、租约 Worker、处理器、结构化输出
  infrastructure/
    models/                # LLM/embedding 客户端与调用记录
    media/                 # 图片/语音/视频供应商适配器
    mq/                    # RocketMQ Proxy SDK、Inbox/Outbox 消费发布
    postgres/              # 执行任务、检查点、任务产物、记忆与知识包
    storage/               # AWS SDK for Python、RustFS 不可变对象
  workflows/
    content/               # 通用生成、改编、计划、写作和审核
    media/                 # 多模态任务、素材恢复、分镜、时间线和合成
    novel/                 # 小说专业 Agent、Canon、创作约束与评测适配器
  prompts/                 # 冻结 Prompt 版本验证与 literal 渲染
  api/                     # readiness、liveness 与 Runtime 自身诊断
  entrypoints/             # MQ 角色启动、优雅关闭
  maintenance/             # 备份与数据维护
```

根目录 scripts 保留平台初始化、迁移、备份和联调工具；SQL 集中在根目录 migrations。运行时处理器不能执行任意消息路径或从 Control 数据库读取业务数据。

## 服务边界

- Go 将任务所需的输入 IR、工作流、Prompt、模型路由、策略和能力版本冻结为不可变执行包。
- Go 先将执行包写入共享 RustFS，再在 Control 事务中写入任务和 Outbox 命令。命令携带对象键与 SHA-256，不携带凭据。
- Runtime 持久化命令 Inbox 后确认 RocketMQ 消息，从 RustFS 读取执行包并校验 hash；执行期间只访问 Runtime 数据库及所需供应商。
- Runtime 将大型输出写入 RustFS，并在 Runtime 数据库事务中创建 Outbox 事件；Go 消费事件并建立 Control 投影。
- 两个服务分别使用 AWS SDK for Python/Go v2 访问限定前缀的 agent-artifacts bucket。RustFS 是大对象数据平面，不用于 RPC。
- Runtime 与 Go 之间的命令、取消、恢复、查询和结果都通过 Outbox + RocketMQ。服务健康端点仅供部署和监控系统分别探测。
- 普通状态和列表由 Go 投影提供；需要 Runtime 现算的语义搜索等操作使用异步命令与结果事件。

## 执行与格式适配

- domain 不依赖 HTTP、数据库、SDK、服务入口或具体工作流。
- execution/context.py 与 errors.py 定义执行上下文和结果协议；基础设施实现由入口显式组装。
- 通用生成、改编、记忆和媒体能力由共享执行框架提供。
- novel 目录保留小说特有图、Canon、人物和章节规则；未来格式按真实需求注册适配器，不复制 Runtime、记忆或任务生命周期。
- 输入快照必须包含任务所需的冻结数据，Python 不得按数据库 ID 回调 Go 补取 Prompt、内容或治理资源。

## 配置、入口与测试

Runtime 配置由 Pydantic 加载，只使用 RUNTIME_*。移除 Go Control 地址和仅用于 Control/Runtime HTTP 调用的令牌配置。供应商 HTTP 凭据属于 Runtime 可访问的密钥配置，不进入 RocketMQ 消息或 RustFS 执行包。

```bash
uv run --locked uvicorn agent_runtime.api.app:app --host 0.0.0.0 --port 8000
uv run --locked python -m agent_runtime.entrypoints.messaging dispatch --env-file .env.runtime.external
uv run --locked ruff check src tests scripts main.py
uv run --locked pytest tests/domain tests/execution tests/novel
```

测试使用假模型与 MQ/S3 seam；集成测试验证命令 Inbox、执行快照 hash、fencing token、产物引用和事件 Outbox。外部联调使用 RocketMQ Proxy、隔离 PostgreSQL 和 RustFS，不能依赖 Go Runtime HTTP 地址。

