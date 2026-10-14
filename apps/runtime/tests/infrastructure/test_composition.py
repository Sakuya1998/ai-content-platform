"""Composition validates real streams and kills all render subprocesses on cancellation."""

import asyncio
import shutil
import sys
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.infrastructure.media.composition import (
    CompositionError,
    ResolvedMediaAsset,
    _run_process,
    compose_timeline,
    probe_media,
)


def timeline(**updates):
    values = dict(
        width=160,
        height=120,
        fps=10,
        duration_ms=2000,
        shots=[
            SimpleNamespace(asset_id="image", start_ms=0, duration_ms=1000, source_start_ms=0, fit="contain"),
            SimpleNamespace(asset_id="video", start_ms=1000, duration_ms=1000, source_start_ms=500, fit="cover"),
        ],
        audio_tracks=[],
        subtitles=[],
    )
    values.update(updates)
    return SimpleNamespace(**values)


async def test_errors_do_not_expose_subprocess_stderr():
    with pytest.raises(CompositionError) as error:
        await _run_process([sys.executable, "-c", "import sys;sys.stderr.write('private manuscript');sys.exit(1)"], 2)
    assert "private manuscript" not in str(error.value)


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancel_kill_process_group(tmp_path, cancel):
    marker = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "open(sys.argv[1],'w').write(str(p.pid));time.sleep(60)"
    )
    task = asyncio.create_task(_run_process([sys.executable, "-c", code, str(marker)], 0.5))
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CompositionError, match="timed out"):
            await task
    pid = int(marker.read_text())
    # A killed child can remain a zombie until container PID 1 reaps it.
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


async def test_rejects_unresolved_asset_without_starting_binary(tmp_path):
    with pytest.raises(CompositionError, match="asset"):
        await compose_timeline(timeline(), {}, tmp_path / "out.mp4", ffmpeg_bin="missing-binary")
    assert not (tmp_path / "out.mp4").exists()


@pytest.fixture
def ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("actual FFmpeg binaries are not installed")
    return "ffmpeg"


async def fixtures(tmp_path):
    image = tmp_path / "source.ppm"
    image.write_bytes(b"P6\n80 80\n255\n" + bytes([255, 0, 0]) * 6400)
    video = tmp_path / "source.mp4"
    audio = tmp_path / "source.wav"
    await _run_process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=240x120:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-vf",
            "drawbox=x=0:y=0:w=iw:h=ih:color=lime:t=fill:enable='gte(t,1)'",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-c:a",
            "aac",
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
    return {
        "image": ResolvedMediaAsset("image", image, "image", "image/x-portable-pixmap"),
        "video": ResolvedMediaAsset("video", video, "video", "video/mp4"),
        "audio": ResolvedMediaAsset("audio", audio, "audio", "audio/wav"),
    }


async def test_real_image_video_concat_has_no_implicit_original_audio(tmp_path, ffmpeg):
    result = await compose_timeline(timeline(), await fixtures(tmp_path), tmp_path / "out.mp4")
    assert result.path.is_file()
    assert result.probe["width"] == 160 and result.probe["height"] == 120
    assert result.probe["has_video"] and not result.probe["has_audio"]
    assert abs(result.probe["duration"] - 2) <= 0.1
    # Observe decoded output: first shot is red; a trimmed video changes from blue to green.
    for second, expected in [(0.5, 0), (1.2, 2), (1.7, 1)]:
        frame = await _run_process(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(second),
                "-i",
                str(result.path),
                "-vf",
                "crop=1:1:80:60,format=rgb24",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            10,
        )
        assert frame[expected] > 200


