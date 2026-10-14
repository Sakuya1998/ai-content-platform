# 仓库重命名运行手册：novel-agent → ai-content-platform

## 背景

项目已从单一小说创作扩展为通用 AI 内容创作平台，支持视频、图片、音频、文本等多种内容类型。重命名以反映实际功能范围。

## 重命名范围

- GitHub 仓库: `novel-agent` → `ai-content-platform`
- Go 模块: `github.com/sakuya/novel-agent` → `github.com/sakuya/ai-content-platform`
- Python 包: `novel-agent` → `ai-content-platform`
- 本地目录: `/home/sakuya/projects/novel-agent` → `/home/sakuya/projects/ai-content-platform`

## 执行步骤

### 第 1 步：在 GitHub 上重命名仓库（手动）

1. 访问 https://github.com/Sakuya1998/novel-agent/settings
2. 滚动到 "Repository name" 部分
3. 将 `novel-agent` 改为 `ai-content-platform`
4. 点击 "Rename" 按钮确认

GitHub 会自动设置从旧名称的重定向，不会立即破坏克隆。

### 第 2 步：更新 Go 模块路径

```bash
cd /home/sakuya/projects/novel-agent/apps/control

# 更新 go.mod
sed -i 's|github.com/sakuya/novel-agent|github.com/sakuya/ai-content-platform|g' go.mod

# 更新所有 Go 文件的导入路径
find . -name "*.go" -type f -exec sed -i 's|github.com/sakuya/novel-agent|github.com/sakuya/ai-content-platform|g' {} \;

# 整理依赖
go mod tidy

# 验证构建
go build -o api ./cmd/api
go test ./...
```

### 第 3 步：更新 Python 包名

```bash
cd /home/sakuya/projects/novel-agent/apps/runtime

# 更新 pyproject.toml
sed -i 's/name = "novel-agent"/name = "ai-content-platform"/g' pyproject.toml

# 验证构建
uv sync --locked
```

### 第 4 步：更新文档和配置

```bash
cd /home/sakuya/projects/novel-agent

# 更新 README.md
sed -i 's/Novel Agent/AI Content Platform/g' README.md
sed -i 's/novel-agent/ai-content-platform/g' README.md

# 更新 CLAUDE.md
sed -i 's/novel-agent/ai-content-platform/g' CLAUDE.md

# 更新 Docker Compose
sed -i 's/novel-agent/ai-content-platform/g' infra/docker/docker-compose*.yml

# 更新 Makefile
sed -i 's/novel-agent/ai-content-platform/g' Makefile

# 更新 CI/CD
sed -i 's/novel-agent/ai-content-platform/g' .github/workflows/*.yml
```

### 第 5 步：更新 Git 远程地址

```bash
cd /home/sakuya/projects/novel-agent

# 更新远程 URL
git remote set-url origin https://github.com/Sakuya1998/ai-content-platform.git

# 验证
git remote -v
```

### 第 6 步：重命名本地目录

```bash
cd /home/sakuya/projects
mv novel-agent ai-content-platform
cd ai-content-platform
```

### 第 7 步：提交所有更改

```bash
git add -A
git commit -m "refactor: rename project from novel-agent to ai-content-platform

Rename project to reflect expanded scope as a general-purpose
AI content creation platform supporting video, images, audio,
and text generation.

- Update Go module path throughout
- Update Python package name
- Update all documentation
- Update CI/CD workflows
- Update Docker Compose configs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"

git push origin main
```

### 第 8 步：验证

```bash
# 验证 Go 构建
cd apps/control
go build ./cmd/api
go test ./...

# 验证 Python 构建
cd ../runtime
uv sync --locked

# 验证 Docker
cd ../../infra/docker
docker compose config

# 验证远程连接
git pull
git log --oneline -3
```

## 回滚步骤（如果需要）

如果重命名过程中遇到问题：

1. 在 GitHub 上将仓库重命名回 `novel-agent`
2. 本地执行：
   ```bash
   cd /home/sakuya/projects/ai-content-platform
   git remote set-url origin https://github.com/Sakuya1998/novel-agent.git
   git reset --hard HEAD~1  # 撤销重命名提交
   cd ..
   mv ai-content-platform novel-agent
   ```

## 注意事项

- GitHub 的重定向会保留一段时间，旧的克隆仍能工作
- 团队成员需要更新他们的远程 URL 或重新克隆
- CI/CD 需要重新触发以使用新路径
- 如果有外部系统引用旧名称，需要单独更新

## 影响评估

### 不受影响
- Git 历史和提交 SHA
- 本地已克隆的仓库（有重定向）
- Docker 镜像名（可单独配置）

### 需要更新
- 所有 import 语句
- 文档和 README
- CI/CD 配置
- 开发者本地远程 URL（建议）
- 部署脚本中的仓库引用

## 完成时间估计

- GitHub 重命名: 1 分钟
- 代码更新: 5 分钟
- 验证测试: 5 分钟
- 总计: ~15 分钟
