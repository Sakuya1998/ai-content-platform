# 图片与语音模型接入

## 支持范围

图片通过 `media.image@1` 执行生成与单张来源图编辑，语音通过 `media.speech@1` 合成旁白或对白。两者复用 Go 模型治理、任务冻结、数据库提示词/策略、Runtime 租约、统一用量和共享 RustFS `agent-artifacts`。

| 能力 | Profile provider | 协议 |
| --- | --- | --- |
| OpenAI/兼容图片 | `openai`、`openai_compatible` | `/images/generations`、multipart `/images/edits` |
| 火山方舟 Seedream | `volcengine` | `/api/v3/images/generations`，编辑使用源图 data URL |
| OpenAI/兼容语音 | `openai`、`openai_compatible` | `/audio/speech`，返回音频流 |
| 豆包语音 | `volcengine_tts` | `/api/v3/tts/unidirectional`，NDJSON/base64 音频流 |

协议实现与能力登记分开：一个供应商可登记多个模型，一个工作区可配置多个供应商 Profile。每个任务冻结具体模型、Profile 版本、参数范围及凭据；修改默认路由只影响新任务。不要把图片或语音模型填到聊天路由。

## 配置模型

使用已认证、携带 CSRF token 的 `POST /api/model-settings/profiles`。图片示例中的模型名称必须替换为实际模型或推理接入点；尺寸、格式与数量按该模型能力填写。

```json
{
  "name": "图片模型",
  "provider": "volcengine",
  "base_url": "https://ark.cn-beijing.volces.com/api/v3",
  "api_key": "change-me",
  "image_models": [{
    "model_name": "your-seedream-endpoint-id",
    "capabilities": {
      "generation": true, "editing": true,
      "sizes": ["1024x1024"], "formats": ["png"], "max_images": 1
    }
  }]
}
```

语音 Profile：

```json
{
  "name": "豆包配音",
  "provider": "volcengine_tts",
  "base_url": "https://openspeech.bytedance.com",
  "api_key": "change-me",
  "speech_models": [{
    "model_name": "your-authorized-resource-id",
    "capabilities": {
      "voices": ["your-speaker-id"], "formats": ["mp3"], "languages": ["zh"],
      "min_speed": 0.5, "max_speed": 2.0, "max_input_chars": 4096,
      "instructions": false
    }
  }]
}
```

豆包 `model_name` 对应已授权的 Resource ID，`voices` 对应声音 ID。新版凭据可直接填写 API Key；旧版应用凭据使用加密字段中的 JSON 字符串，例如 `api_key="{\"app_id\":\"your-app-id\",\"access_token\":\"change-me\"}"`。密钥始终由 Go 加密保存，读取 Profile 只返回掩码。两套服务的基础设施配置无需复制供应商密钥。

OpenAI/兼容服务使用相应的 provider 和 `/v1` base URL。OpenAI 的格式支持 `mp3/wav/opus/aac/flac/pcm`，实际支持范围以模型声明为准；豆包当前适配 `mp3/opus/pcm`、速率 `0.5..2`，不把流式 WAV 假定为可直接拼接的文件。PCM 为 24kHz、16 位小端原始音频，后续处理需按该格式解码。

通过 `PUT /api/model-settings/routes` 在已有 `creative/analysis/embedding` 路由外，添加：

```json
{
  "image_generation": {"profile_id": "image-profile-uuid", "model_name": "your-image-model"},
  "speech_synthesis": {"profile_id": "speech-profile-uuid", "model_name": "your-speech-model"}
}
```

此片段需与原有三条路由一起提交；也可保留 `video_generation`。所有能力列表均按具体模型填写，不会从同一供应商的其他模型继承。媒体生成不自动使用 fallback 或重试 POST。

## 生成与编辑

使用 `POST /api/v2/jobs`，携带已认证会话、CSRF Token 和 `Idempotency-Key`。

```json
{
  "workflow": "media.image",
  "workflow_version": 1,
  "project_id": "project-uuid",
  "document_type": "image",
  "title": "车站分镜",
  "objective": "生成暖色晨光下的车站分镜",
  "arguments": {"image": {"operation": "generate", "size": "1024x1024", "format": "png", "n": 1}}
}
```