async def test_real_trim_mix_gain_fades_and_cjk_subtitles(tmp_path, ffmpeg):
    tracks = [
        SimpleNamespace(
            asset_id="audio",
            start_ms=200,
            duration_ms=1500,
            source_start_ms=500,
            gain=0.5,
            fade_in_ms=100,
            fade_out_ms=200,
        ),
        SimpleNamespace(
            asset_id="audio",
            start_ms=600,
            duration_ms=1000,
            source_start_ms=0,
            gain=0.25,
            fade_in_ms=0,
            fade_out_ms=0,
        ),
    ]
    cues = [SimpleNamespace(start_ms=0, end_ms=1900, text="中文字幕 {\\pos(0,0)}\n第二行")]
    result = await compose_timeline(
        timeline(audio_tracks=tracks, subtitles=cues),
        await fixtures(tmp_path),
        tmp_path / "subtitled.mp4",
    )
    assert result.probe["has_audio"]
    assert abs(result.probe["duration"] - 2) <= 0.1
    audio = await _run_process(
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
    )
    samples = array("f", audio)

    def rms(start, end):
        values = samples[round(start * 8000) : round(end * 8000)]
        return (sum(v * v for v in values) / len(values)) ** 0.5

    assert rms(0.02, 0.15) < 0.002
    assert 0.045 < rms(0.8, 1.1) < 0.08
    assert rms(1.85, 1.95) < 0.002
    pixels = await _run_process(
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
    assert sum(value > 200 for value in pixels) > 20


async def test_real_source_duration_and_stream_kind_are_checked(tmp_path, ffmpeg):
    assets = await fixtures(tmp_path)
    bad = timeline()
    bad.shots[1].source_start_ms = 1500
    with pytest.raises(CompositionError, match="duration"):
        await compose_timeline(bad, assets, tmp_path / "out.mp4")
    assets["video"] = ResolvedMediaAsset("video", assets["audio"].path, "video", "audio/wav")
    with pytest.raises(CompositionError, match="video"):
        await compose_timeline(timeline(), assets, tmp_path / "out.mp4")
    assert not (tmp_path / "out.mp4").exists()


async def test_probe_disallows_network_paths():
    with pytest.raises(CompositionError):
        await probe_media(Path("https://private.example/file.mp4"))


async def test_real_pcm_timing_and_audio_compose(tmp_path, ffmpeg):
    assets = await fixtures(tmp_path)
    pcm = tmp_path / "voice.pcm"
    pcm.write_bytes(b"\x00\x00" * 24000 * 3)
    assets["audio"] = ResolvedMediaAsset("audio", pcm, "audio", "audio/pcm")
    info = await probe_media(pcm, content_type="audio/pcm")
    assert info["has_audio"] and info["duration"] == pytest.approx(3)
    track = SimpleNamespace(
        asset_id="audio", start_ms=0, duration_ms=2000, source_start_ms=1000, gain=1, fade_in_ms=0, fade_out_ms=0
    )
    result = await compose_timeline(timeline(audio_tracks=[track]), assets, tmp_path / "pcm.mp4")
    assert result.probe["has_audio"]


async def test_real_disguised_playlist_is_rejected(tmp_path, ffmpeg):
    assets = await fixtures(tmp_path)
    playlist = tmp_path / "evil.mp4"
    playlist.write_text("#EXTM3U\n#EXTINF:1\n/etc/passwd\n")
    assets["video"] = ResolvedMediaAsset("video", playlist, "video", "video/mp4")
    with pytest.raises(CompositionError):
        await compose_timeline(timeline(), assets, tmp_path / "out.mp4")


async def test_subprocess_output_limit_does_not_hang_cleanup():
    with pytest.raises(CompositionError, match="output exceeded"):
        await asyncio.wait_for(
            _run_process([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*4000000)"], 2),
            timeout=3,
        )


@pytest.mark.parametrize("cancel", [False, True])
async def test_compose_timeout_cancel_removes_temporary_files(tmp_path, monkeypatch, cancel):
    import agent_runtime.infrastructure.media.composition as composition

    async def image_probe(*args, **kwargs):
        return dict(duration=0, width=80, height=80, has_video=True, has_audio=False, fps=0)

    monkeypatch.setattr(composition, "probe_media", image_probe)
    image = tmp_path / "source.ppm"
    image.write_bytes(b"P6\n80 80\n255\n" + bytes([255, 0, 0]) * 6400)
    marker = tmp_path / "render.pid"
    executable = tmp_path / "renderer"
    executable.write_text(
        f"#!{sys.executable}\nimport os,time\nopen({str(marker)!r},'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    )
    executable.chmod(0o700)
    spec = timeline()
    spec.shots = spec.shots[:1]
    spec.duration_ms = 1000
    output = tmp_path / "result.mp4"
    task = asyncio.create_task(
        compose_timeline(
            spec,
            {"image": ResolvedMediaAsset("image", image, "image", "image/x-portable-pixmap")},
            output,
            ffmpeg_bin=str(executable),
            timeout_seconds=0.5,
        )
    )
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CompositionError, match="timed out"):
            await task
    assert not Path(f"/proc/{marker.read_text()}").exists()
    assert not output.exists()
    assert not list(tmp_path.glob("compose-*"))


@pytest.mark.parametrize("preset", ["draft", "standard"])
async def test_real_single_audio_track_has_decodable_samples(tmp_path, ffmpeg, preset):
    track = SimpleNamespace(
        asset_id="audio", start_ms=200, duration_ms=1500, source_start_ms=500, gain=0.5, fade_in_ms=100, fade_out_ms=200
    )
    result = await compose_timeline(
        timeline(audio_tracks=[track]), await fixtures(tmp_path), tmp_path / "single.mp4", preset=preset
    )
    raw = await _run_process(
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
    )
    assert len(raw) >= 2 * 8000 * 4
    assert max(abs(value) for value in array("f", raw)[8000:8800]) > 0.03
    assert result.probe["audio_duration"] == pytest.approx(2, abs=0.03)


async def test_cancel_during_asset_copy_waits_for_copy_before_cleanup(tmp_path, monkeypatch):
    import threading

    import agent_runtime.infrastructure.media.composition as composition

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = shutil.copyfile

    def slow_copy(source, destination):
        started.set()
        assert release.wait(2)
        original(source, destination)
        finished.set()

    monkeypatch.setattr(composition.shutil, "copyfile", slow_copy)
    image = tmp_path / "image.ppm"
    image.write_bytes(b"P6\n1 1\n255\n\xff\0\0")
    spec = timeline()
    spec.shots = spec.shots[:1]
    task = asyncio.create_task(
        compose_timeline(
            spec,
            {"image": ResolvedMediaAsset("image", image, "image", "image/x-portable-pixmap")},
            tmp_path / "out.mp4",
        )
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    task.cancel()
    await asyncio.sleep(0.02)
    released_before_cleanup = not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released_before_cleanup
    assert finished.is_set()
    assert not list(tmp_path.glob("compose-*"))
