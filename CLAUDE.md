# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在此仓库中工作提供指导。

## 架构概览

这是一个模块化单体的通用 AI 内容平台，拆分为两个独立部署的服务：

- **Go 管理面** (`go-control-plane/`)：用户认证、工作区/项目管理、内容版本化、治理策略、模型配置、提示词目录和任务编排。拥有 Control PostgreSQL。
- **Python 执行运行时** (`src/agent_runtime/`)：执行 LangGraph 工作流，管理执行检查点、模型调用、记忆和媒体生成。拥有 Runtime PostgreSQL（含 pgvector）。

**服务之间仅异步通信**：Go 与 Python 通过 RocketMQ 5.x 交换命令和事件，使用 Outbox/Inbox 表。服务之间没有业务 HTTP 调用。大型产物（执行快照、生成内容）存储到共享 RustFS（S3 兼容对象存储）；MQ 消息只携带对象键、哈希和元数据。

**内容类型统一**：项目、文档和任务是通用资源。小说、剧本、文案、短剧、广告和视频是 content_type 值，而非独立的 API 或表。Python 仅在工作流确实不同时提供格式特定适配器。

## 仓库结构

```
go-control-plane/              Go 管理面模块
  cmd/api/                     HTTP 服务器入口
  cmd/mq-check/                MQ 连接诊断工具
  internal/app/                依赖组装和生命周期
  internal/transport/          Chi HTTP 路由和 RocketMQ 适配器
  internal/modules/            业务模块（identity、project、content、task、governance 等）
  internal/platform/           共享基础设施（config、database、messaging、storage、logger）
  
src/agent_runtime/             Python 运行时包
  settings.py                  从 RUNTIME_* 环境变量加载的 Pydantic RuntimeSettings
  domain/                      Content IR、能力快照、协议、记忆对象
  execution/                   任务执行上下文、租约 Worker、处理器、结构化输出
  infrastructure/              LLM/媒体客户端、MQ、PostgreSQL、RustFS 适配器
  workflows/                   LangGraph 实现（content/、media/、novel/）
  prompts/                     冻结提示词版本验证和字面量渲染
  api/                         就绪/存活/诊断 HTTP 端点
  entrypoints/                 MQ 角色启动器（dispatch、control、memory workers）
  
migrations/                    SQL 迁移，分别应用到各个数据库
  control/                     Control Plane 架构迁移
  runtime/                     Runtime 架构迁移
  seeds/                       初始数据（提示词、工作流）用于开发环境
  
contracts/                     Go 和 Python 之间共享的消息和内容契约
deploy/                        Docker Compose 和 Kubernetes 清单
scripts/                       平台初始化、备份、旧数据迁移工具
docs/architecture/             ADR 和服务边界文档
```

## 常用开发命令

### 环境设置

```bash
# 安装 Python 依赖（需要 Python 3.14+ 和 uv 0.12.1）
uv sync --locked --all-extras

# 设置本地环境文件
cp .env.control.example .env.control
cp .env.runtime.example .env.runtime
# 编辑文件设置密码和 API 密钥
```

### 本地运行服务

```bash
# 启动完整平台（Control + Runtime + PostgreSQL + RocketMQ + Redis）
docker compose -f docker-compose.platform.yml up -d --build

# 启动 Worker 进程
docker compose -f docker-compose.platform.yml --profile workers up -d

# 直接运行 Go Control API（需要 Go 1.23+）
cd go-control-plane
go run ./cmd/api

# 直接运行 Python Runtime API
uv run --locked uvicorn agent_runtime.api.app:app --host 0.0.0.0 --port 8000

# 运行消息 Worker
uv run --locked python -m agent_runtime.entrypoints.messaging dispatch --env-file .env.runtime
```

### 外部实例设置

连接到外部 RocketMQ、PostgreSQL 和 RustFS：

```bash
cp .env.control.external.example .env.control.external
cp .env.runtime.external.example .env.runtime.external
# 在文件中配置外部端点

uv run --locked python -m scripts.platform_external_prepare --migrate
docker compose -f docker-compose.external.yml up -d --build
```

### CLI 使用

