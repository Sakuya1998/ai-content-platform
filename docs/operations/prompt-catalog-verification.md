# 统一提示词入库验证

日期：2026-10-09。

## 外部实例

已使用分开的 `.env.control.external` / `.env.runtime.external` 执行统一迁移。Control 009/010 已应用，平台默认包含 16 条已发布提示词、4 个已发布包和4个工作流绑定；逐条读取校验内容哈希通过。重复 seed 的创建、版本新增与绑定更新计数均为零。外部 RustFS `agent-artifacts` bucket 可用。

```bash
uv run --locked python -m scripts.platform_external_prepare --migrate
uv run --locked python -m scripts.platform_prompt_seed
```

## 自动化与完整链路

- 临时 PostgreSQL 16/pgvector 使用非超级用户 control/runtime 角色，统一应用 17 个迁移。
- Python 回归：428 passed，3 个完整环境用例默认跳过；Ruff 通过。
- Go 完整 race 测试与 vet 通过；覆盖权限隔离、平台只读、版本/节点契约不可变、封存包、显式绑定、历史回滚和跨语言哈希。
- 完整通用处理器用例单独通过：真实 Go、PG、RocketMQ 5.3.1 Proxy SDK、RustFS 和确定性 HTTP 模型服务，覆盖生成、IR、记忆/向量、改编、人工审核及提示词版本追踪。
- 完整小说迁移用例单独通过：Go 注册/模型治理/任务冻结到小说 Runtime，覆盖暂停、恢复、最终稿件、导出及确定性 benchmark。
- 评审发现的小说消息角色丢失及技能注入字符串化均已修复，并补充回归测试；复核没有剩余确认问题。
- Compose 本地/外部配置校验通过，Runtime 镜像包含 16 个 seed 模板及全部迁移。
- 两个本地镜像已更新；Go 镜像健康检查、注册及数据库目录读取通过。Go 官方依赖代理下载出现 HTTP/2 stream error，本次构建使用已校验的本机模块缓存；仓库 Dockerfile 保持原下载流程。
- 本次临时服务、容器、数据卷与网络已清理；外部实例及已初始化数据保留。

完整环境测试入口是 `tests/integration/test_handler_integration.py` 和 `tests/integration/test_legacy_integration.py`。分别设置 `HANDLER_TEST_CONTROL_URL`、`LEGACY_TEST_CONTROL_URL` 及测试基础设施的 `RUNTIME_*` 变量；通用用例自动创建测试工作区和模型路由，小说用例还需要可访问的 Runtime API 执行 benchmark。所有 AI 响应均为模拟响应，没有调用收费模型。

## 使用边界

平台 Runtime 提示词只来自数据库冻结版本。源码文件供 seed 与离线测试使用。旧本地 CLI/API 已移除，旧治理 prompt_pack 保留查询兼容，自定义提示词需发布到新目录并明确绑定工作流。缺少新字段的历史任务应重新提交，不能悄悄替换运行中快照。

提示词入库验收时外部配置缺 MQ Proxy 端点及真实模型配置，没有启动外部消费 Worker 或真实 AI 调用。后续 MQ 配置检查见下节。发布、克隆、预览、绑定和回滚示例见 `docs/architecture/prompt-catalog.md`。

## 外部 MQ 配置补齐后的检查

用户补齐 Proxy 后，两个服务的端点、TLS、namespace、认证配置和四个 Topic 名称一致，当前环境为 staging。实际 gRPC QueryRoute 检查四个 Topic 均返回 `TOPIC_NOT_FOUND`，队列数为零；Go SDK 启动失败，外部 MQ 尚未完成初始化。

需在配置所指实例/namespace 创建四个 NORMAL Topic：`agent-staging-runtime-tasks`、`agent-staging-runtime-control`、`agent-staging-runtime-events`、`agent-staging-memory-tasks`。按 `docs/architecture/messaging.md` 创建五个独立消费组，并授予对应生产/消费权限。自管实例管理员可使用 `deploy/rocketmq/init.sh`，设置 `MQ_ENVIRONMENT=staging`、NameServer 与 cluster；托管实例使用管理控制台。

Python SDK 5.1.2 会记录启动路由错误而继续返回成功；Runtime 适配器现已显式检查生产/消费 Topic 是否存在可用队列，缺失时关闭客户端并拒绝就绪。相关回归 18 项通过，Ruff 通过。该修复需重新构建 Runtime 镜像。本轮只进行了 SDK 启动及只读路由查询，没有发送业务消息或启动消费循环/AI Worker。

后续用户已创建四个 Topic 和五个消费组，实际 SDK、心跳及外部完整链路联调均通过；Runtime 镜像已更新。当前结果与配置修复见 [外部联调记录](external-integration-verification.md)。
