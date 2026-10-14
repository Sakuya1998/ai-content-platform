# 通用创作平台 MQ 设计

## 通道与消费组

所有业务 Topic 为 RocketMQ 5.x NORMAL Topic，通过 gRPC Proxy 接入。
默认环境为 dev；生产配置显式使用 prod，环境间禁止复用消费组。

| Topic 后缀（前缀 agent-{env}-） | 生产者 | 消费组后缀 | 职责 |
| --- | --- | --- | --- |
| runtime-tasks | Control Outbox Relay | runtime-dispatch | 持久化 workflow.run/node.run |
| runtime-control | Control Outbox Relay | runtime-control | 持久化 job.cancel/job.resume |
| runtime-events | Runtime Outbox Relay | control-projection | 幂等、按序投影 |
| runtime-events | Runtime Outbox Relay | memory-scheduler | 从 content.version.committed 创建记忆任务 |
| memory-tasks | Runtime Outbox Relay | memory-worker | 持久化 memory.refresh |

Tags 等于消息类型，Keys 等于 command_id/event_id，消息正文是唯一业务依据。
同组消费者竞争消费；独立订阅者使用不同消费组。MessageQueue 分区数量由部署端配置。
后续独立媒体 Worker 使用 media-tasks，不按小说、广告等内容类别创建 Topic。

## 可靠性

创建任务与 Command Outbox 同事务。Runtime Inbox 和 Job 同事务后 ACK。
数据库 Worker 领取任务、续租并检查 fencing token；MQ 不等待创作完成。
完成状态与 Event Outbox 同事务，Relay 发送成功后标记 published_at。
发送成功但标记失败可能重投，消费端必须按业务 ID 去重并拒绝 ID 对应不同正文。
Go 先保存事件，再按 job_id/sequence 连续投影；缺口由后续消息补齐。

取消独立消费并持久化；提前到达的取消记录阻止迟到任务执行。
Worker 通过数据库心跳发现取消，取消已完成任务不改写终态。
业务重试使用数据库 attempt/next_retry_at；MQ 重投仅解决交付失败。
不可恢复的格式错误仍不 ACK，进入部署端配置的死信并告警；重放先修复原因。

## 记忆与处理器

记忆任务幂等键包含项目、内容版本和记忆类型。事件只携带内容引用。
记忆产物关联源版本，项目隔离继续适用；跨项目仅允许显式知识包共享。
下一章节的必要记忆依赖由工作流检查数据库中的记忆完成状态。
创作与记忆处理器由服务端配置可信 Python callable，不能由消息指定模块。
MQ 基础层不提供虚假的 AI 生成器；未配置处理器时仅可靠接收任务。

## 运维

API 与消息进程可独立部署。应用账号不需要 NameServer 管理权限。
Topic、消费组和死信策略由管理员初始化；客户端不能自动创建业务 Topic。
监控 Outbox 待发送年龄、重投/死信、任务租约、业务重试、事件 sequence 缺口。
日志仅记录 ID 和错误类型，不记录正文、稿件、密钥或预签名 URL。