```bash
# 平台 CLI 将所有请求路由到 Go Control API
uv run --locked python main.py --platform-url https://api.example.com --help

# 示例：创建小说项目
uv run --locked python main.py \
  --platform-url http://localhost:8080 \
  --identifier user@example.com \
  --title "我的小说" \
  --inspiration "一个关于..." \
  --chapters 5

# 导出项目
uv run --locked python main.py \
  --platform-url http://localhost:8080 \
  --identifier user@example.com \
  --resume PROJECT_UUID \
  --export output.md \
  --format markdown
```

### 测试和验证

```bash
# Python 代码检查和格式化
uv run --locked ruff check src tests scripts main.py
uv run --locked ruff format src tests scripts main.py

# 运行 Python 测试
uv run --locked pytest
uv run --locked pytest tests/test_api_contract.py  # 运行指定测试

# Go 测试
cd go-control-plane
go test -race ./...
go vet ./...

# 契约验证
uv run --locked python contracts/validate_examples.py
```

### 数据库迁移

迁移位于 `migrations/control/` 和 `migrations/runtime/`，分别应用到各个数据库。平台设置脚本自动处理此过程，手动迁移：

```bash
# 迁移是按顺序应用的 SQL 文件，由部署脚本处理
# 参见 docker-compose.platform.yml 的 migrate 服务
```

### 构建 Docker 镜像

```bash
# 构建 Go Control 镜像
cd go-control-plane
docker build -t control-plane:local .

# 构建 Python Runtime 镜像（从仓库根目录）
docker build -t runtime:local .
```

## 关键设计原则

**服务边界**：Control Plane 管理版本并编排；Runtime 执行冻结快照。Runtime 从不调用 Control HTTP API 或读取 Control 数据库。Control 不执行工作流或调用模型供应商。

**执行快照**：当 Control 创建任务时，将所有输入（内容 IR、工作流版本、提示词版本、模型路由、策略、能力）冻结为不可变执行包上传到 RustFS。MQ 命令只包含对象键和哈希。Runtime 下载、验证哈希并执行——从不通过 ID 从 Control 获取版本。

**提示词目录**：所有生产提示词存储在 Control PostgreSQL 的 `prompts`/`prompt_versions` 表中。Go 管理版本化、发布和工作流绑定。`migrations/seeds/` 中的种子仅用于初始化。Python 渲染冻结的提示词版本；不会回退到本地文件。

**Outbox 模式**：两个服务都使用事务性 Outbox 表。命令/事件与业务状态在同一事务中写入，然后中继发布到 RocketMQ。消费者在 ACK 之前持久化到 Inbox，确保至少一次传递和幂等性。

**Content IR**：所有内容类型（小说、剧本等）序列化为通用中间表示。Python 领域模型和工作流基于此 IR 操作，仅在工作流确实不同时提供格式特定适配器。

**行级安全**：两个数据库都使用 PostgreSQL RLS。每个认证请求设置工作区上下文；查询自动过滤到授权资源。

**任务状态**：公共状态为 `queued`、`running`、`waiting_review`、`cancel_requested`、`completed`、`failed`、`cancelled`。Runtime 内部状态如 `accepted` 映射到 `queued`。章节/场景进度是工作流步骤数据，而非独立状态机。

## Python 运行时分层

