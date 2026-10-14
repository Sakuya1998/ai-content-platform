# Runtime 消息处理器与执行一致性

## 运行边界

Go 管理项目、内容、治理版本和任务请求；Python 消费任务并执行工作流。Go 与 Runtime 之间没有业务 HTTP API。命令通过 Control Outbox 发布到 RocketMQ，Runtime 将命令持久化到 Inbox 后确认；Runtime 结果通过自己的 Outbox 发布事件，Go 持久化事件后更新投影。

任务生成时，Go 将输入 Content IR、工作流版本、Prompt/策略/模型路由版本和能力定义冻结为执行包，写入共享 RustFS。MQ 命令包含执行包对象键、schema 版本、hash 和任务身份。Runtime 不得用项目 ID 或版本 ID 调用 Go 补取内容。

Runtime 通过 AWS SDK for Python 读取执行包，验证 hash、workspace/project、资源版本和能力快照后，只运行内置注册的节点。模型供应商调用仍由 Runtime 按冻结路由执行，凭据由 Runtime 可访问的密钥配置解析，不放入快照。

## 命令与事件流程

1. Go 在一个 Control 事务中写入任务、幂等键与 Outbox 命令。
2. Outbox Relay 发布 MQ 命令；发布失败时重试，不能丢弃未确认记录。
3. Runtime 消费者将 command_id 和 payload hash 持久化到 Inbox 后 ACK。相同 ID 和相同 hash 可安全重放；ID 相同但 hash 不同必须拒绝。
4. Worker 使用租约和 fencing token 执行；进度、审核等待、失败或完成状态都生成有序事件。
5. Runtime 将事件 Outbox 与状态、执行产物引用在同一个 Runtime 事务提交，再发布 MQ。
6. Go 按 workspace、job、连续 sequence 和事件 ID 幂等应用事件；结果 IR 先写 RustFS，再在一个 Control 事务中写入文档版本、内容块和任务投影。
7. 取消、恢复、记忆刷新、评测和 Runtime 查询使用同一消息模式。需即时向量计算的查询返回异步 operation_id，不提供同步 MQ RPC。

大对象不放入 MQ。Runtime 写入不可变输出对象并发送 object_key、size、SHA-256 和 artifact type；Go 用自己的 S3 客户端验证对象，不请求 Runtime 上传或下载。对象键限定 workspace/project 前缀，写入受租约和 fencing token 保护。

## 状态、审核与记忆

公共任务状态统一为 queued、running、waiting_review、cancel_requested、completed、failed、cancelled。Runtime 内部 accepted 映射为 queued；章节、分镜等节点进度作为工作流进度数据，不形成内容类型专属状态机。

审核草稿与意见作为通用任务产物；审核恢复使用 job.resume 命令，approve 复用已有草稿，revise 根据反馈继续执行。终态任务不接受过期 Worker 的内容提交。

记忆节点、Canon 事实、embedding、ContextPack 和 Knowledge Pack 由 Runtime 通用存储处理；默认按项目隔离，跨项目共享必须通过显式发布和绑定。小说适配器定义特有记忆提取规则，但不另建小说记忆数据库或 API。

## 查询与运行观测

任务状态、用量、追踪和已投影记忆元数据通过 Go 管理库查询投影提供。需要 Runtime 进行语义向量搜索等计算时，Go 发布带 correlation_id 的异步命令；Runtime 通过事件返回结果，Go 保存结果供客户端查询。Go 不代理 Runtime API，也不轮询 Runtime 健康接口。

Go 与 Runtime 的 /healthz、/readyz 分别由容器编排与监控系统探测。Runtime 消费延迟、Outbox 堆积和最后事件时间由 MQ 指标及服务自身指标观测。

