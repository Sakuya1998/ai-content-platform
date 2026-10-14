# 外部基础设施联调配置

外部联调只启动迁移容器、Go 管理面和 Python Runtime，不启动本地 PostgreSQL、Redis、RocketMQ 或 RustFS。

## 准备配置

```bash
cp .env.control.external.example .env.control.external
cp .env.runtime.external.example .env.runtime.external
```

`.env.control.external` 填写 Go 管理面和 Control PostgreSQL 配置；`.env.runtime.external` 填写 Python Runtime、Runtime PostgreSQL、Redis、RocketMQ 和 RustFS 配置。两个文件分别保留 `CONTROL_DATABASE_URL` 与 `RUNTIME_DATABASE_URL`；Control URL 同时供 Go 管理面和迁移容器使用。

## 检查并启动

```bash
docker compose \
  --env-file .env.control.external \
  --env-file .env.runtime.external \
  -f docker-compose.external.yml config --quiet

docker compose \
  --env-file .env.control.external \
  --env-file .env.runtime.external \
  -f docker-compose.external.yml up -d --build
```

`migrate` 会先执行 Control/Runtime 数据库迁移；迁移成功后 Go 和 Python 服务才会启动。重复执行时，checksum 未变化的迁移会显示为 `skipped`。

历史数据升级包含跨工作区回填，须由具备 `SUPERUSER` 或 `BYPASSRLS` 且拥有 DDL 权限的迁移管理员执行；应用账号不绕过 RLS。管理员连接仅用于迁移进程，不配置到 Go 或 Runtime 服务：

```bash
export PLATFORM_MIGRATION_ADMIN_URL='postgresql://migration-admin:YOUR_PASSWORD@db.example.com:5432/postgres?sslmode=require'
uv run --locked python scripts/platform_migrate.py
unset PLATFORM_MIGRATION_ADMIN_URL
```

同时设置 `CONTROL_DATABASE_URL`、`RUNTIME_DATABASE_URL`。脚本以管理员连接到两个目标库，迁移后授予各应用角色表和序列权限。已有 `PLATFORM_BOOTSTRAP_ADMIN_URL` 会同时用于初始化和迁移；只升级时使用 `PLATFORM_MIGRATION_ADMIN_URL`，无需应用密码参数。`platform_external_prepare --migrate` 支持相同环境变量和 `--migration-admin-url`。两个库位于不同实例时，应分别调用 `apply_database` 使用各实例管理连接。

外部 PostgreSQL 必须允许 Docker 主机访问，并安装 `pgcrypto`；Runtime PostgreSQL 还必须安装 `vector`。RocketMQ 应提供 5.x gRPC Proxy endpoint（通常为 8081，具体按供应商填写），应用不连接 NameServer。RustFS 使用 S3 API endpoint。

## 共享基础设施

RustFS 通过 S3 API 接入：Go 使用 AWS SDK for Go v2（`github.com/aws/aws-sdk-go-v2/service/s3`），Python 使用 AWS SDK for Python（boto3）。两端均采用 SigV4 签名和 path-style bucket 寻址。SDK 替换不需要修改现有存储配置或迁移对象。

Go 和 Python 使用同一个 RustFS bucket `agent-artifacts`，不按服务设置对象前缀。对象以工作区、项目、资源和版本组织：

```text
workspaces/{workspace_id}/projects/{project_id}/
  uploads/{asset_id}/{filename}
  documents/{document_id}/versions/{version_id}/{filename}
  assets/{asset_id}/versions/{version_id}/{filename}
  exports/{export_id}/{filename}
```

同一项目生成的图片、音频和视频都进入 `assets/`，通过数据库中的来源版本、任务和派生关系关联。Go 的 `internal/platform/objectstorage.ObjectKey` 与 Python 的 `object_keys.object_key` 使用同一规则；资源 ID 和文件名必须是安全单段，documents/assets 必须提供版本 ID。不要直接使用用户上传的原始文件路径作为对象键。

