# 通用 Control API 切换

本版本直接移除 `/api/v2/novels` 和内部业务 HTTP 接口。旧路径返回 404，不提供兼容响应、重定向或迁移窗口。前端与 CLI 必须在本版本发布前完成切换。

项目统一使用 `POST /api/v2/projects`，请求包含 `title`、`content_type`、`input` 和 `brief`。小说、剧本和文案分别使用 `novel`、`screenplay`、`copy`，遵循相同项目与文档契约。格式专属字段放在 `input` 中。

任务通过 `POST /api/v2/jobs` 提交，指定 `project_id`、`workflow`、`workflow_version`、`operation` 和 `arguments`。例如工作流为 `novel.write`、版本为 `1`；通用文本使用 `content.generate`。`Idempotency-Key` 保证重放返回原任务，参数变化返回 409。任务查询、取消、恢复和 SSE 分别使用 `/jobs/{job_id}`、`/cancel`、`/resume`、`/events`。

`POST /jobs/{job_id}/resume` 用于原任务的人工审核恢复，动作可为 `approve`、`revise` 或 `reconcile`。审核暂停只存在于任务状态投影中。同一审核事件序号下重复提交幂等，下一审核轮次产生新的控制命令。

文档和不可变版本使用 `/documents`、`/documents/{document_id}/versions`。新版本请求提供 `expected_version` 和 `document`，后者包含 `schema_version: "1.0"` 与 `blocks`。版本冲突返回 409。简报使用 `/projects/{project_id}/brief` 和 `/brief/versions`，其权威存储同样为文档版本。

候选稿选择、内容版本恢复和评测在 `/projects/{project_id}/blocks/{block_index}` 下提供。评测支持基线标记与比较。规划、审计、冲突、记忆、用量和追踪均为通用项目资源；计算型记忆操作通过 MQ 异步执行。导入使用 `/projects/import`，导出使用 `/projects/{project_id}/export`，异步导出通过 `/export/jobs` 返回 transfer ID。

迁移依次执行 Control 017 回填、018 源数据归档和 019 旧状态表删除。回填校验条数、源内容和哈希；归档保留源 JSONB、SHA-256 和工作区范围。删除前执行 PostgreSQL 备份并验证恢复。由于所有租户表强制启用 RLS，跨工作区回填必须使用专门的迁移管理账号；运行时应用仍使用受 RLS 约束的账号。020 只对修改项目状态的任务施加并发限制，媒体派生任务可并行。

021 初始化受控工作流目录并保护版本定义；022 将文档标题改为可重复的展示属性，文档 ID 保持身份。跨工作区回填设置 `PLATFORM_MIGRATION_ADMIN_URL` 后执行统一迁移，应用服务仍使用原 `CONTROL_DATABASE_URL` 和 `RUNTIME_DATABASE_URL`。每个迁移和 checksum 账本在独立事务中提交，不修改已应用 SQL。

公开接口契约为 `openapi/control-v2.json`，使用 `uv run --locked python -m scripts.export_control_openapi` 重新生成。Go API 和 Python Runtime 之间的业务命令与结果只经 Outbox、RocketMQ 和不可变对象快照传递。
