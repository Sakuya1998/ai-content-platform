# Novel Agent Platform

通用 AI 内容创作平台 Monorepo。

## 仓库结构

```
novel-agent/
├── apps/                   # 应用服务
│   ├── control/           # Go 管理面
│   └── runtime/           # Python 执行运行时
├── packages/              # 共享包
│   └── contracts/         # 服务间契约
├── infra/                 # 基础设施
│   ├── migrations/        # 数据库迁移
│   ├── docker/           # Docker Compose
│   └── monitoring/       # 监控配置
├── tools/                 # 工具脚本
│   ├── migrate/          # 迁移工具
│   ├── dev/              # 开发工具
│   └── seed/             # 种子数据
└── docs/                  # 文档
    ├── architecture/      # 架构设计
    ├── api/              # API 文档
    ├── guides/           # 开发指南
    └── runbooks/         # 运维手册
```

## 快速开始

### 环境要求

- Go 1.23+
- Python 3.14+
- uv 0.12.1+
- Docker & Docker Compose

### 本地开发

```bash
# 构建所有服务
make build

# 运行测试
make test

# 启动平台
make docker-up

# 停止平台
make docker-down
```

### 独立开发

**Go Control Plane**:
```bash
cd apps/control
cp .env.example .env
go run ./cmd/api
```

**Python Runtime**:
```bash
cd apps/runtime
cp .env.example .env
uv run --locked uvicorn agent_runtime.api.app:app --reload
```

## 文档

- [架构说明](docs/architecture/)
- [开发指南](docs/guides/)
- [API 文档](docs/api/)
- [运维手册](docs/runbooks/)

## 贡献

请参阅 [CLAUDE.md](CLAUDE.md) 了解项目结构和开发规范。
