# 分镜到成片的自动生产任务

## 流程与冻结

Go 管理持久化父任务，自动推进：已有分镜 → 镜头素材 → 时间线组装 → FFmpeg 合成 → 成片版本。Python 执行素材生成、真实媒体探测、确定性组装和合成。

父任务创建时冻结分镜、模型路由、提示词包和策略；后续修改默认配置不会改变运行中的子任务。镜头素材使用稳定幂等键，子任务、父子关系和 Command Outbox 原子提交。依赖必须完成且内容版本真正落库，才允许推进下一阶段。

## 创建请求

接口为 `POST /api/v2/projects/{project_id}/productions`，需要认证、写权限和 CSRF Token。

```json
{
  "project_id": "project-uuid",
  "storyboard_version_id": "storyboard-version-uuid",
  "title": "车站短片",
  "idempotency_key": "station-film-001",
  "visual_mode": "image",
  "width": 1280, "height": 720, "fps": 25,
  "subtitles": true, "generate_speech": true,
  "generate_music": true, "generate_sound_effects": true,
  "image_parameters": {"format": "png"},
  "audio_parameters": {"voice": "your-speaker-id", "format": "mp3"},
  "music_parameters": {"output_format": "mp3_44100_128", "force_instrumental": true},
  "sound_effect_parameters": {"output_format": "mp3_44100_128", "prompt_influence": 0.5},
  "bindings": [
    {"shot_id": "shot-1", "visual_version_id": "existing-image-version-uuid", "visual_block_position": 0}
  ]
}
```

替换示例 ID 为实际 UUID。分镜必须为 storyboard@1、Content IR 1.0，最多 32 个镜头、计划总时长最多 10 分钟。API 默认 image、1280×720、25fps，并启用字幕和对白配音；显式 false 可关闭。

bindings 可指定 visual_version_id/visual_block_position 和 audio_version_id/audio_block_position，位置是文档块索引，资产必须属于同项目。绑定素材直接复用；没有视觉素材时生成，有对白且启用 generate_speech 时逐字配音。启用 `generate_music` 后生成一条背景音乐；启用 `generate_sound_effects` 后按镜头生成音效。音乐和音效默认关闭。相同请求和幂等键重复提交返回原 run_id。

参数沿用所选供应商的受控能力。首版每镜头生成一份视觉素材，不通过父任务改写对白；video 模式选择足以覆盖计划镜头的声明时长。

## 查询、审核与取消

```text
GET  /api/v2/projects/{project_id}/productions
GET  /api/v2/projects/{project_id}/productions/{run_id}
POST /api/v2/projects/{project_id}/productions/{run_id}/advance
POST /api/v2/projects/{project_id}/productions/{run_id}/resume
POST /api/v2/projects/{project_id}/productions/{run_id}/cancel
```

查询包含父状态、每个子任务的 stage/slot/job_id/status、结果和版本，以及 timeline_version_id/final_version_id；路由中的 project_id 必须与生产任务所属项目一致。

状态为 queued、assets、assembling、composing、waiting_review、failed、cancelled、completed。Go 每两秒恢复调度；可显式 advance，并发推进不会重复生成。父任务只有在最终成片版本发布后才 completed。

子任务需审核时父任务暂停。批准指定子任务，向 resume 提交：

```json
{"child_job_id": "child-job-uuid", "action": "approve"}
```

不同审核轮次使用独立控制命令，同一轮重试保持幂等。未知供应商提交不会因 approve 重发，需按各媒体接入文档核对供应商记录；视频可通过已有子任务 job.resume/reconcile 接口附加确认的远端 ID，然后继续父任务。

子任务失败停止后续调度，并请求取消活跃的兄弟任务。父任务取消也会阻止新调度、发送子任务取消命令；不代表供应商已停止，需查看子任务结果中的 media_status、remote_cancel_confirmed、remote_work_terminal。已归档素材保留。

## 完整对白与素材恢复

`media.assemble@1` 按冻结镜头绑定探测真实素材时长。图片可延长到完整对白结束；视频不足以容纳计划画面或对白时进入人工审核，不静默截断。旁白和音效从对应镜头起点播放，背景音乐从时间线起点播放；音乐增益默认为 0.25、音效为 0.85。字幕覆盖旁白实际时长，是镜头级显示，不声称逐词对齐。总时长仍限 10 分钟。

组装与合成不调用生成供应商。存储或数据库故障保留恢复入口，复用已归档素材；合成进程恢复按已有凭证继续。字幕长度和控制字符不符合限制时需要调整原稿。

部署与 FFmpeg 配置见 [媒体合成](storyboard-timeline-composition.md)，模型参数见 [图片与语音](image-speech-models.md)、[视频模型](multimodal-models.md)和[音乐与音效](music-sound-effects.md)。

## 初始化与限制

Control015 新增 production_runs/production_children 和工作区 RLS；复用现有 Runtime 表。统一迁移当前初始化为 24 个提示词身份、13 个默认包、9 套策略：

```bash
uv run --locked python -m scripts.platform_external_prepare --migrate
```

本阶段从已有分镜开始。剧本到分镜使用现有工作流；复杂图层、转场和自动逐词字幕仍待后续。真实视觉连贯性、配音质量、音乐表现和音效适配需使用授权模型单独验收。
