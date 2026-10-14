# General AI Content Platform

通用内容创作平台，管理面使用 Go，执行面使用 Python Agent Runtime。平台以 Content IR 统一小说、剧本、文案、短剧、广告与视频片段，并支持受控的内容改编。

## 服务

- `go-control-plane/`：用户、工作区、项目、模型 Profile、提示词、能力治理、任务和版本管理。
- `src/agent_runtime/`：按领域、执行、基础设施、工作流、提示词和入口分层的 Python Runtime。
- `migrations/control/`、`migrations/runtime/`：唯一 SQL 迁移目录。
- `scripts/platform_legacy_*`：一次性旧数据只读导出和幂等导入工具。

## 本地启动

```bash
uv sync --locked --all-extras
cp .env.control.example .env.control
cp .env.runtime.example .env.runtime
docker compose -f docker-compose.platform.yml up -d --build
docker compose -f docker-compose.platform.yml --profile workers up -d
```

外部实例使用分开的 `.env.control.external` 和 `.env.runtime.external`：

```bash
uv run --locked python -m scripts.platform_external_prepare --migrate
docker compose -f docker-compose.external.yml up -d --build
```

Go 管理面使用 Viper 分层配置；Python Runtime 使用 `RuntimeSettings`。模型、提示词、Skill、Tool 和 MCP Policy 在 Go 侧版本化并在任务启动时冻结，Runtime 不读取旧 SQLite 路由，也不会从本地文件回退提示词。

公开接口统一使用项目、文档和任务资源，契约为 [Control v2 OpenAPI](openapi/control-v2.json)，一次切换要求见 [通用 API 切换](docs/migrations/generic-control-api.md)。`/healthz` 表示进程存活，`/readyz` 检查本服务的 PostgreSQL 与对象存储；检查不调用另一个服务。

## 验证

```bash
uv run --locked ruff check src tests scripts main.py
uv run --locked pytest
(cd go-control-plane && go test -race ./...)
(cd go-control-plane && go vet ./...)
```

外部 MQ、RustFS、Redis 和跨服务验证记录见 [外部联调](docs/operations/external-integration-verification.md)。提示词目录管理见 [prompt-catalog.md](docs/architecture/prompt-catalog.md)。历史数据一次性迁移见 [迁移说明](docs/migrations/legacy-data-cutover.md)。

Python 目录职责和依赖约束见 [Runtime 代码结构](docs/architecture/python-runtime-layout.md)。

剧本、短剧、内容转换和 Seedance 视频任务的配置及恢复流程见 [多模态模型与适配器](docs/architecture/multimodal-models.md)。

图片生成/编辑与语音合成支持 OpenAI 兼容协议、Seedream 和豆包 TTS，模型配置及示例见 [图片与语音接入](docs/architecture/image-speech-models.md)。

分镜与时间线规划、FFmpeg 拼接/混音/字幕以及合成任务恢复见 [媒体合成](docs/architecture/storyboard-timeline-composition.md)。

从已有分镜自动补齐素材、组装并交付成片的父子任务 API 见 [自动生产任务](docs/architecture/automatic-production.md)。

CLI 使用 `uv run --locked python main.py --platform-url https://api.example.com --help`，所有管理请求发往 Go 服务。运行创作 Worker 前必须为工作区配置模型路由；视频目前接入火山方舟异步任务协议，其他视频协议及动态 Tools/MCP 执行仍需扩展。前端位于独立仓库，本仓库不包含前端源码。
