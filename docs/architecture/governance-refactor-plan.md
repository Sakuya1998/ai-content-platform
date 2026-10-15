# Governance 模块重构计划

**日期**：2026-10-15  
**状态**：进行中  
**负责人**：Claude Opus 5.5

---

## 背景

governance 模块当前包含 2006 行代码，职责过多且耦合严重。最大的两个文件：
- `postgres/governance.go` (791 行) - 模型路由的所有 CRUD
- `postgres/prompt_catalog_commands.go` (760 行) - Prompt 的所有写操作

## 目标

将大文件拆分为多个职责单一的小文件，每个文件 < 400 行，提升可维护性和可测试性。

## 拆分方案

### 方案选择：按子目录拆分（方案 A）✅

创建独立的子包（models/, prompts/, policies/, resources/），每个子包负责一类功能。

**优势**：
1. 清晰的模块边界，职责单一
2. 更好的封装性，减少耦合
3. 便于独立测试和扩展
4. 符合 Go 标准库组织风格
5. 长期可维护性强

### 目标结构

```
postgres/
├── repository.go              # 主仓储入口（~120 行）
├── encryption.go              # 加密工具（~80 行）
├── catalog_io.go              # HTTP 辅助函数（保持）
├── domain.go                  # 类型别名（保持）
├── execution_catalog.go       # 执行目录接口（保持）
│
├── models/                    # 模型路由子包
│   ├── routes.go             # 路由冻结和解析（~350 行）
│   ├── profiles.go           # 模型配置 CRUD（~350 行）
│   └── testing.go            # 模型测试（~100 行）
│
├── prompts/                   # Prompt 子包
│   ├── catalog.go            # 查询操作（~150 行）
│   ├── commands.go           # Prompt 写操作（~400 行）
│   └── packs.go              # Pack 写操作（~360 行）
│
├── policies/                  # 策略子包
│   ├── catalog.go            # 查询操作（~60 行）
│   └── commands.go           # 写操作（~335 行）
│
└── resources/                 # 通用资源子包
    ├── catalog.go            # 资源 CRUD（~200 行）
    └── builtins.go           # 内置资源（~233 行）
```

### 拆分详情

#### 第一步：创建子包结构

创建 4 个子包目录：
- `postgres/models/`
- `postgres/prompts/`
- `postgres/policies/`
- `postgres/resources/`

#### 第二步：迁移 models 相关代码

**从 governance.go 提取到 models/ 子包**：

1. `models/routes.go`
   - `freezeTarget()` - 冻结路由目标
   - `resolveTarget()` - 解析路由
   - `ResolveFrozenRoutes()` - 批量解析
   - `ResolveFrozenRoute()` - 单个路由解析
   - `CurrentRouteVersion()` - 当前版本

2. `models/profiles.go`
   - `scanModelProfile()` - 扫描模型配置
   - `executeModel()` - HTTP 处理
   - 模型配置的完整 CRUD

3. `models/testing.go`
   - 模型测试相关功能

#### 第三步：迁移 prompts 相关代码

**从现有文件提取到 prompts/ 子包**：

1. `prompts/catalog.go`
   - 来自 `prompt_catalog.go`
   - 查询操作

2. `prompts/commands.go`
   - 来自 `prompt_catalog_commands.go`
   - Prompt 写操作

3. `prompts/packs.go`
   - 来自 `prompt_catalog_commands.go`
   - Pack 操作

#### 第四步：迁移 policies 相关代码

**从现有文件提取到 policies/ 子包**：

1. `policies/catalog.go`
   - 来自 `creation_policy.go`

2. `policies/commands.go`
   - 来自 `creation_policy_commands.go`

#### 第五步：迁移 resources 相关代码

**从 governance.go 提取到 resources/ 子包**：

1. `resources/catalog.go`
   - `FrozenResource()`
   - `executeResource()`
   - `FreezeSnapshot()`

2. `resources/builtins.go`
   - 来自 `governance_builtins.go`

---

## 实施步骤

### 阶段 1：准备工作（10 分钟）

- [x] 创建此文档
- [ ] 备份当前代码（git stash）
- [ ] 创建重构分支 `refactor/governance-split`
- [ ] 运行所有测试，确保初始状态正常

### 阶段 2：创建子包目录结构（5 分钟）

- [ ] 创建子包目录
  ```bash
  cd apps/control/internal/modules/governance/postgres
  mkdir -p models prompts policies resources
  ```

- [ ] 创建包文档文件
  - [ ] `models/doc.go`
  - [ ] `prompts/doc.go`
  - [ ] `policies/doc.go`
  - [ ] `resources/doc.go`

### 阶段 3：迁移 models 子包（30 分钟）

- [ ] 创建 `models/routes.go`
  - [ ] 从 governance.go 提取路由相关函数
  - [ ] 调整包名为 `package models`
  - [ ] 添加必要的 import
  - [ ] 验证编译

- [ ] 创建 `models/profiles.go`
  - [ ] 从 governance.go 提取模型配置函数
  - [ ] 调整包名
  - [ ] 验证编译

