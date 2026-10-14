"""Offline actual-FFmpeg smoke; no database, model provider, or paid API calls.

Run with FFmpeg + ffprobe + libass/CJK fonts installed:
    PYTHONPATH=src python scripts/platform_compose_smoke.py
"""

import asyncio
import json
import tempfile
from array import array
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.infrastructure.media.composition import (
    CompositionError,
    ResolvedMediaAsset,
    _run_process,
    compose_timeline,
    probe_media,
)


async def smoke(directory: Path) -> dict:
    image = directory / "image.ppm"
    image.write_bytes(b"P6\n80 80\n255\n" + bytes([255, 0, 0]) * 6400)
    video = directory / "video.mp4"
    audio = directory / "audio.wav"
    await _run_process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=240x120:r=10:d=2",
            "-vf",
            "drawbox=x=0:y=0:w=iw:h=ih:color=lime:t=fill:enable='gte(t,1)'",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-y",
            str(video),
        ],
        20,
    )
    await _run_process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-y",
            str(audio),
        ],
        20,
    )
    spec = SimpleNamespace(
        width=160,
        height=120,
        fps=10,
        duration_ms=2000,
        shots=[
            SimpleNamespace(asset_id="image", start_ms=0, duration_ms=1000, source_start_ms=0, fit="contain"),
            SimpleNamespace(asset_id="video", start_ms=1000, duration_ms=1000, source_start_ms=500, fit="cover"),
        ],
        audio_tracks=[
            SimpleNamespace(
                asset_id="audio",
                start_ms=200,
                duration_ms=1500,
                source_start_ms=500,
                gain=0.5,
                fade_in_ms=100,
                fade_out_ms=200,
            )
        ],
        subtitles=[SimpleNamespace(start_ms=0, end_ms=1900, text="实际合成验收\n图片、视频、声音与字幕")],
    )
    assets = {
        "image": ResolvedMediaAsset("image", image, "image", "image/x-portable-pixmap"),
        "video": ResolvedMediaAsset("video", video, "video", "video/mp4"),
        "audio": ResolvedMediaAsset("audio", audio, "audio", "audio/wav"),
    }
    result = await compose_timeline(spec, assets, directory / "out.mp4", preset="draft")
    assert result.probe["has_video"] and result.probe["has_audio"]
    assert result.probe["width"] == 160 and result.probe["height"] == 120
    assert abs(result.probe["duration"] - 2) <= 0.1
    for second, channel in [(0.5, 0), (1.2, 2), (1.7, 1)]:
        pixels = await _run_process(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(second),
                "-i",
                str(result.path),
                "-vf",
                "crop=1:1:80:20,format=rgb24",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            10,
        )
        assert pixels[channel] > 200
    subtitle_pixels = await _run_process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            "1.2",
            "-i",
            str(result.path),
            "-vf",
            "crop=160:40:0:80,format=gray",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        10,
    )
    assert sum(value > 200 for value in subtitle_pixels) > 20
    samples = array(
        "f",
        await _run_process(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(result.path),
                "-map",
                "0:a:0",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-f",
                "f32le",
                "pipe:1",
            ],
            10,
        ),
    )
    assert max(abs(value) for value in samples[160:800]) < 0.01
    assert max(abs(value) for value in samples[8000:8800]) > 0.03
    pcm = directory / "voice.pcm"
    pcm.write_bytes(b"\0\0" * 24000 * 3)
    assert (await probe_media(pcm, content_type="audio/pcm"))["duration"] == 3
    spec.shots[1].source_start_ms = 1500
    try:
        await compose_timeline(spec, assets, directory / "invalid.mp4")
    except CompositionError:
        pass
    else:
        raise AssertionError("Source overrun was accepted")
    return {
        "status": "passed",
        "probe": result.probe,
        "verified": [
            "image hold",
            "source trim",
            "concat",
            "audio offsets and gain",
            "CJK subtitles",
            "PCM",
            "source overrun",
        ],
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="compose-smoke-") as directory:
        result = asyncio.run(smoke(Path(directory)))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
