# 仓库贡献指南

## 项目结构与模块组织

本仓库是 Go 与 Python 的 monorepo。`apps/control` 存放 Go 管理面服务，包括 `cmd/` 入口和 `internal/` 包；`apps/runtime` 存放 Python 运行时（`agent_runtime/`）及其 `tests/`。Go/Python 共用的架构定义和示例位于 `packages/contracts/contracts/`。部署资源位于 `infra/`，包括 `docker/`、`k8s/`、`migrations/` 以及监控和消息队列配置。架构与运维文档位于 `docs/`；迁移、开发和数据初始化脚本位于 `tools/`。

## 构建、测试与开发命令

除非另有说明，以下命令均从仓库根目录运行：

- `make build` 构建 Go 服务并同步锁定的 Python 环境。
- `make test` 使用竞态检测器运行 Go 测试，并运行 Python pytest 测试集。
- `make lint` 运行 `go vet` 和 Ruff 检查。
- `make format` 使用 `gofmt` 和 Ruff 格式化代码。
- `make docker-up` / `make docker-down` 启动或停止本地 Compose 服务栈。
- `make dev-control` / `make dev-runtime` 在本地运行对应服务。

运行针对性检查时，可使用 `cd apps/control && go test ./...`，或 `cd apps/runtime && uv run --locked pytest tests/<area> -q`。

## 代码风格与命名约定

Go 代码使用 `gofmt` 格式化，并遵循 Go 标准的包名和导出名称约定。Python 使用四空格缩进和类型注解；模块与函数采用 `snake_case`，类采用 `PascalCase`，常量采用 `UPPER_SNAKE_CASE`。Ruff 用于 Python 格式化和检查，项目配置位于 `apps/runtime/pyproject.toml`。服务专属代码应放在对应应用中；修改共享数据格式时，应同步更新 `packages/contracts/`。

## 测试规范

Python 测试使用 pytest，存放在 `apps/runtime/tests/` 下；文件名使用 `test_<feature>.py`，测试函数名使用 `test_<behavior>`。Go 测试使用标准 `testing` 包，文件以 `_test.go` 结尾；适合时使用表驱动测试。测试应隔离外部服务，避免调用真实模型供应商。提交跨服务改动前请运行 `make test`。

## 提交与拉取请求规范

近期提交记录采用 Conventional Commits 前缀，例如 `feat:`、`fix:`、`docs:`、`chore:`、`refactor:` 和 `test:`。每个提交应聚焦于一项改动，并在前缀后简要说明内容。拉取请求应描述所解决的问题或行为变化，列出相关验证命令；如有相关事项，请附上链接。基础设施改动还应说明对部署或配置的影响。

## 安全与配置

请以仓库中的示例配置为模板；不要提交 `.env` 文件、凭据、API 密钥或加密密钥。日志中不得记录机密信息、完整生成内容或带签名的存储 URL。修改数据库迁移、共享契约或服务边界时，应同步补充相应测试和文档。
