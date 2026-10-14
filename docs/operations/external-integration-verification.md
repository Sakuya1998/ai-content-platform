# 外部基础设施集成联调

日期：2026-10-09。环境：staging。

本文记录旧架构的历史联调结果。2026-10-12 重构后的可执行验证使用公开任务 API、MQ 快照对象和直接 S3；下列历史结果不代表新架构已经通过外部联调，新验证须在隔离环境重新运行。

## 2026-10-13 外部 staging 验收

本次按“外部数据可废弃”授权执行了真实 staging 联调。Control/Runtime PostgreSQL 原有业务表已清空，迁移账本保留并前向补齐到 Control 022、Runtime 016；Prompt、Prompt Pack、Creation Policy 和工作流目录重新 seed。旧小说状态表已按退役迁移删除，`pgcrypto` 和 `pgvector` 可用。

外部 Redis、RocketMQ 5.x gRPC Proxy、RustFS `agent-artifacts` 均通过连通性检查。Go 与 Python SDK 使用同一组 Topic，Go Control Plane、Python Runtime API 及五个消息进程已启动，`/readyz` 均正常，Control/Runtime Outbox 待发送数为零。`platform_mq_integration.py` 通过 Outbox/Inbox、执行快照、RustFS 产物、Go 事件投影、记忆调度、重复投递和取消检查；独立 RustFS 不变性校验通过。

本次使用占位模型路由和确定性诊断处理器，没有调用真实或收费 AI；真实 Worker 未启用。启用 Worker 前必须填写 Runtime LLM/Embedding 配置，替换 Control 内部运维 token，并重新执行真实模型验收。外部配置中的 Go↔Python HTTP 地址已移除，服务间业务链路仍只使用 MQ 和 RustFS 对象引用。为适配 staging 的 Secure Cookie，诊断请求通过一次性本机 HTTPS 转发器完成，未修改生产 Cookie 配置。

## 2026-10-12 本地隔离验证

2026-10-12 本地隔离重构验证：`uv run --locked pytest tests/integration -q` 在全部环境开关启用后合并运行，11 passed，83.47 秒。六条真实端到端链路覆盖文本生成/改编/审核与记忆、小说暂停恢复与通用投影、视频生成、图片/编辑和两组语音协议、分镜/时间线合成、七子任务自动生产；其余五项验证公开任务脚本与 HTTP 边界守卫。基础设施为本地 PostgreSQL/pgvector、RocketMQ Proxy、RustFS 和 Runtime FFmpeg 镜像，供应商为本地 HTTP 模拟。每个执行快照包含经过 hash 校验的冻结工作流版本与受控节点定义；Runtime 访问 Control HTTP 会直接使测试失败。

`platform_mq_integration.py` 的公开登录、重复任务、快照、产物、投影、记忆派生和取消诊断，以及 `platform_artifacts_check.py` 的直接 S3 校验通过。本轮仅使用本地诊断资源，未访问外部实例，未调用收费模型。联调修复了环境变量存储 provider 默认值缺失、不同文档同标题约束及移除该约束后的内容版本 upsert；事件缺口由投影事务回滚造成，修复后顺序处理与恢复通过。

最终执行包移除 Go 小说专属输入字段后，小说真实端到端用例再次通过，3.93 秒；Python 通过通用 input、brief、brief_version、frozen_resources 构造内部格式状态，对应 22 项格式适配与 PostgreSQL 图回归通过。

## 验证范围

使用实际外部 Control/Runtime PostgreSQL、RocketMQ 5.x gRPC Proxy、Redis 和 RustFS `agent-artifacts`。Go/Python 服务在临时容器运行，测试创建独立工作区与项目；AI HTTP 协议响应和向量均由本地测试服务提供，没有调用收费模型或部署业务 AI Worker。

## 已通过

