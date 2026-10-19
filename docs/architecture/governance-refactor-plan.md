# Governance 模块重构记录

**状态**：代码迁移已完成（2026-10-16）

## 目标与结果

将 Governance PostgreSQL 适配器按领域拆入独立子包，移除父包中的重复生产实现，同时保留服务层接口、目录 API 行为和执行时读取入口。迁移按领域逐步切换，未修改跨服务契约。

## 当前结构

- `apps/control/internal/modules/governance/postgres/repositories.go`：组合根与 `GovernanceRepository` 兼容 facade，创建并注入各领域仓储。
- `apps/control/internal/modules/governance/postgres/resources/`：通用资源 CRUD、版本管理、内置样式适配及冻结资源读取。
- `apps/control/internal/modules/governance/postgres/models/`：模型配置 CRUD、API Key 加密、路由冻结、fallback 和冻结路由解析。
- `apps/control/internal/modules/governance/postgres/prompts/`：Prompt、Pack、工作流绑定、版本校验与冻结读取；写操作按 Prompt、Pack、Binding 分文件。
- `apps/control/internal/modules/governance/postgres/policies/`：创建策略版本、发布及工作流绑定。
- `governance.go` 与 `governance_builtins.go`：跨领域执行快照组装和内置资源逻辑，保留在父包。

各仓储通过 `database.BeginWorkspaceTx` 设置 workspace RLS 上下文；审计写入仍在对应业务事务内完成。现有 HTTP/API 与执行模块继续通过 facade 使用这些能力。

## 验证

在 PostgreSQL 17 测试库上应用当前 Control migrations，以非超级用户应用角色运行，并初始化 Prompt/Policy catalog：

```bash
cd apps/control
MQ_TEST_CONTROL_DATABASE_URL="$TEST_DATABASE_URL" go test -race ./... -count=1
go vet ./...
```

全量 Go 竞态测试和 `go vet` 均通过。验证覆盖资源版本生命周期及 workspace 隔离、模型密钥遮罩和冻结路由、Prompt/Policy 发布与绑定，以及 execution 和 HTTP 调用方。各源文件均小于 400 行。

## 后续

本计划中的代码迁移项已完成。后续可独立增加领域仓储级测试，减少对 HTTP 集成测试的依赖；这不阻塞本次重构完成。更详细的实现与验证说明见 [REFACTORING.md](../../apps/control/internal/modules/governance/postgres/REFACTORING.md)。
