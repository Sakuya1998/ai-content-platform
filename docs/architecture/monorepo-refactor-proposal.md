# Monorepo 重构提案

## 当前问题

### 1. 构建根目录混乱
- Python `pyproject.toml` 在仓库根
- Go `go.mod` 在 `go-control-plane/`
- 不清晰谁是主项目，职责不明

### 2. 目录职责不清
- `scripts/` 混杂平台脚本和迁移工具
- `tests/` 只测试 Python，Go 测试在各自模块内
- `contracts/` 共享契约但位置不突出

### 3. 配置文件散落
- 4 个 `.env.*.example` 在根目录
- `config.example.yaml` 在 Go 子目录
- 2 个 `docker-compose.yml` 在根目录

### 4. 文档组织混乱
- `docs/` 混合架构、运维、迁移文档
- `README.md` 和 `go-control-plane/README.md` 重复
- `openapi/` 契约文档独立

## 提议的 Monorepo 结构

### 方案 A：应用优先（推荐）

```
novel-agent/                    # Monorepo 根目录
├── README.md                   # 平台整体说明
├── CLAUDE.md                   # AI 助手指导
│
├── apps/                       # 应用服务
│   ├── control/                # Go 管理面应用
│   │   ├── cmd/
│   │   │   ├── api/           # HTTP API 服务
│   │   │   └── worker/        # MQ Worker（如果需要）
│   │   ├── internal/
│   │   │   ├── app/           # 应用组装
│   │   │   ├── modules/       # 业务模块
│   │   │   ├── transport/     # HTTP/MQ 适配器
│   │   │   └── platform/      # 基础设施
│   │   ├── go.mod
│   │   ├── go.sum
│   │   ├── Dockerfile
│   │   ├── README.md
│   │   └── .env.example
│   │
│   └── runtime/                # Python 执行运行时应用
│       ├── agent_runtime/      # 主包
│       │   ├── domain/
│       │   ├── execution/
│       │   ├── infrastructure/
│       │   ├── workflows/
│       │   ├── prompts/
│       │   ├── api/
│       │   ├── entrypoints/
│       │   └── settings.py
│       ├── tests/              # Runtime 测试
│       ├── pyproject.toml
│       ├── uv.lock
│       ├── Dockerfile
│       ├── README.md
│       └── .env.example
│
├── packages/                   # 共享包（可选，未来扩展）
│   ├── contracts/              # 跨语言契约
│   │   ├── README.md
│   │   ├── content-ir.schema.json
│   │   ├── execution-snapshot.schema.json
│   │   └── validate_examples.py
│   └── protos/                 # gRPC proto（如果使用）
│
├── infra/                      # 基础设施配置
│   ├── migrations/             # 数据库迁移
│   │   ├── control/
│   │   ├── runtime/
│   │   └── seeds/
│   ├── docker/                 # Docker 配置
│   │   ├── docker-compose.yml
│   │   ├── docker-compose.external.yml
│   │   └── docker-compose.dev.yml
│   ├── k8s/                    # Kubernetes 清单
│   │   ├── base/
│   │   └── overlays/
│   ├── terraform/              # 基础设施即代码（如果使用）
│   └── monitoring/             # 监控配置
│       ├── prometheus.yml
│       └── alerts.yml
│
├── tools/                      # 工具脚本
│   ├── migrate/                # 迁移脚本
│   │   ├── platform_migrate.py
│   │   ├── platform_backup.py
│   │   └── platform_legacy_*.py
│   ├── dev/                    # 开发工具
│   │   ├── check_architecture.py
│   │   └── check_deployment.py
│   └── seed/                   # 种子数据初始化
│       └── platform_prompt_seed.py
│
├── docs/                       # 文档
│   ├── README.md               # 文档索引
│   ├── architecture/           # 架构决策和设计
│   │   ├── README.md
│   │   ├── adr/                # 架构决策记录
│   │   ├── control-plane.md
│   │   ├── runtime.md
│   │   └── messaging.md
│   ├── api/                    # API 文档
│   │   ├── control-v2.json     # OpenAPI
│   │   └── README.md
│   ├── guides/                 # 指南
│   │   ├── development.md
│   │   ├── deployment.md
│   │   └── operations.md
│   └── runbooks/               # 运维手册
│       ├── backup-restore.md
│       └── incident-response.md
│
├── .github/                    # GitHub 配置
│   └── workflows/
│       ├── ci.yml
│       └── release.yml
│
├── .gitignore                  # Git 忽略规则
└── Makefile                    # 统一任务脚本（可选）
```

### 方案 B：服务独立（备选）

```
novel-agent/
├── services/                   # 独立服务
│   ├── control-plane/         # Go 服务（完整独立）
│   └── agent-runtime/         # Python 服务（完整独立）
├── shared/                     # 共享资源
│   ├── contracts/
│   ├── migrations/
│   └── protos/
├── deployment/                 # 部署配置
│   ├── docker-compose/
│   └── k8s/
├── tools/                      # 工具脚本
└── docs/                       # 文档
```

### 方案 C：领域驱动（高级）

```
novel-agent/
├── platform/                   # 平台服务
│   ├── control/               # 管理面
│   └── runtime/               # 运行时
├── domains/                    # 领域层（如果未来拆分）
│   ├── content/
│   ├── workflow/
│   └── knowledge/
├── infrastructure/             # 基础设施层
│   ├── database/
│   ├── messaging/
│   └── storage/
└── ...
```

## 推荐方案：方案 A（应用优先）

### 优势

1. **职责清晰**
   - `apps/` 包含可部署的应用
   - `packages/` 包含共享代码（契约、工具库）
   - `infra/` 包含基础设施配置
   - `tools/` 包含开发和运维工具
   - `docs/` 包含所有文档