- Go 与 Python SDK 连接、四个 NORMAL Topic 路由、五个消费组心跳。
- `scripts/platform_mq_integration.py`：Go Command Outbox → MQ → Runtime Inbox、重复投递幂等、数据库完成事务与 Event Outbox、Go 按序投影、记忆调度/提交、取消早于任务。
- `tests/integration/test_handler_integration.py`：数据库冻结提示词、冻结模型配置、内容生成、审核、RustFS 产物、IR 回写、记忆摘要/事实/1536 维向量、项目隔离、改编与人工批准。
- `tests/integration/test_legacy_integration.py`：真实注册/权限/模型治理、完整小说图、暂停/恢复、全文状态投影、章节版本、DOCX 导出和确定性 benchmark。
- 对象存储：Go 预签名 PUT → Python 校验 GET、重复上传 412；Runtime 上传 → Go 预签名 GET、重复登记幂等、事件投影和失效租约 409。
- Redis 使用已配置 ACL 用户验证 PING；两个服务 readyz 返回 200。

完整创作两项用例在最终持久配置下重新执行通过；Go race/vet 通过，Python 消息/产物相关 14 项通过，Ruff 与外部 Compose 校验通过。本轮更新两个本地镜像。

## 联调修复

Go AWS SDK 会将 `Accept-Encoding: identity` 纳入签名，经过外部 Cloudflare 入口后 HeadBucket 返回 403。移除 SDK 添加该头的 middleware，并关闭 HTTP transport 自动压缩后返回 200；上传、读取及预签名回归均通过，保留内容 SHA256 与条件写保护。回归测试位于 `go-control-plane/internal/platform/objectstorage/s3_test.go`。

Runtime Redis URL 未指定用户名，实际实例使用非默认 ACL 用户，导致 WRONGPASS/NOAUTH。已将 `.env.runtime.external` 的 URL 用户名同步为 Go 配置中的已验证用户名，端点、密码和 DB 保留。

Go 持久模型加密主密钥原先为空，现已生成 32 字节随机密钥保存到 `.env.control.external`，权限 0600；从该配置启动容器并读取冻结模型路由、解密测试凭据成功。密钥不能随意更换，需单独备份。

## 复验方式

应用使用 `docker-compose.external.yml`；模型未配置时不要开启 workers profile。MQ 脚本需要 `--username` 与环境变量 `PLATFORM_VERIFY_PASSWORD`，并指定 `.env.runtime.external`、Go URL、已有诊断工作区和项目 ID；用户须有相应写权限。对象烟测使用 `platform_artifacts_check.py --runtime-env-file .env.runtime.external --workspace-id WORKSPACE_UUID --project-id PROJECT_UUID`，直接校验 S3 不变性和内容 hash。

完整用例设置 `HANDLER_TEST_CONTROL_URL`、`LEGACY_TEST_CONTROL_URL` 及外部配置对应的 `RUNTIME_*` 变量后运行：

```bash
uv run --locked pytest tests/integration/test_handler_integration.py \
  tests/integration/test_legacy_integration.py -q
```

测试服务与模型 HTTP 服务必须相互可达；Cookie Secure 仅在本地 HTTP 验收容器临时关闭，生产配置保持原值。测试工作区和模拟产物是诊断数据，模型 Profile 指向临时测试服务，不应用于真实创作。初次临时密钥阶段的测试工作区也不作为持久模型配置使用。

真实 LLM/embedding 配置与长篇真实模型质量验收仍待补齐。

本轮本地临时容器和诊断脚本已清理；外部 staging 的诊断工作区和消息结果保留供核对，RustFS 烟测对象已删除。外部数据库清空和迁移属于本次明确授权的废弃数据操作。

2026-10-10：共享策略与视频扩展已完成迁移和三项创作联调，最新范围及测试数字见 [多模态验收](multimodal-verification.md)。

2026-10-10：图片/编辑和两组 TTS 协议已接入，四项跨服务用例通过，详见 [图片与语音验收](image-speech-verification.md)。

2026-10-10：分镜/时间线与实际 FFmpeg 合成已接入，五项跨服务验收通过，见 [成片合成验收](production-composition-verification.md)。

2026-10-10：自动生产父子任务已接入，六项跨服务验收通过，见 [自动生产验收](automatic-production-verification.md)。
