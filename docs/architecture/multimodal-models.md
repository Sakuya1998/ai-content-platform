# 多模态模型与内容适配器

## 管理与执行边界

Go 使用同一模型目录管理供应商、加密凭据、不可变 Profile 版本和路由。聊天和向量继续使用 `creative`、`analysis`、`embedding`；视频新增可选 `video_generation`。旧的三路由配置仍可使用。

Python 的聊天接口负责文本推理与视频提示词规划。视频提交、状态查询和取消使用独立的 `VideoProvider`，不会把视频模型当作聊天模型调用。目前实际实现火山方舟协议；同一个 Profile 可登记多个 Seedance 模型或推理接入点，不同 Profile 可保存不同账号和供应商。其他视频供应商需要实现并注册协议适配器，不能仅修改名称就宣称可用。

任务冻结路由集合和具体 Profile 版本，视频路由同时冻结所选模型的能力。修改默认模型不会改变已经提交的任务。管理面保留候选 fallback 的能力，但当前视频执行不自动切换供应商，以避免不确定提交造成重复计费。

## 配置视频模型

通过已认证且携带 CSRF token 的 `POST /api/model-settings/profiles` 创建 Profile。下面的能力仅为配置示例，必须按实际模型文档填写；模型名称使用自己的有效模型或推理接入点 ID。

```json
{
  "name": "方舟视频",
  "provider": "volcengine",
  "base_url": "https://ark.cn-beijing.volces.com/api/v3",
  "api_key": "change-me",
  "video_models": [{
    "model_name": "your-video-endpoint-id",
    "capabilities": {
      "text_to_video": true,
      "image_to_video": true,
      "durations": [5],
      "ratios": ["16:9"],
      "resolutions": ["720p"],
      "generate_audio": false,
      "cancellation": false
    }
  }]
}
```

`PUT /api/model-settings/routes` 同时提供现有三个路由与视频路由：

```json
{
  "creative": {"profile_id": "text-profile-id", "model_name": "chat-model"},
  "analysis": {"profile_id": "text-profile-id", "model_name": "chat-model"},
  "embedding": {"profile_id": "embedding-profile-id", "model_name": "embedding-model"},
  "video_generation": {"profile_id": "video-profile-id", "model_name": "your-video-endpoint-id"}
}
```

供应商凭据由 Go 加密入库；提交任务时，模型路由版本及非敏感执行参数冻结到 RustFS 执行包，Runtime 通过 RocketMQ 收到对象引用后加载。凭据不进入 MQ 或执行包，由 Runtime 可访问的密钥配置解析。无需在两个服务配置中复制 Seedance API key。Profile 连通性测试只提供 chat/embedding，视频验收请使用明确的生成任务，避免连通性按钮隐式创建收费视频。

## 内容结构与转换

Content IR 保持 `1.0`。块的 `metadata.block_id` 保存稳定标识；未指定时由文档 ID 和块位置生成。编辑、插入或重排时应显式保留原块 ID。

剧本与短剧复用文本图和共享 Canon、分层记忆及策略。场次使用 `metadata.scene` 的 `number/location/time_of_day/duration_seconds/episode_number`；集使用 `metadata.episode.number/duration_seconds`。人物声明和对白使用 `character_id`，可用 `scene_id` 引用场次。适配器检查重复标识、场次与集数、时长和人物引用，兼容已有未结构化场景文本。

文本转换请求指定 `source_version_id`，输出块通过 `metadata.source_block_ids` 和 `source_version_id` 指向冻结的原稿。来源缺失、越界、版本不符或未覆盖的源块会进入人工审核；这些检查只能验证结构和血缘，事实忠实度仍需要模型与人工评审。文案和广告继续使用通用文本能力，无需单独复制一套 Agent。

## 视频任务与恢复

通过已认证会话调用 `POST /api/v2/jobs`，写请求携带 CSRF Token 和 `Idempotency-Key`：

```json
{
  "workflow": "media.video",
  "workflow_version": 1,
  "project_id": "project-uuid",
  "title": "车站片段",
  "document_type": "video",
  "objective": "将原稿转为一个人物走进车站的镜头",
  "source_version_id": "optional-source-version-uuid",
  "arguments": {"video": {"duration": 5, "ratio": "16:9", "resolution": "720p"}}
}
```

无原稿时省略 `source_version_id`。`image_url` 可提供首帧；其余受控参数为 `generate_audio`、`camera_fixed`、`watermark`、`seed`。能力校验与供应商适配可用性检查发生在模型调用之前。

`media.video@1` 先通过数据库绑定的 `media.plan` 和 `media.review` 规划并评审提示词，再提交视频任务。拒绝的评审持久化后进入 `waiting_review`；明确 `job.resume` 的 `action=approve` 才允许提交，进程重启不能跳过评审。

提交前提交数据库状态 `submitting`，取得远端 ID 后只查询原任务。超时、服务端错误、响应缺少 ID 或提交期间进程退出会保留 `submission_unknown`，禁止自动重复提交。运营人员在供应商侧确认对应任务后，可恢复任务并传入 `{"action":"reconcile","remote_task_id":"confirmed-provider-task-id"}`；Runtime 查询并核对远端 ID/模型后继续归档。普通 approve 不会重发未知提交。只有确认没有创建远端任务后，才应取消旧任务并创建新任务。

查询超过 `RUNTIME_MEDIA_POLL_TIMEOUT_SECONDS`（默认 3600）进入人工恢复点；恢复继续查询原 ID。轮询间隔 `RUNTIME_MEDIA_POLL_SECONDS` 默认 5，Worker 心跳持续续租。视频下载受 `RUNTIME_ARTIFACT_MAX_BYTES` 限制，随后写入共享 `agent-artifacts` 的 assets；Content IR 保存不可变对象引用、模型身份、参数、usage 和原稿版本，不暴露供应商临时下载 URL。视频用量以幂等记录进入统一 model_calls，尚未配置定价时不会推算费用。

本地取消与远端停止分别报告。取消结果中的 `media_status`、`remote_cancel_confirmed`、`remote_work_terminal` 表明供应商任务是否真正结束；模型不支持取消、DELETE 失败或任务已暂停时，远端可能继续运行，应在供应商侧核对。进程正常退出或失去租约不发送取消请求。

当前视频工作流产出一个模型原生片段；多镜头、配音、字幕和成片使用[自动生产任务](automatic-production.md)。提交前评审只检查提示词，尚未提供生成画面的自动理解评测。

## 迁移与验证

新增 `control/012_video_models.sql`、`runtime/009_media_tasks.sql`；统一迁移同时初始化 18 条提示词、5 个工作流包和 3 套策略。已经冻结的历史版本保留。

上述为视频阶段的初始化范围。图片与语音扩展后，当前统一初始化为 20 条提示词身份、7 个工作流包和 5 套策略，新增协议见 [图片与语音接入](image-speech-models.md)。

```bash
uv run --locked python -m scripts.platform_external_prepare --migrate
uv run --locked pytest tests/content tests/execution/test_media_workflow.py tests/domain/test_media.py
```

实际 MQ/RustFS 联调用例为 `tests/integration/test_media_integration.py`，通过 `MEDIA_TEST_CONTROL_URL` 和外部 Runtime 环境启动。它模拟方舟 HTTP，验证一次提交、视频归档和 Go IR 投影，不调用收费模型。