- [ ] 创建 `models/testing.go`
  - [ ] 提取模型测试函数
  - [ ] 验证编译

### 阶段 4：迁移 prompts 子包（25 分钟）

- [ ] 创建 `prompts/catalog.go`
  - [ ] 从 prompt_catalog.go 迁移
  - [ ] 调整包名为 `package prompts`
  - [ ] 验证编译

- [ ] 创建 `prompts/commands.go`
  - [ ] 从 prompt_catalog_commands.go 提取 Prompt 操作
  - [ ] 验证编译

- [ ] 创建 `prompts/packs.go`
  - [ ] 从 prompt_catalog_commands.go 提取 Pack 操作
  - [ ] 验证编译

### 阶段 5：迁移 policies 子包（15 分钟）

- [ ] 创建 `policies/catalog.go`
  - [ ] 从 creation_policy.go 迁移
  - [ ] 调整包名为 `package policies`
  - [ ] 验证编译

- [ ] 创建 `policies/commands.go`
  - [ ] 从 creation_policy_commands.go 迁移
  - [ ] 验证编译

### 阶段 6：迁移 resources 子包（15 分钟）

- [ ] 创建 `resources/catalog.go`
  - [ ] 从 governance.go 提取资源相关函数
  - [ ] 调整包名为 `package resources`
  - [ ] 验证编译

- [ ] 创建 `resources/builtins.go`
  - [ ] 从 governance_builtins.go 迁移
  - [ ] 验证编译

### 阶段 7：更新主仓储（20 分钟）

- [ ] 更新 `repository.go`
  - [ ] Import 新的子包
  - [ ] 调整方法调用到子包
  - [ ] 验证编译

- [ ] 创建 `encryption.go`
  - [ ] 从 repositories.go 提取加密功能
  - [ ] 验证编译

- [ ] 删除旧文件
  - [ ] 删除 governance.go
  - [ ] 删除 prompt_catalog_commands.go
  - [ ] 删除 creation_policy.go
  - [ ] 删除 creation_policy_commands.go
  - [ ] 删除 governance_builtins.go
  - [ ] 删除 prompt_catalog.go
  - [ ] 删除旧的 repositories.go（保留新的 repository.go）

### 阶段 5：验证和清理（15 分钟）

- [ ] 运行所有单元测试
  ```bash
  cd apps/control
  go test -race ./internal/modules/governance/...
  ```

- [ ] 运行集成测试
  ```bash
  go test -race ./...
  ```

- [ ] 代码格式化
  ```bash
  go fmt ./internal/modules/governance/...
  ```

- [ ] Vet 检查
  ```bash
  go vet ./internal/modules/governance/...
  ```

- [ ] 更新文档
  - [ ] 更新 `docs/control-plane-overview.md`
  - [ ] 标记此任务为完成

### 阶段 6：提交和合并（10 分钟）

- [ ] 提交代码
  ```bash
  git add .
  git commit -m "refactor: split governance module into smaller files

  - Split governance.go (791 lines) into:
    - model_routes.go (~350 lines)
    - model_profiles.go (~350 lines)
    - governance_resources.go (~100 lines)
  
  - Split prompt_catalog_commands.go (760 lines) into:
    - prompt_commands.go (~400 lines)
    - prompt_pack_commands.go (~360 lines)
  
  - Split repositories.go (198 lines) into:
    - repository.go (~120 lines)
    - encryption.go (~80 lines)
  
  All tests pass. No functional changes.
  
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  ```

- [ ] 创建 Pull Request
- [ ] Code Review
- [ ] 合并到 main

---

## 风险评估

### 风险等级：低

**理由**：
1. 只是文件拆分，不修改逻辑
2. 不改变 API 接口
3. 不调整 import 路径
4. 测试覆盖率 >70%

### 潜在问题

1. **编译错误**
   - 可能：低
   - 影响：编译时发现
   - 缓解：逐个文件验证编译

2. **测试失败**
   - 可能：低
   - 影响：测试时发现
   - 缓解：每个阶段运行测试

3. **遗漏函数**
   - 可能：低
   - 影响：编译或测试失败
   - 缓解：仔细检查，使用 grep 确认

---

## 回滚计划

如果出现问题：

1. **立即回滚**
   ```bash
   git reset --hard HEAD~1
   # 或
   git revert <commit-hash>
   ```

2. **部分回滚**
   - 恢复单个文件：`git checkout HEAD~1 -- path/to/file.go`

3. **问题修复**
   - 分析失败原因
   - 创建修复分支
   - 重新测试和验证

---

## 成功标准

- [ ] 所有文件 < 400 行
- [ ] 所有测试通过（单元 + 集成）
- [ ] 代码格式化和 vet 检查通过
- [ ] 编译无警告
- [ ] 文档更新完成

---

## 后续工作

完成文件拆分后，下一阶段可以考虑：

1. 为每个子模块增加单元测试
2. 提取通用的辅助函数
3. 优化错误处理
4. 增加更多文档注释

---

**更新日志**：
- 2026-10-15：创建重构计划
