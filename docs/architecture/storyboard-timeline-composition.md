# 分镜、时间线与媒体合成

## 工作流与边界

平台增加三个工作流：`media.storyboard@1` 创作分镜，`media.timeline@1` 规划时间线，`media.compose@1` 使用已有素材输出成片。Go 管理版本、输入和派生关系，Python 执行规划、审核与 FFmpeg 合成。

分镜和时间线使用冻结的数据库提示词、创作策略及 analysis 路由。合成不调用图片、视频、语音或聊天供应商；其 analysis/embedding 与 memory.refresh 绑定仅供完成后的衍生记忆任务使用。

## 分镜与时间线 IR

Content IR 保持 `1.0`，结构分别存入 `metadata.storyboard` 和 `metadata.timeline`：

- `storyboard@1`：shots 中保存唯一 shot_id、description、dialogue、duration_ms、source_block_ids。来源标识从冻结文档取得。
- `timeline@1`：width、height、fps、duration_ms、assets、shots、audio_tracks、subtitles。所有位置、裁剪起点和时长均使用毫秒。
- asset 只保存 asset_id、不可变 document_version_id 和 block_position。客户端不传 URL、存储路径或 FFmpeg 命令。
- 画面轨必须按时间顺序连续覆盖总时长；图片停留、视频裁剪通过 shot 的 source_start_ms、duration_ms 和 fit（contain/cover）指定。
- 音轨用 start_ms/source_start_ms/duration_ms 定位，用 gain、fade_in_ms/fade_out_ms 混音。可使用 narration、music、effect 角色；本工作流引用已有音频资产，自动生成音乐和音效由独立工作流提供，见[音乐与音效](music-sound-effects.md)。
- 字幕用 start_ms/end_ms/text 定位并烧录。文本作为普通文本处理，不解释 ASS 覆盖指令。

默认限制：总时长最多 600000 ms，128 个镜头、128 条音轨、256 条字幕、256 个资产；尺寸为 64..1920 的偶数，像素总量不超过 2073600，帧率为 1..60 的整数。音轨和字幕允许重叠，不能超出时间线；视频/音频的实际可用时长还会通过 ffprobe 检查。

## 创建分镜

使用 `POST /api/v2/jobs`，携带已认证会话、CSRF Token 和 `Idempotency-Key`：

```json
{
  "workflow": "media.storyboard",
  "workflow_version": 1,
  "project_id": "project-uuid",
  "document_type": "storyboard",
  "title": "车站分镜",
  "objective": "将剧本规划为两秒、两个镜头的分镜",
  "source_version_id": "screenplay-version-uuid",
  "input_version_ids": ["image-version-uuid", "video-version-uuid"],
  "arguments": {"storyboard": {"target_duration_ms": 2000, "max_shots": 2}}
}
```

分镜输出包含来源块标识；缺少来源引用会进入人工审核。`source_version_id` 可省略，用于从创作目标开始规划。

## 创建时间线

使用 `workflow=media.timeline`、`workflow_version=1` 和 `document_type=timeline`，source 指向分镜版本，input_version_ids 列出可选的图片、视频和音频版本。未传 arguments.timeline 时由 AI 规划；也可提交完整时间线，校验后评审。合成选择 `workflow=media.compose`、`operation=compose`。

```json
{
  "schema_version": "timeline@1",
  "width": 1280, "height": 720, "fps": 25, "duration_ms": 2000,
  "assets": [
    {"asset_id": "picture", "document_version_id": "image-version-uuid", "block_position": 0},
    {"asset_id": "clip", "document_version_id": "video-version-uuid", "block_position": 0},
    {"asset_id": "voice", "document_version_id": "audio-version-uuid", "block_position": 0}
  ],
  "shots": [
    {"shot_id": "shot-1", "asset_id": "picture", "start_ms": 0, "duration_ms": 1000},
    {"shot_id": "shot-2", "asset_id": "clip", "start_ms": 1000, "duration_ms": 1000, "source_start_ms": 1000}
  ],
  "audio_tracks": [
    {"track_id": "narration", "asset_id": "voice", "start_ms": 0, "duration_ms": 2000, "gain": 1.0}
  ],
  "subtitles": [
    {"cue_id": "caption-1", "start_ms": 0, "end_ms": 2000, "text": "车站旁白字幕"}
  ]
}
```