Go 与 Runtime 使用各自受限 S3 凭据直接读写授权项目路径。Go 写入不可变执行快照后，通过任务 Outbox 发送对象键、版本、大小和 SHA-256；Runtime 下载并校验后执行。endpoint 用于服务端访问；public_endpoint 仅用于面向用户的产物访问，两者须指向同一存储。

Python Runtime 使用 boto3 上传和读取产物，上传后返回 bucket、object_key、SHA-256、ETag、大小和 Content-Type。相同版本内容重复上传可安全重试，不同内容不能覆盖。下载必须通过 SHA-256 校验。

Runtime 调用 `S3ArtifactStore.upload` 写入产物后，在 Runtime DB 同一事务中登记 `artifact_records` 与 `artifact.created` Outbox 事件；登记和结果提交均检查租约及 fencing token。上传成功但数据库失败时可用相同版本重试。默认单次上限 50 MiB，可通过 `RUNTIME_ARTIFACT_MAX_BYTES` 调整。

服务间业务通信仅通过 MQ 与对象引用。Runtime 不调用 Go 签名或上传 HTTP 接口；Go 使用自己的 S3 客户端校验 Runtime 结果，再更新任务和内容投影。

目录位置不能替代权限校验；跨项目派生须显式授权。已有对象不会自动移动或改 bucket，原 bucket 应继续保留直到数据库引用完成显式迁移。默认改名只影响新的部署配置。

本地 Compose 的 `artifact-init` 服务会幂等创建 bucket。外部实例可先手动创建 `agent-artifacts`，或在仓库根目录使用拥有 CreateBucket 权限的 Runtime 配置执行：

```bash
uv run --locked python scripts/platform_storage_init.py \
  --env-file .env.runtime.external --create-bucket
```

不加 `--create-bucket` 时只检查 bucket。两个服务的 `/readyz` 会在已配置存储但 bucket 不可访问时返回 503。

可在已有诊断工作区和项目路径执行直接存储校验（会写入测试产物）：

```bash
uv run --locked python scripts/platform_artifacts_check.py \
  --runtime-env-file .env.runtime.external \
  --workspace-id EXISTING_WORKSPACE_UUID --project-id EXISTING_PROJECT_UUID
```

该测试验证 S3 条件写入、相同内容安全重试、冲突内容拒绝，以及下载大小和 SHA-256 校验。它不登记任务或事件；MQ 诊断和处理器集成测试负责验证数据库与投影事务。

Redis 共用一个实例的 DB 0，Go key 前缀为 `control:`，Python 为 `runtime:`。共享 key 必须另外约定；禁止服务使用 `FLUSHDB` 清理自己数据。

Go 配置：`mq.endpoint`、`mq.tls_enabled`、`mq.access_key`、`mq.secret_key`、`mq.namespace`；环境变量分别为 `CONTROL_PLANE_MQ_ENDPOINT` 等。Python 对应 `RUNTIME_ROCKETMQ_ENDPOINT`、`RUNTIME_ROCKETMQ_TLS_ENABLED`、`RUNTIME_ROCKETMQ_ACCESS_KEY`、`RUNTIME_ROCKETMQ_SECRET_KEY`、`RUNTIME_ROCKETMQ_NAMESPACE`。

两个服务的 tasks/control/events/memory Topic 必须一致，使用 `agent-{env}-runtime-tasks/runtime-control/runtime-events/memory-tasks`。独立消费组及职责见 [消息设计](../architecture/messaging.md)。外部模板默认 staging；生产使用 prod。旧 COMMAND_TOPIC/CONSUMER_GROUP 配置已替换为 Go 的 `MQ_TOPICS_*`、`MQ_GROUPS_PROJECTION` 与 Python 的 `ROCKETMQ_TASK_TOPIC`、各角色 `*_GROUP`，请更新本地配置文件。预先创建四个 NORMAL Topic 和五个消费组，再授权生产/消费。AK/SK 为空仅适用于关闭认证的本地环境，TLS 参数应与 Proxy 配置一致。

