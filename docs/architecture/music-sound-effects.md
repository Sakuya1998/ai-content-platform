# 音乐与音效生成

## 模型登记

音乐与音效使用独立工作流、Profile 模型列表和路由键，不填入聊天或 TTS 路由。首个协议为 ElevenLabs：音乐使用 `POST /v1/music`，音效使用 `POST /v1/sound-generation`；请求通过 `xi-api-key` 认证并同步返回 MP3。Go 加密保存密钥，任务快照冻结 Profile 版本和能力，Runtime 不自动重试结果未知的 POST。

创建单独的供应商 Profile：

```json
{
  "name": "ElevenLabs Audio",
  "provider": "elevenlabs",
  "base_url": "https://api.elevenlabs.io/v1",
  "api_key": "change-me",
  "music_models": [{
    "model_name": "music_v2_5",
    "capabilities": {
      "output_formats": ["mp3_44100_128"],
      "min_duration_ms": 3000,
      "max_duration_ms": 600000,
      "instrumental": true
    }
  }],
  "sound_effect_models": [{
    "model_name": "eleven_text_to_sound_v2",
    "capabilities": {
      "output_formats": ["mp3_44100_128"],
      "min_duration_seconds": 0.5,
      "max_duration_seconds": 30,
      "prompt_influence": true,
      "loop": true
    }
  }]
}
```

在现有完整模型路由集合中添加：

```json
{
  "music_generation": {"profile_id": "profile-uuid", "model_name": "music_v2_5"},
  "sound_effect_generation": {"profile_id": "profile-uuid", "model_name": "eleven_text_to_sound_v2"}
}
```

工作区可将两种能力分别路由到不同 Profile。音乐格式限定为 `mp3_44100_128`、`mp3_48000_192`；音效按模型声明选择 MP3 格式。音效长度为 0.5 到 30 秒，音乐长度为 3 到 600 秒。

## 生成与时间线

两种能力都使用 `document_type: "audio"`，由参数键选择工作流。`media.music@1` 调用数据库提示词 `media.music.plan`，输出纯音乐描述；`media.sound_effect@1` 调用 `media.sound_effect.plan`，输出单个非语言音效描述。结构化参数由 Go 预检，生成文本经过媒体评审后才提交供应商。

手动生成时，音乐参数放在 `arguments.music`，支持 `output_format`、`music_length_ms`、`force_instrumental`；音效参数放在 `arguments.effect`，支持 `output_format`、`duration_seconds`、`prompt_influence`、`loop`。二进制资产进入共享 `agent-artifacts`，Content IR 音频块保存 `audio_role`、模型身份、参数和资产校验值。

自动生产请求可设置 `generate_music` 和 `generate_sound_effects`。开启音乐时为成片生成一条背景轨；开启音效时，为计划长度至少 500 毫秒的每个镜头生成一条音效。组装阶段探测真实音频时长，音乐从时间线起点播放，音效从所属镜头起点播放且最长不超过镜头长度。时间线 `AudioTrack.role` 分别为 `music`、`effect`；旁白仍使用 `narration`。默认音乐增益为 0.25，音效为 0.85。

自动生产中音乐默认按计划时长生成，至少 3 秒；成片时间线较短时裁到最终时长。音乐和音效不替换或改写对白，也不会启用聊天模型 fallback。同步调用超时、响应损坏或归档结果不完整时进入人工恢复，避免重复扣费。

## 迁移与验收

统一迁移新增 Control016 和 Runtime012，并新增两条提示词身份及两个工作流包。外部数据库运行：

```bash
uv run --locked python -m scripts.platform_external_prepare --migrate
uv run --locked pytest tests/infrastructure/test_audio_generation_providers.py tests/domain/test_assembly.py
```

协议依据：[ElevenLabs Music](https://elevenlabs.io/docs/api-reference/music/compose) 和 [ElevenLabs Sound Effects](https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert)。
