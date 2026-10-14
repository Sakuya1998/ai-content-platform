"""Bounded, local-only FFmpeg composition of a validated timeline.

Inputs have already been scoped and checksum-verified by the asset resolver. This
module copies them to controlled filenames, checks actual streams, and publishes
only a probed MP4. No timeline value is interpreted as a filename or CLI option.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import signal
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agent_runtime.domain.timeline import Timeline


class CompositionError(RuntimeError):
    """Safe composition failure; never contains subprocess diagnostics or paths."""


@dataclass(frozen=True)
class ResolvedMediaAsset:
    asset_id: str
    path: Path
    kind: Literal["image", "video", "audio"]
    content_type: str


@dataclass(frozen=True)
class RenderResult:
    path: Path
    probe: dict


_FORMATS = "mov,matroska,wav,mp3,ogg,flac,aac,png_pipe,jpeg_pipe,webp_pipe,bmp_pipe,ppm_pipe"
_MIME_FORMATS = {
    "image/png": "png_pipe",
    "image/jpeg": "jpeg_pipe",
    "image/webp": "webp_pipe",
    "image/bmp": "bmp_pipe",
    "image/x-portable-pixmap": "ppm_pipe",
    "video/mp4": "mov",
    "video/quicktime": "mov",
    "video/webm": "matroska",
    "audio/mp4": "mov",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/aac": "aac",
    "audio/webm": "matroska",
    "audio/opus": "ogg",
    "audio/pcm": "s16le",
    "audio/l16": "s16le",
    "audio/x-pcm": "s16le",
}


async def _run_process(args: list[str], timeout_seconds: float, *, cwd: Path | None = None) -> bytes:
    """Run argv directly, discard private stderr, and reap the entire process group."""
    if timeout_seconds <= 0:
        raise CompositionError("Media process timed out")
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        raise CompositionError("Media executable is unavailable") from None

    async def collect():
        output = bytearray()
        while chunk := await process.stdout.read(65536):
            output.extend(chunk)
            if len(output) > 1024 * 1024:
                raise CompositionError("Media process output exceeded limit")
        await process.wait()
        if process.returncode:
            raise CompositionError("Media process failed")
        return bytes(output)

    try:
        return await asyncio.wait_for(collect(), timeout_seconds)
    except BaseException as exc:
        # Kill descendants even when the group leader has already exited.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await asyncio.shield(process.wait())
        if isinstance(exc, TimeoutError):
            raise CompositionError("Media process timed out") from None
        raise


async def _copy_media(source: Path, destination: Path) -> None:
    # Cancelling to_thread does not stop filesystem IO. Finish this bounded copy
    # before TemporaryDirectory removes its destination; otherwise the worker can
    # recreate or keep writing files after cancellation cleanup has returned.
    copy_task = asyncio.create_task(asyncio.to_thread(shutil.copyfile, source, destination))
    try:
        await asyncio.shield(copy_task)
    except asyncio.CancelledError:
        while not copy_task.done():
            try:
                await asyncio.shield(copy_task)
            except asyncio.CancelledError:
                continue
            except OSError:
                break
        with suppress(OSError):
            copy_task.result()
        raise


def _local_file(path: Path) -> Path:
    try:
        if isinstance(path, Path) and path.is_file():
            return path.resolve(strict=True)
    except OSError:
        pass
    raise CompositionError("Media asset is not a local file")


def _input_options(content_type: str) -> list[str]:
    try:
        demuxer = _MIME_FORMATS[content_type]
    except KeyError:
        raise CompositionError("Unsupported media asset type") from None
    options = ["-protocol_whitelist", "file,pipe", "-f", demuxer]
    if demuxer == "s16le":
        options += ["-sample_rate", "24000", "-ch_layout", "mono"]
    return options


async def probe_media(
    path: Path,
    *,
    ffprobe_bin: str = "ffprobe",
    timeout_seconds: float = 30,
    content_type: str | None = None,
) -> dict:
    """Return compact stream metadata; PCM is 24kHz 16-bit LE mono.

    Automatic probing allows only closed binary container/image demuxers; caller
    supplied MIME forces the demuxer and cannot enable playlists or network IO.
    """
    local_path = _local_file(path)
    options = (
        _input_options(content_type)
        if content_type
        else ["-protocol_whitelist", "file,pipe", "-format_whitelist", _FORMATS]
    )
    data = await _run_process(
        [ffprobe_bin, "-v", "error"]
        + options
        + [
            "-show_entries",
            "format=duration:stream=codec_type,width,height,duration,avg_frame_rate",
            "-of",
            "json",
            str(local_path),
        ],
        timeout_seconds,
    )
    try:
        raw = json.loads(data)
        streams = raw.get("streams", [])
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        durations = [float(s["duration"]) for s in streams if s.get("duration") not in (None, "N/A")]
        if raw.get("format", {}).get("duration") not in (None, "N/A"):
            durations.append(float(raw["format"]["duration"]))
        duration = max(durations, default=0.0)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError

        def stream_duration(stream):
            value = stream.get("duration")
            result = float(value) if value not in (None, "N/A") else duration
            if not math.isfinite(result) or result < 0:
                raise ValueError
            return result

        video = videos[0] if videos else {}
        num, den = str(video.get("avg_frame_rate", "0/1")).split("/")
        fps = float(num) / float(den) if float(den) else 0.0
        return {
            "duration": duration,
            "width": int(video.get("width", 0)),
            "height": int(video.get("height", 0)),
            "has_video": bool(videos),
            "has_audio": bool(audios),
            "fps": fps,
            "video_duration": stream_duration(videos[0]) if videos else 0.0,
            "audio_duration": stream_duration(audios[0]) if audios else 0.0,
        }
    except ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError:
        raise CompositionError("Media probe returned invalid metadata") from None


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.6f}"


def _ass_time(ms: int) -> str:
    centiseconds = ms // 10
    return (
        f"{centiseconds // 360000}:{centiseconds // 6000 % 60:02}:{centiseconds // 100 % 60:02}.{centiseconds % 100:02}"
    )


def _write_subtitles(path: Path, timeline: Timeline) -> None:
    size = max(12, round(timeline.height * 0.055))
    header = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {timeline.width}\nPlayResY: {timeline.height}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Noto Sans CJK SC,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"0,0,0,0,100,100,0,0,1,1,0,2,8,8,{max(6, size // 2)},1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for cue in timeline.subtitles:
        # Prevent ASS override tags and escape sequences from user text. Fullwidth
        # equivalents preserve readability of literal brace/backslash characters.
        text = cue.text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "".join(c for c in text if c == "\n" or ord(c) >= 32).replace("\n", r"\N")
        lines.append(f"Dialogue: 0,{_ass_time(cue.start_ms)},{_ass_time(cue.end_ms)},Default,,0,0,0,,{text}\n")
    path.write_text(header + "".join(lines), encoding="utf-8")


def _check_duration(metadata: dict, start_ms: int, duration_ms: int, fps: int, kind: str) -> None:
    if kind != "image":
        # One source frame / one AAC frame tolerates timestamp rounding, not a
        # truncated clip. Audio sample priming must not mask larger overruns.
        tolerance = 1 / (metadata["fps"] or fps) if kind == "video" else 1024 / 48000
        if (start_ms + duration_ms) / 1000 > metadata.get(f"{kind}_duration", metadata["duration"]) + tolerance + 1e-6:
            raise CompositionError("Media asset duration is insufficient")


async def compose_timeline(
    timeline: Timeline,
    assets: dict[str, ResolvedMediaAsset],
    output_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    timeout_seconds: float = 300,
    threads: int = 2,
    preset: str = "standard",
) -> RenderResult:
    """Compose explicit tracks into H.264/AAC MP4 and atomically publish after probing."""
    if preset not in {"draft", "standard"} or isinstance(threads, bool) or not 1 <= threads <= 16:
        raise CompositionError("Invalid render configuration")
    referenced = {shot.asset_id for shot in timeline.shots} | {track.asset_id for track in timeline.audio_tracks}
    if not referenced.issubset(assets):
        raise CompositionError("Timeline asset is unresolved")
    for asset_id in referenced:
        asset = assets[asset_id]
        if asset.asset_id != asset_id or asset.kind not in {"image", "video", "audio"}:
            raise CompositionError("Invalid resolved media asset")
        if asset.content_type not in _MIME_FORMATS:
            raise CompositionError("Unsupported media asset type")
        _local_file(asset.path)
    output_path = Path(output_path)
    if output_path.exists():
        raise CompositionError("Render output already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with asyncio.timeout(timeout_seconds):
            with tempfile.TemporaryDirectory(prefix="compose-", dir=output_path.parent) as directory:
                return await _compose(
                    timeline,
                    assets,
                    referenced,
                    output_path,
                    Path(directory),
                    ffmpeg_bin,
                    ffprobe_bin,
                    timeout_seconds,
                    threads,
                    preset,
                )
    except TimeoutError:
        raise CompositionError("Composition timed out") from None
    except OSError:
        raise CompositionError("Composition file operation failed") from None


async def _compose(timeline, assets, referenced, output_path, work, ffmpeg, ffprobe, timeout, threads, preset):
    base = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-filter_threads",
        str(threads),
        "-filter_complex_threads",
        str(threads),
    ]
    encoding = [
        "-c:v",
        "libx264",
        "-threads",
        str(threads),
        "-preset",
        "ultrafast" if preset == "draft" else "medium",
        "-crf",
        "28" if preset == "draft" else "23",
        "-pix_fmt",
        "yuv420p",
    ]
    controlled = {}
    metadata = {}
    for index, asset_id in enumerate(sorted(referenced)):
        asset = assets[asset_id]
        path = work / f"input-{index}.media"
        await _copy_media(asset.path, path)
        controlled[asset_id] = path
        metadata[asset_id] = await probe_media(
            path,
            ffprobe_bin=ffprobe,
            timeout_seconds=timeout,
            content_type=asset.content_type,
        )
        actual = metadata[asset_id]
        if asset.kind in {"image", "video"} and (
            not actual["has_video"] or not actual["width"] or not actual["height"]
        ):
            raise CompositionError("Media asset has no video stream")
        if asset.kind == "audio" and not actual["has_audio"]:
            raise CompositionError("Media asset has no audio stream")

    segments = []
    for index, shot in enumerate(timeline.shots):
        asset = assets[shot.asset_id]
        if asset.kind not in {"image", "video"}:
            raise CompositionError("Shot requires a video or image asset")
        _check_duration(metadata[shot.asset_id], shot.source_start_ms, shot.duration_ms, timeline.fps, asset.kind)
        start_frame = round(shot.start_ms * timeline.fps / 1000)
        end_frame = round((shot.start_ms + shot.duration_ms) * timeline.fps / 1000)
        if index == len(timeline.shots) - 1:
            end_frame = max(1, end_frame)
        frame_count = end_frame - start_frame
        if frame_count <= 0:
            continue
        args = base + _input_options(asset.content_type)
        if asset.kind == "image":
            args += ["-loop", "1", "-framerate", str(timeline.fps)]
        else:
            args += ["-ss", _seconds(shot.source_start_ms)]
        args += ["-i", str(controlled[shot.asset_id])]
        w, h = timeline.width, timeline.height
        if shot.fit == "contain":
            scale = (
                f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
            )
        else:
            scale = f"scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2,crop={w}:{h}"
        vf = f"{scale},setsar=1,fps={timeline.fps},tpad=stop_mode=clone:stop_duration={1 / timeline.fps:.6f}"
        segment = work / f"shot-{index}.mp4"
        args += ["-map", "0:v:0", "-an", "-vf", vf, "-frames:v", str(frame_count)] + encoding + [str(segment)]
        await _run_process(args, timeout, cwd=work)
        segments.append(segment.name)
    (work / "shots.txt").write_text("".join(f"file '{name}'\n" for name in segments), encoding="utf-8")
    args = base + ["-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "1", "-i", "shots.txt"]
    filters = []
    for index, track in enumerate(timeline.audio_tracks):
        asset = assets[track.asset_id]
        if not metadata[track.asset_id]["has_audio"] or asset.kind == "image":
            raise CompositionError("Audio track requires an audio stream")
        _check_duration(metadata[track.asset_id], track.source_start_ms, track.duration_ms, timeline.fps, "audio")
        args += _input_options(asset.content_type) + ["-i", str(controlled[track.asset_id])]
        chain = (
            f"[{index + 1}:a:0]atrim=start={_seconds(track.source_start_ms)}:duration={_seconds(track.duration_ms)},"
            f"asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,volume={track.gain:.6f}"
        )
        if track.fade_in_ms:
            chain += f",afade=t=in:st=0:d={_seconds(track.fade_in_ms)}"
        if track.fade_out_ms:
            chain += (
                f",afade=t=out:st={_seconds(track.duration_ms - track.fade_out_ms)}:d={_seconds(track.fade_out_ms)}"
            )
        chain += f",adelay={track.start_ms}:all=1,asetpts=N/SR/TB[a{index}]"
        filters.append(chain)
    total_frames = max(1, round(timeline.duration_ms * timeline.fps / 1000))
    duration = total_frames / timeline.fps
    if filters:
        labels = "".join(f"[a{i}]" for i in range(len(timeline.audio_tracks)))
        filters.append(
            f"{labels}amix=inputs={len(timeline.audio_tracks)}:normalize=0:dropout_transition=0,"
            f"asetpts=N/SR/TB,apad,atrim=duration={duration:.6f},asetpts=N/SR/TB[audio]"
        )
        args += [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[audio]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
        ]
    else:
        args += ["-map", "0:v:0", "-an"]
    if timeline.subtitles:
        _write_subtitles(work / "subtitles.ass", timeline)
        args += ["-vf", "ass=subtitles.ass"]
    args += encoding + ["-t", f"{duration:.6f}", "-r", str(timeline.fps), "-movflags", "+faststart", "output.mp4"]
    await _run_process(args, timeout, cwd=work)
    result = await probe_media(work / "output.mp4", ffprobe_bin=ffprobe, timeout_seconds=timeout)
    if (
        not result["has_video"]
        or result["width"] != timeline.width
        or result["height"] != timeline.height
        or abs(result["fps"] - timeline.fps) > 1e-6
        or abs(result["video_duration"] - duration) > 1 / timeline.fps + 1e-6
        or result["has_audio"] != bool(timeline.audio_tracks)
        or (timeline.audio_tracks and abs(result["audio_duration"] - duration) > 1024 / 48000 + 1e-6)
        or abs(result["duration"] - timeline.duration_ms / 1000) > 1 / timeline.fps + 1024 / 48000 + 1e-6
    ):
        raise CompositionError("Rendered media does not match timeline")
    os.replace(work / "output.mp4", output_path)
    return RenderResult(output_path, result)