2. **构建独立**
   - 每个应用有自己的构建配置
   - 可以独立开发、测试、部署
   - 依赖关系明确

3. **扩展性好**
   - 未来可添加更多应用（如 `apps/scheduler/`）
   - 可添加共享包（如 `packages/common-go/`）
   - 支持微服务演进

4. **工具友好**
   - IDE 可以正确识别每个应用
   - 各自的 `go.mod` 和 `pyproject.toml` 独立
   - 测试和 lint 配置独立

5. **符合业界实践**
   - Nx、Turborepo、Rush 等工具的标准结构
   - 类似 Google、Uber、Netflix 的 Monorepo 实践

## 迁移步骤

### 阶段 1：创建新结构（保持代码不变）

```bash
# 1. 创建新目录结构
mkdir -p apps/control apps/runtime
mkdir -p packages/contracts
mkdir -p infra/{migrations,docker,monitoring}
mkdir -p tools/{migrate,dev,seed}
mkdir -p docs/{architecture/adr,api,guides,runbooks}

# 2. 移动 Go Control
git mv go-control-plane/* apps/control/
git mv go-control-plane/.* apps/control/ 2>/dev/null || true

# 3. 移动 Python Runtime
git mv src/agent_runtime apps/runtime/
git mv tests apps/runtime/
git mv pyproject.toml apps/runtime/
git mv uv.lock apps/runtime/

# 4. 移动共享资源
git mv contracts packages/
git mv migrations infra/
git mv docker-compose*.yml infra/docker/
git mv deploy/* infra/

# 5. 移动工具脚本
git mv scripts/platform_migrate.py tools/migrate/
git mv scripts/platform_*.py tools/migrate/
git mv scripts/check_*.py tools/dev/

# 6. 重组文档
git mv openapi docs/api/
git mv docs/architecture/*.md docs/architecture/
# 进一步细分...
```

### 阶段 2：更新配置文件

1. 更新 `apps/control/Dockerfile`
2. 更新 `apps/runtime/Dockerfile`
3. 更新 `infra/docker/docker-compose.yml` 中的路径
4. 更新 `.github/workflows/` 中的路径
5. 创建根目录 `Makefile` 或 `package.json` 统一任务

### 阶段 3：更新文档

1. 更新所有 README.md
2. 更新 CLAUDE.md
3. 更新架构图和文档引用

### 阶段 4：验证和测试

```bash
# Go 服务
cd apps/control
go test -race ./...
docker build -t control:test .

# Python 服务
cd apps/runtime
uv run --locked pytest
docker build -t runtime:test .

# 平台集成
cd infra/docker
docker compose up -d
```

## 配置管理

### 环境变量命名

```
apps/control/.env.example       # Control 配置模板
apps/runtime/.env.example       # Runtime 配置模板
infra/docker/.env.example       # Docker Compose 配置
```

### 统一任务管理（Makefile）

```makefile
# 根目录 Makefile
.PHONY: help build test lint docker-up

help:
	@echo "Available commands:"
	@echo "  make build        - Build all services"
	@echo "  make test         - Run all tests"
	@echo "  make lint         - Lint all code"
	@echo "  make docker-up    - Start Docker Compose"

build:
	@echo "Building Control..."
	cd apps/control && go build ./cmd/api
	@echo "Building Runtime..."
	cd apps/runtime && uv sync --locked

test:
	@echo "Testing Control..."
	cd apps/control && go test -race ./...
	@echo "Testing Runtime..."
	cd apps/runtime && uv run --locked pytest

lint:
	cd apps/control && go vet ./...
	cd apps/runtime && uv run --locked ruff check .

docker-up:
	cd infra/docker && docker compose up -d

docker-down:
	cd infra/docker && docker compose down
```

## 决策点

### 需要决定的问题

1. **是否使用 Workspace 工具？**
   - Go: 原生 Go Workspace (`go.work`)
   - Python: UV Workspace 或独立项目
   - 推荐：保持独立，简化构建

2. **是否需要根目录构建脚本？**
   - Makefile（简单）
   - npm scripts（需要 package.json）
   - Task（Go 工具）
   - 推荐：Makefile

3. **CI/CD 如何组织？**
   - 单个 workflow 包含所有任务
   - 每个应用独立 workflow
   - 推荐：按应用独立，共享 CI 步骤

4. **契约共享方式？**
   - 保持 JSON Schema
   - 引入 Protobuf
   - 推荐：保持 JSON Schema

## 风险评估

### 低风险
- 目录重组（Git 保留历史）
- 文档整理
- 配置文件移动

### 中风险
- 构建脚本更新
- CI/CD 流程调整
- 开发工作流变化

### 高风险
- 打破正在进行的开发
- 影响外部部署环境

## 建议执行时机

1. **当前分支已合并后**
2. **新功能开发前**
3. **创建专门的重构分支**
4. **逐步迁移，保持可回退**

## 实施计划

### Week 1: 准备
- 冻结当前分支
- 文档化现有依赖
- 创建迁移脚本

### Week 2: 重组
- 执行目录移动
- 更新配置文件
- 更新文档

### Week 3: 验证
- 运行所有测试
- 验证构建流程
- 验证部署流程

### Week 4: 收尾
- 更新 CI/CD
- 培训团队
- 合并到主分支

## 参考资料

- [Nx Monorepo](https://nx.dev/)
- [Google Monorepo](https://cacm.acm.org/magazines/2016/7/204032-why-google-stores-billions-of-lines-of-code-in-a-single-repository/)
- [Uber Monorepo](https://eng.uber.com/monorepo-bazel-migration/)