编辑时改为 `operation=edit`，并提供 `source_version_id`，指向同一项目已有图片文档版本。Runtime 校验冻结源图的 bucket、项目路径、大小和 SHA256，然后把图交给所选协议。初版编辑支持一张源图，不提供 mask、多参考图或本地任意文件路径。

`n` 为平台允许的 1..4；OpenAI 按请求数量校验，Seedream 的序列模式将其作为生成上限，实际返回可少于该值。多个图片输出分别生成 IR 块，共享源版本与模型身份。

## 合成语音

```json
{
  "workflow": "media.speech",
  "workflow_version": 1,
  "project_id": "project-uuid",
  "document_type": "audio",
  "title": "车站旁白",
  "objective": "为车站分镜创作并合成简短旁白",
  "arguments": {"audio": {"voice": "your-speaker-id", "format": "mp3", "speed": 1.0, "language": "zh"}}
}
```

可通过 `source_version_id` 使用冻结的剧本或文案；默认由 `media.speech.plan` 生成可朗读文本。如需逐字合成已有稿件，设置 `arguments.audio.text`，跳过文本改写，仍评审原文后再合成。

`instructions` 仅在所选模型声明支持时使用。豆包会转换为上下文指令；OpenAI 使用其原生 instructions。OpenAI 音频接口没有独立 language 参数，语言主要由合成文本决定；豆包使用显式语言参数。未指定声音、格式或速率时，Runtime 从冻结能力中选择并持久化默认值。

`character_id`、`scene_id` 可关联冻结原稿中的人物声明或场次；缺失原稿、引用不存在或跨项目来源会拒绝执行。当前不生成逐词时间戳、自动字幕或时间线混音。

## 恢复、取消与素材

图片与语音是同步调用，超时并不能证明供应商没有完成生成。Runtime 在 POST 前持久化 `submitting`；请求结果不明、音频流未完成或归档中断时保留 `submission_unknown`，进入人工恢复点，普通 approve 也不会重复 POST。

产物写入不可变 assets 后，再写 `receipt.json`，内容包含请求 hash、冻结模型身份、素材引用和用量。进程在归档后、数据库提交前退出，恢复会验证原凭证及素材并继续发布，不再次生成。部分产物写入但未形成凭证时，仍需人工核对；若确认生成未发生，可取消旧任务后创建新任务。

审核拒绝存储为 `prepared`，明确 `job.resume` 的 `action=approve` 才能继续。确认输出后状态为 `outputs_ready`，生成内容 IR 并回写 Go。IR 保存图片或音频块、实际提示词/合成文本、资产、参数、用量和来源版本；二进制不进入 MQ。音频脚本长度与媒体字节总量受冻结能力及 `RUNTIME_ARTIFACT_MAX_BYTES` 限制。

取消同步调用只能取消本地任务，无法保证远端停止。取消详情保留未知结果或已归档状态，不报告未经证实的远端取消。用量按任务幂等登记，保留供应商真实 token 或 `text_words` 指标；未知用量不推算，未配置定价时费用为零。

## 初始化与验收

新增 Control013、Runtime010，统一迁移初始化 20 条提示词身份、7 个工作流包和 5 套创作策略。通用媒体评审提示词升级为新版本；旧任务继续使用原版本。

```bash
uv run --locked python -m scripts.platform_external_prepare --migrate
uv run --locked pytest tests/domain/test_images.py tests/domain/test_speech.py \
  tests/infrastructure/test_image_provider.py tests/infrastructure/test_speech_provider.py \
  tests/execution/test_generated_media.py
```

跨服务验收位于 `tests/integration/test_image_speech_integration.py`，环境变量为 `ASSET_TEST_CONTROL_URL` 和外部 Runtime 配置。模型 HTTP 模拟，MQ/RustFS 实际接入；真实视觉效果、配音质量仍需使用授权模型验收。音乐和音效生成见[独立接入说明](music-sound-effects.md)；语音识别与声音克隆尚未接入。

协议依据：[OpenAI Images](https://developers.openai.com/api/reference/resources/images/methods/generate)、[OpenAI Speech](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create/)、[方舟官方 SDK](https://github.com/volcengine/volcengine-python-sdk/blob/master/volcenginesdkarkruntime/resources/images/images.py)、[豆包 HTTP 单向流式 V3](https://www.volcengine.com/docs/6561/1598757)。
