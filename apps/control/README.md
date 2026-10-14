# Go 控制面

控制面使用 Chi、Viper、slog 与 pgx，按平台业务能力组织为模块化单体。`cmd/api` 启动服务，`internal/app` 显式组装依赖并管理生命周期；业务用例与 PostgreSQL 适配器分别位于 `internal/modules/<domain>` 和对应模块的 `postgres` 子包，HTTP 接口位于 `internal/transport/http`。数据库连接池只在组合根创建并注入模块适配器。

## 启动与配置

先在仓库根目录执行数据库初始化和迁移，再启动服务：

```bash
CONTROL_PLANE_CONFIG=config.yaml go run ./cmd/api
```

配置示例见 `config.example.yaml`。Viper 读取分层 YAML 和明确绑定的 `CONTROL_PLANE_*` 环境变量；数据库 URL 唯一环境覆盖项为 `CONTROL_DATABASE_URL`，也可填写 `database.addr/db/username/password`。生产配置需启用认证、限定 CORS 来源，并设置独立的模型存储密钥与执行快照加密密钥。

## 接口与消息

- `/healthz`：进程存活检查；`/readyz`：数据库和对象存储依赖检查。
- `/api/v2/projects`、`/documents`、`/content`：通用项目和不可变 Content IR。
- `/api/v2/jobs`：任务创建、查询、取消、审核恢复与 SSE。
- `/api/v2/workspaces/{workspace_id}/workflows`：受控工作流版本、发布和禁用。
- 工作区治理接口管理模型、提示词、策略、Skills、Tools、MCP Policy 和知识包。
- `/api/v2/projects/{project_id}/productions`：素材生成和自动成片调度。

写请求使用认证会话和 CSRF Token。旧小说 API 已直接下线。Go 与 Python Runtime 不互调业务 HTTP；命令和结果经事务 Outbox、RocketMQ 5.x Proxy 与事件投影传递。

RocketMQ 地址配置为 gRPC Proxy endpoint，例如 `rocketmq-proxy:8081`。Topic 由配置中的 tasks、control、events 和 memory 指定；API 运行命令 Relay、结果投影与恢复循环。完整消息语义见[消息架构](../docs/architecture/messaging.md)。

Go 和 Runtime 共用 `agent-artifacts` S3 bucket 与 Redis DB 0。对象按工作区、项目、资源和版本组织；两端各用 AWS SDK 访问对象。执行快照只通过对象引用发布，且校验 SHA-256；凭据通过独立执行密钥加密封装。

## 验证

```bash
go test -race ./...
go vet ./...
MQ_TEST_CONTROL_DATABASE_URL=postgresql://... go test -race ./...
```

数据库测试连接必须指向已迁移的隔离数据库，并使用 Control 应用角色验证 RLS。RustFS 直接读写测试可设置 `ARTIFACT_TEST_S3_ENDPOINT`、`ARTIFACT_TEST_S3_ACCESS_KEY`、`ARTIFACT_TEST_S3_SECRET_KEY` 后运行 `go test ./internal/platform/objectstorage -run TestS3RustFSIntegration`。根目录 `scripts/check_architecture.py` 验证服务和模块依赖边界。

完整接口契约见 [`openapi/control-v2.json`](../openapi/control-v2.json)，分层和运行方式见[Go 控制面架构](../docs/architecture/go-control-plane.md)。
