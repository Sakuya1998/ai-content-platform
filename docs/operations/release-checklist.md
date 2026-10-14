# 发布门槛清单

- Python Ruff、pytest、Go race/vet 与跨服务 JSON Schema 产物检查通过；数据库测试使用隔离 PG 实例及非超级用户。
- Go Control Plane 和 Python Runtime 镜像分别构建、扫描并固定 tag；默认 Runtime 依赖不含 Chroma 或 SQLite checkpoint。
- 平台 Compose/Kubernetes 配置通过检查，两服务 `/healthz`、`/readyz` 可用。
- 外部 PostgreSQL/pgvector、四个 RocketMQ NORMAL Topic、五个消费组、共享 `agent-artifacts` bucket 和 Redis ACL 已验证。
- 工作区模型 Profile 与三类路由已配置并测试；提示词包显式绑定，任务冻结不可变版本。
- 生产 CORS 仅允许显式来源，Cookie Secure/HttpOnly/SameSite 与 CSRF 校验通过。
- 跨工作区访问、viewer 权限、任务取消、SSE 重连、审核恢复和产物不可覆盖测试通过。
- `scripts/platform_backup.py` 加密备份已验证，Control 模型加密主密钥单独备份，恢复与镜像回滚演练通过。
- 日志记录 request_id/job_id，不输出模型密钥、Cookie、完整 Prompt、稿件或预签名 URL。
- 真实模型长篇质量及长任务恢复验收完成后再切换业务流量；旧数据导入仅使用 `platform_legacy_*` 工具。

## 观测

Go `/metrics` 需要内部服务 token，目前提供 `agent_control_database_up`。Prometheus 配置见 `deploy/prometheus.yml`，部署时挂载 token 文件。任务积压、状态和投影缺口使用鉴权后的 `/api/monitoring/summary`；没有实现的 HTTP、SSE 或模型指标不应配置告警。

发布后的稳定窗口检查就绪状态、MQ 重试/死信、Outbox 待发送年龄、租约失效和数据库备份。异常恢复后重新执行只读检查与代表性工作流。
