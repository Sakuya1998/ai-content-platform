# 数据库迁移

平台使用两个独立 PostgreSQL 数据库：

- `bootstrap.sql`：由 PostgreSQL 管理员执行，创建 `control`/`runtime` 用户、数据库、密码、扩展和 schema 权限。

- `control/001_init.sql`：内容项目、文档版本、派生关系、能力快照、任务投影、媒体资产和 RLS。
- `runtime/001_init.sql`：Runtime 任务 Inbox/Outbox、检查点相关执行状态、Canon、ContextPack、记忆和 pgvector。
- `runtime/002_runtime_defaults.sql`：为新接收命令设置 `runtime_jobs.status = accepted` 默认值。
- `runtime/003_artifacts.sql`：生成产物记录、校验和、RLS 及 Runtime Outbox 事件关联，支持上传失败后重试与幂等登记。
- `control/003_messaging.sql`：跨服务事件字符串 ID、有序投影进度、Outbox 重试时间和 RLS。
- `runtime/004_messaging.sql`：任务分类与业务重试、提前取消记录、记忆调度 Inbox/Outbox、Worker 租户发现表。
- `control/004_generation.sql`：文档版本与源任务的幂等关联、能力快照 RLS。
- `runtime/005_execution.sql`：审核恢复输入、源版本的记忆完成状态与 RLS。
- `control/005_identity_governance.sql`：用户会话、工作区模型/资源不可变版本和 RLS。
- `control/006_novel_management.sql`：小说状态与历史查询投影、brief、传输任务和单活跃任务约束。
- `control/007_operations.sql`：旧数据原始行归档、运维记录。
- `control/008_benchmarks.sql`：工作区质量 benchmark 报告和 RLS。
- `control/009_prompt_catalog.sql`：统一提示词身份、不可变版本、Prompt Pack、工作流显式绑定和 RLS。
- `control/010_prompt_catalog_identity.sql`：目录身份与节点约束、绑定删除历史及不可变契约保护。
- `control/011_creation_policies.sql`：通用 Canon、分层记忆、创作约束和评测策略的不可变版本及工作流绑定。
- `control/012_video_models.sql`：逐模型视频能力与视频工作流绑定。
- `control/013_image_speech_models.sql`：逐模型图片/语音能力及工作流绑定。
- `control/014_timeline_workflows.sql`：分镜、时间线与合成工作流绑定。
- `control/015_production_runs.sql`：自动生产父子任务、冻结快照、依赖状态和 RLS。
- `control/016_music_sound_effect_models.sql`：登记独立音乐与音效模型能力和工作流。
- `runtime/006_novel_workflows.sql`：完整小说状态/记录、官方 LangGraph 检查点/pending writes、pgvector 项目记忆。
- `runtime/007_knowledge_operations.sql`：不可变知识包、显式绑定和记忆运维记录。
- `runtime/008_shared_canon.sql`：Runtime 共享 Canon 快照的不可变存储。
- `runtime/009_media_tasks.sql`：视频评审、提交标记、远端任务与恢复状态的持久化、身份保护和 RLS。
- `runtime/010_generated_media.sql`：同步图片/语音的提交标记、审核、不可变归档结果与项目 RLS。
- `runtime/011_composition_runs.sql`：本地合成请求、输入指纹、渲染状态与不可变产物、项目 RLS。
- `runtime/012_music_sound_effect_media.sql`：扩展生成媒体持久化类型以支持音乐和音效。
- `control/017..019`：通用项目和内容回填、来源 hash/count 校验、原始行归档及旧状态表删除。
- `control/020..022`：状态任务并发限制、不可变工作流目录、文档 ID 与标题身份分离。
- `control/023`：独立评测策略、全局评测维度和 benchmark 聚合查询字段。
- `runtime/013..016`：通用工作流状态、检查点、分层记忆回填与校验、来源归档及旧表删除。

Control 011 同时提供版本化 creation policy（Canon、memory、constraints、evaluation）及工作流绑定；默认策略由统一 seed 初始化。

Control 023 的独立评测策略、benchmark 分析 API 和 Runtime 快照兼容说明见 `docs/architecture/evaluation.md`。

基础设施变量模板按服务拆分：`.env.control.example` 给 Go 管理面和 Control PostgreSQL，`.env.runtime.example` 给 Python Runtime、Runtime PostgreSQL、Redis、RocketMQ 和 RustFS。分别复制为 `.env.control`、`.env.runtime` 后填写实际端点和密码；这两个文件已被 Git 忽略，不应提交。

Go 使用 Viper 读取分层 `server/database/redis/mq/object_storage` YAML，或对应的 `CONTROL_PLANE_*` 环境变量；Python Runtime 使用 `RuntimeSettings`（`pydantic-settings`）读取 `RUNTIME_*` 环境变量。不要让 Go 和 Python 共享一个应用配置文件。

推荐初始化顺序：先执行 `bootstrap.sql` 或 `scripts/platform_bootstrap.py`，再运行 `scripts/platform_migrate.py`。应用用户不应拥有创建数据库或角色的权限。

已有数据升级需要跨工作区回填。设置仅供迁移进程使用的 `PLATFORM_MIGRATION_ADMIN_URL`（拥有 DDL 权限及 SUPERUSER/BYPASSRLS），同时提供 `CONTROL_DATABASE_URL` 和 `RUNTIME_DATABASE_URL`；统一脚本连接各目标库、逐版本事务提交并授予应用表和序列权限。不要把管理员连接放进服务配置。初始化入口已有 `PLATFORM_BOOTSTRAP_ADMIN_URL` 时可复用该管理连接。SQL 与账本 checksum 不得在应用后改写；重跑会校验并跳过已应用版本。

```bash
uv run --locked python scripts/platform_migrate.py --dry-run
uv run --locked python scripts/platform_migrate.py
```

删除旧表前先备份并演练恢复。对应非空来源和租户隔离测试为 `tests/infrastructure/test_control_migration.py` 与 `test_workflow_migration.py`；管理员测试连接使用 `MQ_TEST_ADMIN_DATABASE_URL`，应用查询保持普通角色。

统一迁移完成后自动幂等初始化 24 条内置提示词身份、13 个默认包与工作流绑定，以及 9 套创作策略。历史版本保留，版本行数可大于身份数。外部实例使用 `uv run --locked python -m scripts.platform_external_prepare --migrate`；仅补齐提示词可运行 `uv run --locked python -m scripts.platform_prompt_seed`。详细发布与回滚流程见 `docs/architecture/prompt-catalog.md`。

迁移运行用户应拥有 `pgcrypto`；Runtime 数据库还必须安装 `vector` 扩展。Runtime 外部命令、任务和事件 ID 使用 `text`，以保持 MQ/HTTP 协议的字符串标识兼容；内部向量和文档版本仍可使用 UUID。应用连接池在每个事务开始时设置 `app.workspace_id`，RLS 策略据此隔离租户。
