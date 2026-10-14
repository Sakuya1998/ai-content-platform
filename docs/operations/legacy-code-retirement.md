# 旧实现清理记录

日期：2026-10-09。

## 当前入口

- Go Control Plane 承担用户、权限、作品/内容、模型、提示词、任务、版本和导入导出管理。
- Python 镜像默认启动 `agent_runtime.api.app:app`；MQ 执行角色使用 `platform.messaging_service`。
- `main.py` 只调用远程平台 CLI，必须指定 `--platform-url`，不创建本地作品或数据库。
- 部署只使用 `docker-compose.platform.yml` 或 `docker-compose.external.yml`。

## 已删除

旧 Python 管理 API、本地 CLI、SQLite 小说存储、Chroma 向量实现、SQLite 模型档案/路由与旧认证代码已删除。旧本地备份、资源回填、评测脚本、旧 OpenAPI、单体 Compose、配置模板以及对应实现测试一并移除。

README、架构和处理器文档、发布检查与监控配置已按 Go/Python 平台改写，旧单体设计/实施文档和过时迁移验收记录已清理。CI 使用临时 PG/pgvector、非超级用户角色、统一迁移、Python 回归及 Go race/vet；发布分别构建两个服务镜像。

## 保留

- 小说专业 Agent、Graph、Canon、分层记忆、质量规则及结构化解析由当前 Runtime 复用，属于现行功能。
- 存储和模型工厂只解析任务上下文，无上下文时明确失败，不能创建旧本地存储。
- `migrations/seeds/prompts/catalog.v1.json` 供幂等数据库 seed 和离线测试使用；平台任务只读取冻结数据库提示词版本。
- `scripts/platform_legacy_*` 保留为一次性只读旧数据导出/幂等导入工具；Chroma 和 SQLite checkpoint 依赖移至 `migration` extra，默认生产依赖不安装。
- PostgreSQL 平台迁移、加密备份与恢复工具保留。未改动已有 SQL、真实配置、凭据和历史数据。
- `AGENTS.md` 按此前明确要求未修改；其中旧 API、评测及 OpenAPI 命令不再适用，当前命令以 README 为准。

## 验证

- 全量 Python（临时 Control/Runtime PG）：243 passed，3 个环境用例默认 skipped；Go PG race 与 vet、Ruff、部署检查、YAML 解析及文档链接检查通过。
- 两个完整处理器用例单独通过：Go/新 Runtime/实际 MQ Proxy 与 RustFS，使用临时 PostgreSQL 和模拟模型，覆盖通用生成、记忆/向量、改编审核及小说暂停/恢复/导出。未调用收费 AI。
- 加密平台备份、pg_dump/pg_restore 及中断重试用例单独通过。
- 新 Runtime 镜像构建、默认入口就绪和处理器导入检查通过，确认镜像不存在 Chroma、langchain-chroma 或 SQLite checkpoint 包。

删除旧实现测试后总测试数下降；复用的 Agent/Graph、Canon、记忆评测和平台行为测试仍保留。新增边界测试禁止运行时代码重新依赖旧管理/存储包，并验证无上下文时不会生成本地文件。

临时应用、数据库容器及匿名数据卷已清理。本轮验收使用临时数据库，外部数据库与原有本地数据未修改；外部 MQ 中仅确认并清理了本轮临时工作区的诊断事件，测试对象保留在对应项目路径。