SDK：Go 使用 `github.com/apache/rocketmq-clients/golang/v5`，Python 使用 `rocketmq-python-client`。Go 的 `CONTROL_PLANE_MQ_ENABLED=true` 启动 Outbox Relay 和事件投影。Python API 外独立启动 dispatch/control/relay/memory/memory-scheduler；Compose 已配置这些进程。Proxy 在路由响应中返回的地址与端口必须从客户端可达，不能只修改 Docker 端口映射。

自管 RocketMQ 管理员可执行 `deploy/rocketmq/init.sh`，设置 `ROCKETMQ_HOME`、`MQ_ADMIN_NAMESERVER`、`MQ_ADMIN_CLUSTER` 和 `MQ_ENVIRONMENT`。脚本默认 4 个读写分区、16 次交付重试；可使用 `MQ_QUEUE_COUNT` 和 `MQ_RETRY_MAX_TIMES` 调整。NameServer 访问仅属于此管理员初始化步骤，应用仍连接 Proxy。托管实例通过管理控制台创建同名资源。

配置端点后仍需检查 Topic 路由：Python SDK 启动返回成功不能单独证明 Topic 可用，Runtime 适配器会额外要求生产/消费 Topic 存在可用队列。`TOPIC_NOT_FOUND` 时核对环境前缀、namespace 与目标实例，并先初始化资源；不要启动业务 Worker。

真实执行 Worker 需要配置 `RUNTIME_WORKER_HANDLER=可信模块:异步函数` 和 `RUNTIME_MEMORY_HANDLER`。处理器签名为 `async def handle(command: RuntimeCommand) -> dict`；可返回 `WaitingReview` 保存审核检查点，抛出 `RetryableTaskError` 请求数据库重试。内容提交结果通过 `content_commit.document_version_id` 派生记忆刷新；记忆处理器返回非空 `nodes`（memory_type/content），节点按源版本持久化。pgvector 写入及必要记忆依赖检查由业务处理器负责。处理器只能由部署配置指定，消息不得携带可执行模块路径。

配置后使用 `docker compose -f docker-compose.external.yml --profile workers up -d --build` 启动执行 Worker。多个实例应设置不同 `RUNTIME_WORKER_ID`。未配置处理器时不要启用 workers profile，任务只接收并排队。

配置加载后可以单独检查两个 SDK 的 Proxy 连接，检查命令不发送或消费业务消息：

```bash
# Go：先将 .env.control.external 中的变量导入当前进程环境
cd go-control-plane
go run ./cmd/mq-check

# Python：在仓库根目录执行，直接读取 Runtime 配置文件
uv run --locked python scripts/platform_mq_check.py --env-file .env.runtime.external
```

客户端启动检查仍不等于消息投递、Outbox 和数据库投影全链路通过；完整联调必须额外验证发送、消费、重投和确认。

可在已迁移数据库、已创建 MQ 资源且 Go 开启 MQ 时运行四通道诊断（会在指定项目创建诊断任务和摘要）：

```bash
uv run --locked python scripts/platform_mq_integration.py \
  --env-file .env.runtime.external --control-url http://localhost:8080 \
  --workspace-id WORKSPACE_UUID --project-id PROJECT_UUID --username DIAGNOSTIC_USERNAME
```

通过 `PLATFORM_VERIFY_PASSWORD` 环境变量提供诊断用户密码，脚本使用公开登录会话和 CSRF 提交任务。该用户须有指定工作区和项目的写权限。此脚本自身消费消息，使用隔离的联调环境以避免与业务 dispatch/worker 竞争。它不调用真实 AI；验证快照下载、S3 产物、完成/取消投影、记忆派生与事务提交。SDK 关闭时的 pending-task 警告不能代替消息确认和数据库结果核对。

## 验证

### 配置升级与真实处理器

保留外部实例凭据并升级旧 MQ 配置：

