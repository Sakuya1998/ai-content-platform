# 旧数据一次性迁移

旧 SQLite、ChromaDB 和本地 LangGraph 检查点不再作为运行时存储。需要迁移历史作品时，停止旧服务并保持源文件只读，使用平台导出/导入工具写入 Control PostgreSQL、Runtime PostgreSQL/pgvector 和 `agent-artifacts`。

```bash
uv sync --locked --extra migration
uv run --locked python scripts/platform_legacy_export.py \
  --sqlite memory/novels.db \
  --checkpoint memory/checkpoints.db \
  --chroma memory/chroma_db \
  --output data/legacy-export.json

uv run --locked python -m scripts.platform_legacy_import \
  data/legacy-export.json --dry-run

uv run --locked python -m scripts.platform_legacy_import \
  data/legacy-export.json \
  --legacy-model-key-file data/model-settings.key
```

导入使用稳定 ID、源文件 hash 和 `legacy_import_rows` 幂等；冲突不会覆盖平台已有项目。旧密码 hash 可以保留，模型密钥需要旧密钥和新的 `CONTROL_PLANE_SECURITY_MODEL_ENCRYPTION_KEY` 才能重新加密。会话、正在运行的任务和本地输出路径不迁移，运行中的旧任务按中断状态处理。

导入后核对项目数、版本数、归档行数、稿件 hash、检查点恢复和向量维度。确认结果后再切换流量；迁移完成后不要重新启动旧 API、SQLite 或 Chroma 运行时。

平台备份使用 `scripts/platform_backup.py`，备份 Control/Runtime PostgreSQL 及对象存储索引，不依赖旧 SQLite 备份工具。

## 平台备份与恢复

停止 Go、Runtime 和所有写入 Worker 后，配置 `PLATFORM_BACKUP_CONTROL_DATABASE_URL`、`PLATFORM_BACKUP_RUNTIME_DATABASE_URL` 和 `PLATFORM_BACKUP_PASSWORD`。备份账号需读取全部租户及 BYPASSRLS 权限；恢复目标使用隔离空库，账号需创建表、扩展及授权权限。

```bash
uv run --locked python -m scripts.platform_backup create data/platform.enc --confirm-stopped
uv run --locked python -m scripts.platform_backup verify data/platform.enc
# 将两个备份连接改为隔离的空目标，再恢复。
uv run --locked python -m scripts.platform_backup restore data/platform.enc --confirm-stopped
```

需要同主版本的 `pg_dump/pg_restore`，或设置 `PLATFORM_PG_TOOLS_IMAGE=pgvector/pgvector:pg16` 使用容器工具。恢复中断后只重试同一归档，脚本按 hash 校验进度及对象冲突。Control 模型加密主密钥需独立备份，不包含在归档中。
