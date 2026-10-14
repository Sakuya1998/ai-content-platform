# 统一提示词目录

## 管理与执行

Control PostgreSQL 是平台 Runtime 提示词的唯一来源。根目录 seed JSON 仅用于幂等初始化和离线测试，不参与 Runtime 隐式回退。Go 管理平台默认与工作区克隆、不可变版本、发布、禁用、Prompt Pack 和工作流显式绑定；Python 渲染任务冻结的具体版本。

## 数据与接口

- prompts：提示词身份 key 与 scope（平台 workspace_id=NULL；工作区 UUID）。
- prompt_versions：不可变 messages、variables_schema、output_schema、renderer_version、status、content_hash。
- prompt_packs/prompt_pack_versions：集合身份和不可变版本；prompt_pack_entries 精确引用提示词版本并关联节点。
- workflow_prompt_bindings：workspace/workflow 精确绑定已发布 pack version；平台默认显式绑定提供新工作区起点。
- prompt_node_contracts：内置节点的变量/输出契约，发布校验必须兼容，禁止未知节点。

管理 API 使用 /api/v2/workspaces/{id}/prompts、prompt-packs、workflow-prompt-bindings。读取允许 workspace viewer，写入要求 owner。平台资源通过克隆修改；不允许工作区写入平台默认资源。绑定/回滚影响新任务，运行中任务保留版本 ID。Go 在提交任务时将绑定的 Prompt 版本内容冻结到执行包；Runtime 不通过 Control 内部 HTTP API 拉取版本。

## 跨语言契约

CapabilitySnapshot 增加可选 prompt_bindings（node→prompt_version_id）；旧字段省略保持历史 hash 算法。新任务必须冻结完整节点绑定。ExecutionContext.prompt_versions 是已验证的版本字典。节点名：12 个小说 agent_name、content.plan/content.write/content.review、media.plan/media.review、media.image.plan/media.speech.plan/media.music.plan/media.sound_effect.plan、memory.refresh。纯生成和改编共用内容包，但绑定记录分别存在；所有创作包包含 memory.refresh 供衍生记忆任务冻结使用。

版本 payload 固定 messages=[{role,content}]，renderer_version=literal-v1，变量 schema 支持有类型的对象。仅替换 {snake_case} 占位符，不执行表达式；JSON 示例里的花括号不作为表达式。小说模板保持原必填变量集合；消息里的缺失、未知变量或多个同节点引用在发布时拒绝。Prompt Pack 必须完整满足工作流契约，含变量、输出和渲染器；历史已发布版本禁止更新/删除。

## 验证与迁移

Control 009/010 新增目录、RLS、身份约束与不可变版本保护。独立 seed 从根目录种子载入 24 条提示词身份，生成 13 个显式默认工作流绑定（包含视频、图片、语音、音乐和音效）；种子文本变化创建新版本，不覆盖已发布数据。节点契约变化必须升级工作流，不能覆盖原契约。现有 governance prompt_pack 保留兼容查询，不参与新任务冻结；自定义模板需通过新目录创建版本并显式绑定。

初始化已集成到统一迁移脚本和 Compose migrate 服务。外部数据库使用：

```bash
uv run --locked python -m scripts.platform_external_prepare --migrate
# 仅重复初始化提示词，读取 .env.control.external 中 CONTROL_DATABASE_URL
uv run --locked python -m scripts.platform_prompt_seed
```

种子要求迁移表所有者或管理员权限，事务内恢复 FORCE RLS；应用请求不能修改平台默认资源。`PLATFORM_PROMPT_SEED_DATABASE_URL` 可指定专用初始化连接，不改变服务配置。

## 修改、发布与回滚

使用登录会话及 `X-CSRF-Token`；以下路径的共同前缀为 `/api/v2/workspaces/{workspace_id}`：

1. `GET /prompts` 和 `/prompt-packs` 查找平台默认版本及 `entries`。
2. `POST /prompts/{id}/clone`，例如 `{"key":"team.content.plan","name":"团队规划"}`，创建工作区草稿。
3. `PATCH /prompts/{id}` 修改 `messages`，保留节点占位符与变量/输出 schema；用 `expected_version` 防止并发覆盖。每次修改都会新增版本。
4. `POST /prompts/{id}/preview`，例如 `{"variables":{"objective":"创作广告","document_type":"advertisement","context":""}}`；只渲染，不调用模型。`POST /prompts/{id}/publish` 携带 `{}` 发布并取得新 `version_id`。
5. `POST /prompt-packs/{id}/clone` 创建工作区包；`PATCH /prompt-packs/{id}` 提交完整 `entries=[{node_key,prompt_version_id}]`，替换指定节点。通用包包含 plan/write/review 与 memory.refresh 四个节点。
6. `POST /prompt-packs/{id}/publish` 发布；`PUT /workflow-prompt-bindings/content.generate@1` 提交 `{"pack_version_id":"已发布版本UUID"}`，使后续任务使用该包。

回滚时重新绑定之前的包版本；`GET /workflow-prompt-bindings/{workflow}/history` 查看历史，`DELETE /workflow-prompt-bindings/{workflow}` 恢复平台默认。发布或禁用不会改写已冻结任务，历史版本仍可读取。缺少 `prompt_bindings` 的旧任务执行前明确失败，需要提交新的任务；不能在运行中静默替换快照。

## 执行与追踪

Go 构建执行包时及 Python 加载时均校验 payload hash；预览与执行保持 system/user/assistant 消息角色，技能追加不会破坏角色。Runtime 对变量和结构化输出校验冻结 schema，格式纠正重试沿用同一版本。禁止远程 schema 引用及模板表达式。

Runtime `model_calls` 与 `prompt_renderings` 保存提示词/包版本、渲染器、变量哈希、实际输入哈希、输出契约和 token 使用，不保存变量正文。小说历史查询只公开安全追踪字段。旧本地 CLI/API 已删除；平台执行不会因数据库缺失回退到文件。