此对象作为 `arguments.timeline` 的值。替换示例中的版本 ID 为真实 UUID，引用版本必须列入 input_version_ids。每个块必须包含已归档的图片/视频/音频 asset；音频不能充当画面，图片不能充当音轨。

## 合成成片

```json
{
  "project_id": "project-uuid",
  "document_type": "video",
  "operation": "compose",
  "title": "车站成片",
  "objective": "根据已审核时间线合成视频",
  "source_version_id": "timeline-version-uuid",
  "input_version_ids": ["image-version-uuid", "video-version-uuid", "audio-version-uuid"],
  "arguments": {"compose": {"preset": "standard"}}
}
```

节点固定为 validate/render/publish。preset 为 draft 或 standard，尺寸和帧率来自冻结时间线。输出 MP4 使用 H.264/AAC；原视频声音不会隐式加入，需显式添加对应音轨。音轨可裁剪、延迟、调整增益及淡入淡出；字幕包括 CJK 字体支持。帧和编码器量化存在有限时间误差，完成后会校验实际尺寸、帧率、画面/音轨时长。

## 持久化、取消和恢复

Go 原子校验所有输入属于当前项目，并冻结 input_version_ids。ExecutionBundle 返回有序输入版本；完成后同时记录时间线与每个输入资产的派生关系。不可变 IR 返回时保留数值表示，例如 `1.0`，避免跨语言往返改变校验和。

Runtime 的 composition_runs 保存请求、timeline、输入 hash、渲染设置、状态、产物和探测结果。渲染前检查 bucket、项目路径、大小、SHA256，再复制到受控临时目录；FFmpeg 使用固定参数和输入格式，不读取客户端指定路径、网络 URL 或播放列表。

渲染完成先写不可变 video.mp4 和 composition.json 凭证，再提交数据库与内容 IR。进程重启优先验证凭证，已归档成片直接复用；尚未归档时可重新执行本地渲染，不会重新调用素材生成供应商。

合成超时、取消会终止子进程组并清理临时文件。取消结果通过 local_render_terminal 区分“请求取消”与“已停止/已完成”；未确认进程终止时不会误报。存储/数据库暂时不可用时可恢复原任务，模型审核拒绝则沿用明确的 job.resume/action=approve 流程。

## 部署与验证

统一迁移新增 Control014、Runtime011；当前初始化为 22 条提示词身份、10 个工作流包与 8 套策略。Runtime 镜像包含 ffmpeg、ffprobe、fonts-noto-cjk，CI 同样安装，执行真实二进制测试。

```dotenv
RUNTIME_FFMPEG_BIN=ffmpeg
RUNTIME_FFPROBE_BIN=ffprobe
RUNTIME_COMPOSE_TIMEOUT_SECONDS=300
RUNTIME_COMPOSE_THREADS=2
RUNTIME_COMPOSE_INPUT_MAX_BYTES=268435456
```

单个素材及输出继续受 RUNTIME_ARTIFACT_MAX_BYTES 限制。运行环境应为每个合成 Worker 提供足够的 CPU、内存和临时磁盘；这些值属于部署配置，不能由请求指定命令。

```bash
uv run --locked python -m scripts.platform_external_prepare --migrate
uv run --locked pytest tests/domain/test_timeline.py tests/domain/test_storyboard.py
docker run --rm --network none novel-agent-runtime:local python scripts/platform_compose_smoke.py
```

`scripts/platform_task_once.py --workspace-id UUID --job-id ID` 只执行明确指定的已接收任务，供运维验收使用。完整跨服务测试见 `tests/integration/test_production_integration.py`：模拟 AI + 实际 FFmpeg/MQ/RustFS，检查成片与派生关系。

首版组装流程不包含自动逐词字幕、复杂图层或跨镜头转场动画；音乐与音效生成现由独立工作流提供，见[音乐与音效](music-sound-effects.md)。缺失素材自动生成和父子任务编排见 [自动生产任务](automatic-production.md)。

自动生产扩展已增加 media.assemble@1；当前统一初始化为 24 个提示词身份、13 个工作流包、9 套策略，见 [自动生产任务](automatic-production.md)。
