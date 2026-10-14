# 多模态适配验收记录

日期：2026-10-10。

## 验证结果

- 隔离 PostgreSQL/pgvector 下 Python 全套：366 passed、4 个环境依赖用例跳过。
- Go 使用同一临时 Control 数据库：`go test -race ./... -count=1`、`go vet ./...` 通过。
- Ruff、两个 Compose 配置、JSON 契约及本轮文档链接校验通过。
- 三个跨服务用例最终一起执行：3 passed（18.50 秒）。视频、通用文本/记忆和完整小说流程使用实际外部 RocketMQ Proxy、RustFS，以及临时 Control/Runtime 数据库；外部 Redis 单独验证 ACL 认证、选库和 PING。模型响应由本地 HTTP 服务模拟，没有调用付费 AI。
- Go 和 Runtime 两个本地镜像已重建。wheel 文件集合与当前 109 个 Python 源文件一致，不含旧模块或提示词模板；Runtime 镜像确认媒体模块、统一迁移与模板资源移除。

## 行为覆盖

逐模型能力、历史 Profile/路由冻结、剧本/短剧结构、转换血缘、未知提交禁止重放、重启恢复原任务、持久化审核、人工核对失败后再次恢复、项目/工作区隔离、失效租约和 fencing 均有回归用例。

视频完整用例验证：Go 冻结任务 → MQ → Runtime 规划/评审 → 模拟方舟异步任务 → 实际 RustFS 视频和 IR 归档 → MQ 事件 → Go 内容版本投影。提交次数为一次；统一用量包含两次文本规划/评审和一次视频任务，视频 usage 与对象校验均保留。

取消结果保留远端 pending/unknown/terminal 状态，Go 投影保留审核与取消详情。取消确认和重复轮询的用量记录保持幂等；尚未提交的本地取消不记录供应商调用。

## 初始化与清理

实际外部数据库已应用 Control 012、Runtime 009，并新增 2 条视频提示词、1 个视频包和 1 套视频策略。当前合计 18 条提示词、5 个包/工作流绑定、3 套策略。临时数据库重复统一迁移全部跳过，重复种子创建/更新计数全部为零。

本轮自建 `agent-media-control`、`agent-media-api`、`agent-media-db` 容器和临时数据库数据卷已移除。外部基础设施与诊断对象未删除；没有部署持续运行的 AI Worker。真实 Seedance 画面质量、多镜头剪辑、配音与字幕仍需后续验收或实现，配置步骤见 [多模态模型与内容适配器](../architecture/multimodal-models.md)。