```bash
uv run --locked python scripts/platform_config_upgrade.py \
  --environment staging --proxy-endpoint PROXY_HOST:8081 --no-tls
uv run --locked python -m scripts.platform_external_prepare --migrate --create-bucket
```

升级工具不把 NameServer 地址推断为 Proxy。缺失 Proxy 时暂时关闭 Go MQ 循环；数据库凭据保留，旧服务间 URL 和 Runtime 内部 token 会移除。两端执行包加密密钥 `CONTROL_PLANE_SECURITY_EXECUTION_ENCRYPTION_KEY` 与 `RUNTIME_EXECUTION_ENCRYPTION_KEY` 共用一个独立随机 32 字节 hex 密钥；已有密钥保留，配置不一致会在写文件前拒绝。Control 工具 token 独立保留，配置文件权限设为 0600。

在 `.env.runtime.external` 中填写 `RUNTIME_LLM_PROVIDER`（openai/openai_compatible/anthropic）、`RUNTIME_LLM_MODEL`、`RUNTIME_LLM_API_KEY`；兼容接口还需 `RUNTIME_LLM_BASE_URL`。`RUNTIME_ANALYSIS_MODEL` 为空时使用同一模型。记忆向量模型使用独立的 `RUNTIME_EMBEDDING_MODEL/BASE_URL/API_KEY`，必须输出 1536 维向量。任务使用冻结执行包中的模型配置；供应商凭据由共享执行包密钥解密，不通过 Control HTTP 补拉。不要在聊天、日志或提交记录中粘贴 API Key。

默认处理器已经注册为 `agent_runtime.execution.handlers:generate_content` 与 `refresh_memory`。配置完整后启用 workers profile。内置流程支持小说、剧本、文案、短剧和广告的文本生成/改编，以及配置了对应协议的图片、音频、视频、分镜和成片任务。新处理器不读取旧 SQLite 的模型路由。

公开入口 `POST /api/v2/jobs` 接收以下请求，自动冻结快照、创建任务与 Outbox；使用登录会话、CSRF 和 `Idempotency-Key`：

```json
{
  "project_id": "PROJECT_UUID",
  "workflow": "content.adapt",
  "workflow_version": 1,
  "document_type": "screenplay",
  "title": "第一场",
  "objective": "将源小说改编为剧本，保持角色与因果一致",
  "source_version_id": "SOURCE_VERSION_UUID",
  "nodes": ["plan", "write", "review"],
  "required_memory_version_ids": []
}
```

纯创作使用 `content.generate` 并省略 source_version_id；续写同一文档可指定 document_id。`Idempotency-Key` 在客户端重试时保持不变。Runtime 从 MQ 的 execution_snapshot 引用下载冻结快照与源 IR；用户通过 `GET /api/v2/content/versions/{id}?project_id=...` 读取已提交内容。审核通过 `POST /api/v2/jobs/{id}/resume` 提交 action=approve/revise，修订时附 feedback。

真实 SDK 与本地协议模型的完整联调测试位于 `tests/integration/test_handler_integration.py`：覆盖公开注册会话、生成、IR 回写、摘要/事实/向量、改编与审核批准。需要隔离的 PostgreSQL/RustFS/RocketMQ 和 Go 服务，配置 HANDLER_TEST_CONTROL_URL 及 RUNTIME_* 后执行；它创建独立诊断工作区和内容，使用本地供应商协议服务，并断言 Runtime 未访问 Control HTTP。

```bash
curl http://localhost:8080/readyz
curl http://localhost:8000/readyz
set -a
source .env.control.external
set +a
UV_CACHE_DIR=/tmp/novel-agent-uv-cache uv run --locked python scripts/platform_integration.py \
  --compose-file docker-compose.external.yml \
  --control-database-url "$CONTROL_DATABASE_URL" \
  --rocketmq-endpoint "实际Proxy地址:8081"
```

生产环境不要使用模板中的 `change-me`，并通过 Secret 管理数据库密码、Redis 密码、RustFS 密钥、执行包加密密钥和独立工具 token。
