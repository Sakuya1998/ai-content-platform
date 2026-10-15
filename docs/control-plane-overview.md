# Control Plane 全面概览

本文档为 AI Content Platform 的 Go Control Plane 提供全面的架构和实现概览。

## 目录

- [核心职责](#核心职责)
- [架构设计](#架构设计)
- [代码组织](#代码组织)
- [关键模块详解](#关键模块详解)
- [数据流与消息传递](#数据流与消息传递)
- [安全与治理](#安全与治理)
- [开发与测试](#开发与测试)

---

## 核心职责

Control Plane 是整个平台的**控制中心和协调层**，负责：

### 1. **业务逻辑与状态管理**
- 工作区、项目、文档版本的 CRUD
- 任务生命周期管理（创建、查询、取消、恢复）
- 内容时间线和 IR（中间表示）管理
- 生产调度（素材生成、自动成片）

### 2. **治理与安全**
- 身份认证和授权（Session + CSRF）
- 工作区级别的资源隔离（Row-Level Security）
- 模型路由配置（文本、图像、语音、视频）
- Prompt 目录和策略管理
- 能力封装（Skills、Tools、MCP Policy、知识包）

### 3. **执行协调**
- 生成不可变的执行快照（含工作流、模型、Prompt、策略）
- 通过 RocketMQ 向 Runtime 发送命令
- 接收 Runtime 事件并投影到查询模型
- 管理任务状态机和幂等性

### 4. **对象存储管理**
- 管理 S3 上的执行快照、产物、传输文件
- 使用 SHA-256 校验完整性
- 按工作区、项目、资源组织对象键

### 5. **可观测性**
- 健康检查（`/healthz`、`/readyz`）
- SSE 实时推送任务进度
- 审计日志和基准测试记录

---

## 架构设计

### 服务拓扑

```
┌─────────────┐
│   Browser   │ HTTP/SSE
└──────┬──────┘
       │
       v
┌─────────────────────────────────────────────┐
│          Go Control Plane                    │
│  ┌─────────────┐    ┌──────────────────┐   │
│  │  HTTP API   │    │  MQ Command      │   │
│  │  (Chi Router)│   │  Publisher       │   │
│  └─────────────┘    └──────────────────┘   │
│          │                    │              │
│          v                    v              │
│  ┌─────────────┐    ┌──────────────────┐   │
│  │  Business   │    │  Event Projector │   │
│  │  Modules    │    │  (MQ Consumer)   │   │
│  └─────────────┘    └──────────────────┘   │
│          │                    ^              │
│          v                    │              │
│  ┌─────────────────────────────────────┐   │
│  │    Control PostgreSQL               │   │
│  │    (RLS + Outbox Pattern)          │   │
│  └─────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
                  │                ^
                  │ Commands       │ Events
                  v                │
         ┌────────────────────────────┐
         │    RocketMQ 5.x Proxy      │
         └────────────────────────────┘
                  │                ^
                  v                │
┌─────────────────────────────────────────────┐
│         Python Runtime                       │
│  ┌─────────────┐    ┌──────────────────┐   │
│  │ Execution   │    │  Event Publisher │   │
│  │ Engine      │    │  (Outbox)        │   │
│  └─────────────┘    └──────────────────┘   │
└─────────────────────────────────────────────┘
                  │
                  v
         ┌────────────────┐
         │  S3 (RustFS)   │
         │  Artifacts     │
         └────────────────┘
```

### 关键设计原则

1. **异步解耦**：Go 和 Python 之间**零同步 HTTP 调用**，只通过消息和对象传递
2. **Outbox Pattern**：保证命令/事件与数据库状态的原子性
3. **不可变快照**：执行快照一旦生成，不可修改，Runtime 不回调 Control
4. **投影查询**：Go 维护 Runtime 事件的查询投影，避免跨服务查询
5. **幂等性**：命令和事件都有唯一键，支持重试和去重

---

## 代码组织

### 目录结构

```
apps/control/
├── cmd/
│   ├── api/              # 主服务入口（HTTP + MQ Worker）
│   └── mq-check/         # MQ 连接诊断工具
├── internal/
│   ├── app/              # 应用组装层（依赖注入、生命周期）
│   ├── contracts/        # Go/Python 消息契约（DTO）
│   ├── modules/          # 业务模块（11 个领域）
│   │   ├── content/      # 内容 IR 和版本
│   │   ├── evaluation/   # 评测和基准测试
│   │   ├── execution/    # 任务执行快照和投影
│   │   ├── governance/   # 模型、Prompt、策略目录
│   │   ├── identity/     # 用户和工作区认证
│   │   ├── knowledge/    # 知识包管理
│   │   ├── observability/# 审计日志
│   │   ├── production/   # 素材生成和成片调度
│   │   ├── project/      # 项目管理
│   │   ├── task/         # 任务状态机
│   │   └── workflow/     # 工作流版本
│   ├── platform/         # 基础设施适配器
│   │   ├── config/       # Viper 配置加载
│   │   ├── database/     # pgx 连接池 + RLS
│   │   ├── logger/       # slog 结构化日志
│   │   ├── messaging/    # RocketMQ 客户端
│   │   ├── objectstorage/# S3 SDK 封装
│   │   ├── password/     # Argon2id 哈希
│   │   ├── security/     # AES-256-GCM 加密
│   │   └── modelprovider/# 模型配置校验
│   └── transport/
│       └── http/         # Chi 路由和 HTTP 处理器
│           ├── api/      # 主路由器和 CORS
│           ├── governance/# 治理接口
│           ├── identity/ # 登录/登出
│           ├── operations/# 内部操作 API
│           ├── production/# 生产接口
│           ├── resources/# 资源导入导出
│           └── workflow/ # 工作流接口
├── config.example.yaml   # 配置模板
├── go.mod                # Go 模块定义
└── README.md             # 快速开始
```

### 依赖方向

```
HTTP/MQ Transport
       ↓
   Modules (Domain + Use Cases)
       ↓
   Platform (Infra Adapters)
```

- **Transport** 调用 **Modules**，不直接访问数据库
- **Modules** 定义接口，**Platform** 实现接口
- **Modules** 之间通过接口依赖，避免循环依赖
- **app** 层负责组装，其他层不做全局查找

---

## 关键模块详解

### 1. **app** - 应用组装层

**职责**：显式依赖注入和生命周期管理

**核心代码**：`internal/app/app.go`

```go
func New(ctx context.Context, cfg config.Config, logger *slog.Logger) (*Application, error) {
    // 1. 创建数据库连接池
    pool, err := database.Open(...)
    
    // 2. 创建各个 Repository（Postgres 适配器）
    identityRepo := identitypostgres.NewRepository(pool)
    governanceRepo := governancepostgres.NewRepository(pool, encryptionKey, ...)
    messagesRepo := executionpostgres.NewMQRepository(pool, mqTopics, ...)
    
    // 3. 创建 Service（业务用例）
    identityService := identity.NewService(identityRepo, passwordCodec, ...)
    catalogService := governance.NewCatalogService(governanceRepo, ...)
    workflowService := workflow.NewService(workflowRepo)
    
    // 4. 创建 HTTP Handler
    identityHandler := identityhttp.NewHandler(identityService, ...)
    governanceHandler := governancehttp.NewHandler(catalogService, ...)
    
    // 5. 组装 HTTP 路由器
    router := httptransport.NewRouter(api.NewServerWithDependencies(dependencies), ...)
    
    // 6. 启动后台任务（恢复循环、MQ 消费）
    backgroundTasks := []backgroundTask{
        periodic("transfer recovery", time.Minute, resourceHandler.RecoverTransfers),
        periodic("production recovery", 2*time.Second, productionRepository.RecoverProductions),
        {name: "messaging", run: messaging.RunMessaging(...)},
    }
    
    return &Application{handler: router, background: backgroundTasks}, nil
}
```

**要点**：
- 无 DI 容器，显式组装
- 统一管理 cleanup 和 background 任务
- 启动时验证所有依赖的 readiness

---

### 2. **execution** - 执行协调模块

**职责**：任务生命周期、执行快照、事件投影

**关键文件**：
- `internal/modules/execution/snapshot.go` - 执行快照结构
- `internal/modules/execution/postgres/mq_repository.go` - Outbox 发布和事件投影
- `internal/modules/execution/postgres/generation.go` - 任务创建和状态更新

**执行快照生成流程**：

```go
// 1. 冻结工作流版本
frozenWorkflow := workflowService.Frozen(ctx, workspaceID, templateName)

// 2. 冻结治理能力（模型路由、Prompt、策略、Skills、Tools）
frozenCapabilities := governanceRepo.FreezeCapabilitySnapshot(ctx, workspaceID, baseConfig)

// 3. 组装不可变快照
snapshot := ExecutionSnapshot{
    WorkspaceID: workspaceID,
    WorkflowDefinition: frozenWorkflow,
    ModelProfile: frozenCapabilities["model_profile"],
    Prompts: frozenCapabilities["prompts"],
    Policies: frozenCapabilities["policies"],
    Skills: frozenCapabilities["skills"],
    Tools: frozenCapabilities["tools"],
    MCPPolicy: frozenCapabilities["mcp_policy"],
    Knowledge: frozenCapabilities["knowledge"],
    InputIR: contentIR,
}

// 4. 加密敏感字段（API Keys）
encrypted := cipher.Encrypt(snapshot)

// 5. 上传到 S3
objectKey := s3.Put(encrypted)

// 6. 在同一事务中：保存任务 + Outbox 命令
tx.Begin()
tx.InsertJob(jobID, workspaceID, projectID, ...)
tx.InsertOutbox(commandID, {
    type: "execution_requested",
    snapshot_key: objectKey,
    snapshot_hash: sha256(encrypted),
})
tx.Commit()
```

**事件投影流程**：

```go
// Runtime 发送事件 → RocketMQ → Control 消费
func (r *MQRepository) ProjectEvent(ctx context.Context, event RuntimeEvent) error {
    // 1. 幂等检查：是否已处理过此事件
    if r.isProcessed(ctx, event.EventID) {
        return nil
    }
    
    // 2. 在事务中：更新投影 + 标记事件已处理
    tx.Begin()
    switch event.Type {
    case "job_started":
        tx.UpdateJobStatus(event.JobID, "running")
    case "job_completed":
        tx.UpdateJobStatus(event.JobID, "completed")
        tx.InsertContentVersion(event.Artifact)
    case "job_failed":
        tx.UpdateJobStatus(event.JobID, "failed", event.Error)
    }
    tx.MarkEventProcessed(event.EventID, event.SequenceNumber)
    tx.Commit()
    
    return nil
}
```

---

### 3. **governance** - 治理目录模块

**职责**：模型路由、Prompt 目录、创作策略、能力封装

**关键概念**：

#### 模型路由（Model Profile）
```yaml
text_generation:
  provider: anthropic
  model: claude-3-5-sonnet-20241022
  temperature: 0.7
  max_tokens: 4096

image_generation:
  provider: stability
  model: stable-diffusion-xl-1024-v1-0
  steps: 30

audio_generation:
  provider: elevenlabs
  model: eleven_multilingual_v2
  voice_id: "..."
```

#### Prompt 目录（Prompt Catalog）
- 版本化的 Prompt 模板
- 支持变量替换和条件逻辑
- 按用途分类（outline、chapter、character、...）
- 工作流引用时冻结版本

#### 创作策略（Creation Policy）
- 长度约束（字数、时长）
- 风格指南（正式、轻松、专业）
- 内容限制（避免暴力、色情）
- 质量门槛（可读性评分）

#### 能力封装
- **Skills**：预定义能力模块（搜索、计算、调用 API）
- **Tools**：自定义工具定义（JSON Schema）
- **MCP Policy**：Model Context Protocol 策略
- **Knowledge**：知识包（向量数据库、文档库）

**冻结机制**：
```go
// 执行时冻结所有治理配置，确保可重现性
func FreezeCapabilitySnapshot(ctx context.Context, workspaceID string, base map[string]any) (map[string]any, error) {
    frozen := make(map[string]any)
    
    // 1. 解析工作流中引用的模型配置名称
    modelProfileName := base["model_profile"].(string)
    modelProfile := fetchModelProfile(ctx, workspaceID, modelProfileName)
    frozen["model_profile"] = modelProfile
    
    // 2. 解析 Prompt 引用，冻结版本
    promptRefs := base["prompts"].([]string)
    frozenPrompts := []Prompt{}
    for _, ref := range promptRefs {
        prompt := fetchLatestPrompt(ctx, workspaceID, ref)
        frozenPrompts = append(frozenPrompts, prompt)
    }
    frozen["prompts"] = frozenPrompts
    
    // 3. 冻结策略、Skills、Tools、MCP Policy
    // ...
    
    return frozen, nil
}
```

---

### 4. **workflow** - 工作流模块

**职责**：管理版本化的工作流定义

**工作流生命周期**：
1. **Draft（草稿）**：开发中，可修改
2. **Published（已发布）**：冻结版本，生产可用
3. **Deprecated（已弃用）**：不再推荐，但仍可执行

**版本管理**：
```go
type WorkflowVersion struct {
    ID          string
    Name        string    // 例如：novel.write.v2
    Version     int       // 版本号
    ContentHash string    // YAML 定义的 SHA-256
    Definition  string    // YAML 原文
    Status      string    // draft | published | deprecated
    CreatedAt   time.Time
    PublishedAt *time.Time
}

// 发布新版本
func (s *Service) Publish(ctx context.Context, workspaceID, name string) (*WorkflowVersion, error) {
    draft := s.repo.GetDraft(ctx, workspaceID, name)
    if draft == nil {
        return nil, ErrNotFound
    }
    
    // 计算内容哈希
    hash := sha256.Sum256([]byte(draft.Definition))
    
    // 生成新版本号
    latestVersion := s.repo.GetLatestVersion(ctx, workspaceID, name)
    newVersion := latestVersion + 1
    
    // 保存已发布版本
    published := &WorkflowVersion{
        ID:          uuid.New().String(),
        Name:        name,
        Version:     newVersion,
        ContentHash: hex.EncodeToString(hash[:]),
        Definition:  draft.Definition,
        Status:      "published",
        PublishedAt: timeNow(),
    }
    
    s.repo.Save(ctx, workspaceID, published)
    return published, nil
}

// 执行时冻结版本
func (s *Service) Frozen(ctx context.Context, workspaceID, name string) (*WorkflowVersion, error) {
    // 始终使用最新已发布版本，确保可重现性
    return s.repo.GetLatestPublished(ctx, workspaceID, name)
}
```

---

### 5. **content** - 内容 IR 模块

**职责**：管理平台无关的内容中间表示（IR）

**Content IR 结构**：
```json
{
  "type": "novel",
  "metadata": {
    "title": "示例小说",
    "author": "AI",
    "genre": "科幻"
  },
  "structure": {
    "outline": {
      "theme": "...",
      "chapters": [
        {"序号": 1, "标题": "开端", "梗概": "..."},
        {"序号": 2, "标题": "发展", "梗概": "..."}
      ]
    }
  },
  "content": {
    "chapters": [
      {
        "chapter_number": 1,
        "title": "开端",
        "paragraphs": ["段落1", "段落2", ...]
      }
    ]
  },
  "artifacts": {
    "cover_image": {"key": "s3://...", "sha256": "..."},
    "audio_narration": {"key": "s3://...", "duration": 3600}
  }
}
```

**版本管理**：
- 每次修改生成新版本
- 版本链：`v1 → v2 → v3`
- 支持分支和合并
- 版本间 diff 计算

**时间线（Timeline）**：
```go
// 内容演化的时间线视图
type ContentTimeline struct {
    DocumentID string
    Versions   []ContentVersion
    Branches   []Branch
}

type ContentVersion struct {
    VersionID    string
    ParentID     *string
    CreatedAt    time.Time
    SourceJobID  *string  // 哪个任务生成的
    IRSnapshot   json.RawMessage
    Diff         *ContentDiff
}
```

---

### 6. **production** - 生产调度模块

**职责**：素材生成、自动成片

**生产管道**：
```
1. 用户提交生产请求（例如：为小说第 1 章生成配图）
2. Control 创建生产任务
3. 调度器根据规则拆分为多个子任务
4. 发送命令到 Runtime
5. Runtime 执行并返回产物
6. Control 收集产物，组装成片
```

**自动成片规则**：
```yaml
production_rules:
  - trigger: chapter_completed
    actions:
      - type: generate_cover_image
        input: chapter_summary
      - type: generate_audio_narration
        input: chapter_text
        voice: character_main
      - type: compose_video
        timeline:
          - layer: background
            source: cover_image
          - layer: audio
            source: audio_narration
          - layer: subtitle
            source: chapter_text
```

---

### 7. **identity** - 身份认证模块

**职责**：用户登录、会话管理、权限控制

**认证流程**：
```
1. POST /api/v2/identity/login
   {username, password, workspace_id}
2. 验证密码（Argon2id）
3. 创建会话（存 Redis，TTL = 24h）
4. 设置 Secure HttpOnly Cookie
5. 返回 CSRF Token
```

**授权策略**：
- **Workspace Owner**：所有权限
- **Workspace Member**：读 + 执行任务
- **Guest**：只读

**Row-Level Security（RLS）**：
```sql
-- 所有表都有 workspace_id 列
-- 每个连接设置当前工作区上下文
SET LOCAL app.workspace_id = 'ws_abc123';

-- RLS 策略自动过滤
CREATE POLICY workspace_isolation ON jobs
    USING (workspace_id = current_setting('app.workspace_id'));
```

---

## 数据流与消息传递

### 任务提交流程

```
[Browser] 
  ↓ POST /api/v2/jobs
[HTTP Handler]
  ↓ CreateJob(request)
[execution.Repository]
  ↓ 1. 生成执行快照
  ↓ 2. 上传 S3
  ↓ 3. BEGIN TRANSACTION
  ↓    - INSERT INTO jobs
  ↓    - INSERT INTO outbox_commands
  ↓ 4. COMMIT
[MQ Publisher]
  ↓ 读取 outbox_commands
  ↓ 发送到 RocketMQ
[Runtime]
  ↓ 消费命令
  ↓ 下载快照
  ↓ 执行任务
  ↓ 上传产物
  ↓ 发布事件到 RocketMQ
[MQ Consumer]
  ↓ 消费事件
[execution.Projector]
  ↓ BEGIN TRANSACTION
  ↓    - UPDATE jobs SET status = 'completed'
  ↓    - INSERT INTO document_versions
  ↓    - INSERT INTO inbox_events
  ↓ COMMIT
[SSE Publisher]
  ↓ 推送给等待的客户端
[Browser]
  ↓ 收到任务完成通知
```

### Outbox Pattern 实现

**Command Outbox（Go → Runtime）**：
```sql
CREATE TABLE outbox_commands (
    id UUID PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    type TEXT NOT NULL,  -- execution_requested, cancel_requested
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    sequence_number BIGSERIAL
);

-- 发布循环（每 100ms）
SELECT * FROM outbox_commands 
WHERE published_at IS NULL 
ORDER BY sequence_number 
LIMIT 100;

-- 发布后标记
UPDATE outbox_commands 
SET published_at = NOW() 
WHERE id = ANY($1);
```

**Event Inbox（Runtime → Go）**：
```sql
CREATE TABLE inbox_events (
    event_id UUID PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    type TEXT NOT NULL,  -- job_started, job_completed, job_failed
    payload JSONB NOT NULL,
    sequence_number BIGINT NOT NULL UNIQUE,
    received_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

-- 投影循环（每次处理一批）
SELECT * FROM inbox_events 
WHERE processed_at IS NULL 
ORDER BY sequence_number 
LIMIT 100;

-- 处理后标记
UPDATE inbox_events 
SET processed_at = NOW() 
WHERE event_id = $1;
```

---

## 安全与治理

### 1. **数据加密**

**执行快照加密**：
```go
// AES-256-GCM 加密敏感字段
type ExecutionCipher struct {
    key [32]byte
}

func (c *ExecutionCipher) Encrypt(snapshot ExecutionSnapshot) ([]byte, error) {
    // 加密 API Keys、模型凭据
    encryptedKeys := c.encryptField(snapshot.ModelProfile.APIKeys)
    snapshot.ModelProfile.APIKeys = encryptedKeys
    
    // 序列化
    return json.Marshal(snapshot)
}

func (c *ExecutionCipher) Decrypt(encrypted []byte) (ExecutionSnapshot, error) {
    var snapshot ExecutionSnapshot
    json.Unmarshal(encrypted, &snapshot)
    
    // 解密
    decryptedKeys := c.decryptField(snapshot.ModelProfile.APIKeys)
    snapshot.ModelProfile.APIKeys = decryptedKeys
    
    return snapshot, nil
}
```

**模型配置加密**：
```go
// 存储时加密 API Keys
encryptedConfig := encrypt(modelConfig, workspaceEncryptionKey)
db.SaveModelProfile(workspaceID, profileName, encryptedConfig)

// 冻结时解密
decryptedConfig := decrypt(encryptedConfig, workspaceEncryptionKey)
snapshot.ModelProfile = decryptedConfig
```

### 2. **完整性校验**

**SHA-256 校验**：
```go
// 上传时计算
hash := sha256.Sum256(snapshotBytes)
s3.Put(objectKey, snapshotBytes, hash)

// 下载时验证
downloaded := s3.Get(objectKey)
computedHash := sha256.Sum256(downloaded)
if computedHash != expectedHash {
    return ErrCorruptedSnapshot
}
```

### 3. **Row-Level Security**

**PostgreSQL RLS 实现**：
```sql
-- 1. 启用 RLS
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;

-- 2. 创建策略
CREATE POLICY workspace_isolation ON jobs
    USING (workspace_id = current_setting('app.workspace_id', true));

-- 3. 连接时设置上下文
BEGIN;
SET LOCAL app.workspace_id = 'ws_abc123';
SET LOCAL app.user_id = 'user_xyz';
SELECT * FROM jobs;  -- 自动过滤 workspace_id
COMMIT;
```

**Go 中设置 RLS 上下文**：
```go
func (pool *Pool) WithWorkspaceContext(ctx context.Context, workspaceID, userID string) (pgx.Tx, error) {
    tx, err := pool.Begin(ctx)
    if err != nil {
        return nil, err
    }
    
    // 设置 RLS 上下文
    _, err = tx.Exec(ctx, `
        SET LOCAL app.workspace_id = $1;
        SET LOCAL app.user_id = $2;
    `, workspaceID, userID)
    
    if err != nil {
        tx.Rollback(ctx)
        return nil, err
    }
    
    return tx, nil
}
```

### 4. **CSRF 防护**

```go
// 登录时生成 CSRF Token
csrfToken := generateRandomToken()
session.CSRFToken = csrfToken

// 写操作验证
func ValidateCSRF(r *http.Request, session Session) error {
    headerToken := r.Header.Get("X-CSRF-Token")
    if headerToken != session.CSRFToken {
        return ErrCSRFMismatch
    }
    return nil
}
```

---

## 开发与测试

### 本地开发

```bash
# 1. 启动依赖（PostgreSQL, RocketMQ, S3）
cd infra/docker && docker compose up -d

# 2. 运行数据库迁移
cd apps/control
go run ../../tools/migrate up

# 3. 配置环境变量
export CONTROL_DATABASE_URL="postgresql://..."
export CONTROL_PLANE_CONFIG="config.yaml"

# 4. 启动 Control Plane
go run ./cmd/api

# 5. 健康检查
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

### 单元测试

```bash
# 运行所有测试
go test ./...

# 带竞态检测
go test -race ./...

# 指定模块
go test ./internal/modules/governance/...

# 带覆盖率
go test -coverprofile=coverage.out ./...
go tool cover -html=coverage.out
```

### 集成测试

```bash
# 需要真实数据库
export MQ_TEST_CONTROL_DATABASE_URL="postgresql://test:test@localhost/control_test"

# 运行集成测试
go test -race ./internal/modules/execution/postgres -run TestMQRepository

# S3 集成测试
export ARTIFACT_TEST_S3_ENDPOINT="http://localhost:9000"
export ARTIFACT_TEST_S3_ACCESS_KEY="minioadmin"
export ARTIFACT_TEST_S3_SECRET_KEY="minioadmin"
go test ./internal/platform/objectstorage -run TestS3RustFSIntegration
```

### 架构验证

```bash
# 检查模块依赖边界
python scripts/check_architecture.py
```

---

## 配置示例

```yaml
server:
  environment: development
  name: control-plane-dev
  http_addr: ":8000"

database:
  url: "postgresql://control:secret@localhost:5432/control"
  max_connections: 20
  min_connections: 2
  idle_timeout_seconds: 300

redis:
  addr: "localhost:6379"
  db: 0
  key_prefix: "ai-platform:"
  max_connections: 10

mq:
  enabled: true
  type: rocketmq
  endpoint: "localhost:8081"
  access_key: ""
  secret_key: ""
  namespace: ""
  tls_enabled: false
  topics:
    tasks: "execution-tasks"
    control: "control-commands"
    events: "runtime-events"
    memory: "memory-updates"
  groups:
    projection: "control-projection"

object_storage:
  default: s3
  providers:
    - type: s3
      endpoint: "http://localhost:9000"
      access_key: "minioadmin"
      secret_key: "minioadmin"
      region: "us-east-1"
      bucket: "agent-artifacts"
      use_path_style: true

security:
  internal_token: "dev-internal-token-change-in-prod"
  allow_memory: false
  auth_enabled: true
  cookie_secure: false  # 生产环境必须 true
  session_hours: 24
  allowed_origins:
    - "http://localhost:3000"
    - "http://localhost:5173"
  model_encryption_key: "base64:..."  # 32 字节密钥
  execution_encryption_key: "base64:..."  # 32 字节密钥
```

---

## 关键指标

### 代码规模
- **Go 文件数**：150 个
- **代码行数**：约 25,000 行
- **模块数**：11 个业务模块
- **HTTP 端点**：约 50 个
- **测试覆盖率**：> 70%

### 性能指标
- **启动时间**：< 3 秒
- **健康检查响应**：< 50ms
- **任务提交延迟**：< 200ms
- **事件投影延迟**：< 500ms
- **数据库连接池**：20 max, 2 min

---

## 相关文档

- [Go Control Plane 架构](./architecture/go-control-plane.md)
- [Python Runtime 布局](./architecture/python-runtime-layout.md)
- [消息架构](./architecture/messaging.md)
- [Prompt 目录](./architecture/prompt-catalog.md)
- [自动生产](./architecture/automatic-production.md)
- [图像语音模型](./architecture/image-speech-models.md)

---

**最后更新**：2026-10-14  
**维护者**：AI Content Platform Team