- **domain/**：纯领域模型，无基础设施依赖。Content IR、能力快照、上下文包、记忆节点、协议。
- **execution/**：任务执行上下文、基于租约的 Worker、处理器、结构化输出解析。依赖 domain 和基础设施接口。
- **infrastructure/**：具体适配器，包括模型（LLM/embedding 客户端）、媒体（图片/语音/视频供应商）、MQ（RocketMQ inbox/outbox）、postgres（任务/检查点/记忆仓储）、storage（通过 AWS SDK 访问 RustFS）。
- **workflows/**：用于内容生成/改编、媒体规划/合成、小说特定 Agent 的 LangGraph 状态机。依赖 domain 和 execution context。
- **prompts/**：验证冻结的提示词版本并渲染字面量模板（无表达式求值）。

## 配置

Go Control 使用 Viper 分层配置（文件 + 环境变量）。Python Runtime 使用 Pydantic `RuntimeSettings` 仅加载 `RUNTIME_*` 环境变量。

**不要提交密钥**：绝不提交凭据、加密密钥或 API 密钥。使用 `.env.control.example` 和 `.env.runtime.example` 作为模板。

**环境特定文件**：
- `.env.control` / `.env.runtime`：本地 Compose 设置
- `.env.control.external` / `.env.runtime.external`：外部基础设施
- `go-control-plane/config.example.yaml`：Go 配置模板

**关键设置**：
- Control：`CONTROL_PLANE_SECURITY_MODEL_ENCRYPTION_KEY`、`CONTROL_PLANE_SECURITY_EXECUTION_ENCRYPTION_KEY`、`CONTROL_PLANE_SECURITY_AUTH_ENABLED=true`、显式 `CONTROL_PLANE_SECURITY_ALLOWED_ORIGINS`
- Runtime：`RUNTIME_WORKER_HANDLER`、`RUNTIME_MEMORY_HANDLER`、`RUNTIME_LLM_PROVIDER`、`RUNTIME_LLM_API_KEY`、`RUNTIME_EXECUTION_ENCRYPTION_KEY`

生产环境必须启用认证并使用显式 CORS 来源。日志不得包含 API 密钥、完整内容或预签名 URL。

## 测试策略

Python 测试使用 pytest，`asyncio_mode = "auto"`。测试文件：`tests/test_<feature>.py`，函数：`test_<behavior>`。使用 `tests/conftest.py` 中的 fixture。模拟 LLM 响应和外部 API；使用 `tmp_path` 进行文件隔离。领域和执行测试不应调用真实模型供应商。

Go 测试使用标准 `testing` 包和 `-race` 检测器。适当时使用表驱动测试。

契约测试使用 `contracts/` 中的示例验证消息架构和执行快照格式。

## 代码风格

**Python**：4 空格缩进，模块/函数用 `snake_case`，类用 `PascalCase`，常量用 `UPPER_SNAKE_CASE`。需要类型提示。Ruff 强制样式（目标：py314，行长度：120）。使用 `uv run --locked ruff format` 格式化。

**Go**：标准 Go 格式化（`gofmt`）。使用 `log/slog` 结构化日志。在 `internal/app` 中显式依赖注入，无 DI 容器。

**提交**：使用约定式提交前缀：`feat:`、`fix:`、`docs:`、`chore:`、`refactor:`、`test:`。

## 重要架构文档

- `docs/architecture/monorepo-layout.md`：服务边界和通信模式
- `docs/architecture/go-control-plane.md`：Control Plane 模块和职责
- `docs/architecture/python-runtime-layout.md`：Runtime 目录职责和依赖约束
- `docs/architecture/runtime-handlers.md`：命令/事件流和执行一致性
- `docs/architecture/prompt-catalog.md`：提示词版本化、发布和工作流绑定
- `docs/architecture/adr-004-resource-version-snapshots.md`：执行快照设计
- `docs/operations/release-checklist.md`：发布前验证要求

## 安全注意事项

- 生产环境需要 `CONTROL_PLANE_SECURITY_AUTH_ENABLED=true` 和显式 `ALLOWED_ORIGINS`
- 模型配置和执行包的加密密钥必须单独设置和备份
- 行级安全在两个数据库中强制工作区隔离
- 对象存储键使用 workspace/project 前缀；Runtime 验证工作区上下文
- 日志不得输出密钥、完整提示词、生成内容或预签名 URL
- 会话 Cookie 在生产环境中使用 Secure/HttpOnly/SameSite 及 CSRF 令牌验证

## 健康检查和可观测性

- `/healthz`：进程存活性（两个服务）
- `/readyz`：服务特定依赖（自己的 PostgreSQL、对象存储）
- Go `/metrics`：Prometheus 指标（需要内部认证令牌）
- 两个服务都发出带有 `request_id`/`job_id` 关联的结构化 JSON 日志

## 附加说明

- 前端位于独立仓库；本仓库不包含前端源码
- 根目录的 `novel-agent-frontend/` 是空的占位目录
- 模型供应商配置在 Control 数据库中版本化，在任务创建时冻结
- 视频生成使用火山引擎异步协议；其他供应商需要适配器
- 动态 Tools/MCP 执行支持已规划但尚未实现
- 旧数据迁移工具以 `scripts/platform_legacy_*` 为前缀，用于一次性数据导入
