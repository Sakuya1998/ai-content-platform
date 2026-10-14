#!/usr/bin/env python3
"""
Monorepo 结构重组迁移脚本

此脚本将当前仓库结构重组为清晰的 Monorepo 布局。

执行前请确保：
1. 所有更改已提交
2. 创建了备份分支
3. 在新分支上执行

使用方法：
    python tools/migrate/restructure_monorepo.py --dry-run  # 预览
    python tools/migrate/restructure_monorepo.py            # 执行
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

# 迁移映射：(源路径, 目标路径)
MIGRATIONS: List[Tuple[str, str]] = [
    # Go Control Plane
    ("go-control-plane/", "apps/control/"),

    # Python Runtime
    ("src/agent_runtime/", "apps/runtime/agent_runtime/"),
    ("tests/", "apps/runtime/tests/"),
    ("pyproject.toml", "apps/runtime/pyproject.toml"),
    ("uv.lock", "apps/runtime/uv.lock"),

    # 共享契约
    ("contracts/", "packages/contracts/"),

    # 基础设施
    ("migrations/", "infra/migrations/"),
    ("docker-compose.platform.yml", "infra/docker/docker-compose.yml"),
    ("docker-compose.external.yml", "infra/docker/docker-compose.external.yml"),
    ("deploy/prometheus.yml", "infra/monitoring/prometheus.yml"),
    ("deploy/novel-agent-alerts.yml", "infra/monitoring/alerts.yml"),

    # 工具脚本
    ("scripts/platform_migrate.py", "tools/migrate/platform_migrate.py"),
    ("scripts/platform_backup.py", "tools/migrate/platform_backup.py"),
    ("scripts/platform_external_prepare.py", "tools/migrate/platform_external_prepare.py"),
    ("scripts/platform_storage_init.py", "tools/migrate/platform_storage_init.py"),
    ("scripts/platform_prompt_seed.py", "tools/seed/platform_prompt_seed.py"),
    ("scripts/check_architecture.py", "tools/dev/check_architecture.py"),
    ("scripts/check_deployment.py", "tools/dev/check_deployment.py"),

    # 文档
    ("openapi/", "docs/api/"),
    ("docs/architecture/adr-003-auth-topology.md", "docs/architecture/adr/003-auth-topology.md"),
    ("docs/architecture/adr-004-resource-version-snapshots.md", "docs/architecture/adr/004-resource-version-snapshots.md"),
]

# 需要创建的新目录
NEW_DIRECTORIES = [
    "apps/control",
    "apps/runtime",
    "packages/contracts",
    "infra/migrations",
    "infra/docker",
    "infra/monitoring",
    "tools/migrate",
    "tools/dev",
    "tools/seed",
    "docs/api",
    "docs/architecture/adr",
    "docs/guides",
    "docs/runbooks",
]

# 需要删除的旧目录（在迁移后）
OLD_DIRECTORIES_TO_REMOVE = [
    "go-control-plane",
    "src",
    "contracts",
    "migrations",
    "deploy",
    "scripts",
    "openapi",
]


def run_command(cmd: List[str], dry_run: bool = False) -> bool:
    """执行命令"""
    cmd_str = " ".join(cmd)
    if dry_run:
        print(f"[DRY RUN] {cmd_str}")
        return True

    print(f"[EXEC] {cmd_str}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] {result.stderr}", file=sys.stderr)
        return False
    return True


def check_git_status() -> bool:
    """检查 Git 状态"""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True
    )
    if result.stdout.strip():
        print("❌ 工作区不干净，请先提交或暂存所有更改")
        return False
    return True


def create_directories(dry_run: bool = False) -> bool:
    """创建新目录"""
    print("\n📁 创建新目录结构...")
    for directory in NEW_DIRECTORIES:
        if not dry_run:
            Path(directory).mkdir(parents=True, exist_ok=True)
        print(f"   ✓ {directory}")
    return True


def migrate_files(dry_run: bool = False) -> bool:
    """迁移文件"""
    print("\n📦 迁移文件...")

    for src, dst in MIGRATIONS:
        src_path = Path(src)
        dst_path = Path(dst)

        if not src_path.exists():
            print(f"   ⚠️  跳过（源不存在）: {src}")
            continue

        # 确保目标目录存在
        if not dry_run:
            dst_path.parent.mkdir(parents=True, exist_ok=True)

        # 使用 git mv 保留历史
        if src_path.is_dir() and not str(src).endswith('/'):
            src = f"{src}/"

        cmd = ["git", "mv", src, dst]
        if run_command(cmd, dry_run):
            print(f"   ✓ {src} → {dst}")
        else:
            print(f"   ❌ 失败: {src} → {dst}")
            return False

    return True


def cleanup_old_directories(dry_run: bool = False) -> bool:
    """清理旧目录"""
    print("\n🗑️  清理旧目录...")

    for directory in OLD_DIRECTORIES_TO_REMOVE:
        dir_path = Path(directory)
        if dir_path.exists() and not any(dir_path.iterdir()):
            cmd = ["git", "rm", "-r", directory]
            if run_command(cmd, dry_run):
                print(f"   ✓ 删除空目录: {directory}")
        elif dir_path.exists():
            print(f"   ⚠️  目录不为空，保留: {directory}")

    return True


def create_root_makefile(dry_run: bool = False) -> bool:
    """创建根目录 Makefile"""
    print("\n📝 创建根目录 Makefile...")

    makefile_content = """# Novel Agent Platform Monorepo Makefile

