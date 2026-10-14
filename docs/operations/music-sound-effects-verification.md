# 音乐与音效验收

日期：2026-10-12。

## 本地检查

- Python 全量测试：647 passed、143 skipped。音乐/音效协议、时间线组装和提示词契约测试通过。
- Go `go test -race ./...`、`go vet ./...`、Ruff 和部署检查通过。两个需要 `MQ_TEST_CONTROL_DATABASE_URL` 的 Go 数据库用例在本地跳过。
- 外部自动生产集成：1 passed。模拟服务收到图片 1 次、TTS 1 次、音乐 1 次、音效 2 次；模型 HTTP 由本地模拟，RocketMQ Proxy、RustFS、PostgreSQL 和 FFmpeg 使用实际服务。
- 未调用真实 ElevenLabs 或其他收费模型。

## 外部验收

外部 Control 已应用 016，Runtime 已应用 012。提示词目录更新到 24 个身份、13 个包/工作流绑定，创作策略绑定更新 2 项。

`tests/integration/test_automatic_production_integration.py` 使用 HTTP 模拟供应商响应，真实运行 MQ、RustFS 和 FFmpeg。确认同一父任务冻结模型路由，最终 timeline 包含 `music`、`effect`、`narration` 三种轨道角色；单个音乐请求和每镜头音效请求均只提交一次，旁白时长完整，音乐裁到成片时长。

外部迁移首次通过 Compose 执行时发现 Runtime 镜像只将 `/app/src` 配入 `PYTHONPATH`，策略种子导入失败。现在容器路径为 `/app:/app/src`，Compose 迁移和种子在外部数据库成功幂等运行。联调结束后已移除本轮 Control、Runtime、MQ Worker 容器及 Compose 网络；外部数据库、MQ、RustFS 和其数据均保留。