.PHONY: help build test lint clean docker-up docker-down

help:
\t@echo "Available commands:"
\t@echo "  make build        - Build all services"
\t@echo "  make test         - Run all tests"
\t@echo "  make lint         - Lint all code"
\t@echo "  make clean        - Clean build artifacts"
\t@echo "  make docker-up    - Start Docker Compose platform"
\t@echo "  make docker-down  - Stop Docker Compose platform"

# Build
build: build-control build-runtime

build-control:
\t@echo "Building Control Plane..."
\tcd apps/control && go build ./cmd/api

build-runtime:
\t@echo "Building Runtime..."
\tcd apps/runtime && uv sync --locked --all-extras

# Test
test: test-control test-runtime

test-control:
\t@echo "Testing Control Plane..."
\tcd apps/control && go test -race ./...

test-runtime:
\t@echo "Testing Runtime..."
\tcd apps/runtime && uv run --locked pytest

# Lint
lint: lint-control lint-runtime

lint-control:
\t@echo "Linting Control Plane..."
\tcd apps/control && go vet ./...

lint-runtime:
\t@echo "Linting Runtime..."
\tcd apps/runtime && uv run --locked ruff check .

# Clean
clean:
\t@echo "Cleaning build artifacts..."
\tcd apps/control && go clean
\trm -rf apps/runtime/.pytest_cache apps/runtime/.ruff_cache

# Docker
docker-up:
\t@echo "Starting Docker Compose..."
\tcd infra/docker && docker compose up -d

docker-down:
\t@echo "Stopping Docker Compose..."
\tcd infra/docker && docker compose down

# Development
dev-control:
\tcd apps/control && go run ./cmd/api

dev-runtime:
\tcd apps/runtime && uv run --locked uvicorn agent_runtime.api.app:app --reload

# Database migrations
migrate:
\tcd apps/runtime && uv run --locked python ../../tools/migrate/platform_migrate.py

# Format code
format:
\tcd apps/control && go fmt ./...
\tcd apps/runtime && uv run --locked ruff format .
"""

    if not dry_run:
        Path("Makefile").write_text(makefile_content)
    print("   ✓ Makefile 已创建")
    return True


def update_root_readme(dry_run: bool = False) -> bool:
    """更新根目录 README"""
    print("\n📝 创建根目录 README...")

    readme_content = """# Novel Agent Platform

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
"""

    if not dry_run:
        # 备份原 README
        if Path("README.md").exists():
            Path("README.old.md").write_text(Path("README.md").read_text())
        Path("README.md").write_text(readme_content)
    print("   ✓ README.md 已更新")
    return True


def main():
    parser = argparse.ArgumentParser(description="重组 Monorepo 结构")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式，不实际执行操作"
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="自动确认，不询问"
    )
    args = parser.parse_args()

    print("=" * 80)
    print("Novel Agent Monorepo 结构重组")
    print("=" * 80)

    if args.dry_run:
        print("\n⚠️  DRY RUN 模式 - 仅预览，不会实际执行\n")
    else:
        print("\n⚠️  即将重组仓库结构\n")
        if not args.yes:
            response = input("确认继续？(yes/no): ")
            if response.lower() != "yes":
                print("已取消")
                return
        else:
            print("自动确认模式，开始执行...\n")

    # 检查 Git 状态
    if not args.dry_run and not check_git_status():
        sys.exit(1)

    # 执行迁移步骤
    steps = [
        ("创建目录结构", create_directories),
        ("迁移文件", migrate_files),
        ("清理旧目录", cleanup_old_directories),
        ("创建 Makefile", create_root_makefile),
        ("更新 README", update_root_readme),
    ]

    for step_name, step_func in steps:
        print(f"\n{'=' * 80}")
        print(f"步骤: {step_name}")
        print('=' * 80)
        if not step_func(args.dry_run):
            print(f"\n❌ {step_name} 失败")
            sys.exit(1)

    print("\n" + "=" * 80)
    if args.dry_run:
        print("✅ 预览完成！使用不带 --dry-run 参数执行实际迁移")
    else:
        print("✅ 迁移完成！")
        print("\n下一步:")
        print("1. 检查迁移结果: git status")
        print("2. 更新配置文件路径")
        print("3. 运行测试: make test")
        print("4. 提交更改: git commit -m 'refactor: restructure to monorepo layout'")
    print("=" * 80)


if __name__ == "__main__":
    main()
