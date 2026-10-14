import copy

import pytest
from pydantic import ValidationError

from agent_runtime.domain.assembly import (
    AssemblyRequest,
    AssemblyReviewRequired,
    ProbedAsset,
    assemble_timeline,
)
from agent_runtime.domain.storyboard import Storyboard
from agent_runtime.domain.timeline import Timeline

IMAGE = "12345678-1234-5678-9234-567812345678"
VOICE = "87654321-4321-8765-9321-876543218765"
VIDEO = "00000000-0000-4000-8000-000000000001"
MUSIC = "00000000-0000-4000-8000-000000000002"
EFFECT_ONE = "00000000-0000-4000-8000-000000000003"
EFFECT_TWO = "00000000-0000-4000-8000-000000000004"


def storyboard(*, dialogue="月亮升起来了。", duration_ms=2000, second=True):
    shots = [{"shot_id": "opening", "description": "月下庭院", "dialogue": dialogue, "duration_ms": duration_ms}]
    if second:
        shots.append({"shot_id": "closing", "description": "回家", "duration_ms": 1000})
    return Storyboard.model_validate({"shots": shots})


def request_payload(*, visual=IMAGE, audio=VOICE, second=True):
    opening = {"shot_id": "opening", "visual_version_id": visual, "visual_block_position": 0}
    if audio is not None:
        opening.update(audio_version_id=audio, audio_block_position=2)
    shots = [opening]
    if second:
        shots.append({"shot_id": "closing", "visual_version_id": IMAGE, "visual_block_position": 0})
    return {"shots": shots}


def probes(*, voice_duration=3000):
    return {
        (IMAGE, 0): ProbedAsset(IMAGE, 0, "image", 0),
        (VOICE, 2): ProbedAsset(VOICE, 2, "audio", voice_duration),
    }


def test_image_extends_to_full_narration_and_keeps_next_shot_sequential():
    request = AssemblyRequest.model_validate(request_payload())
    timeline = assemble_timeline(storyboard(), request, probes())
    assert isinstance(timeline, Timeline)
    assert (timeline.width, timeline.height, timeline.fps, timeline.duration_ms) == (1280, 720, 25, 4000)
    assert [(shot.shot_id, shot.start_ms, shot.duration_ms) for shot in timeline.shots] == [
        ("opening", 0, 3000),
        ("closing", 3000, 1000),
    ]
    assert all(shot.source_start_ms == 0 and shot.fit == "contain" for shot in timeline.shots)
    assert len(timeline.assets) == 2
    assert timeline.shots[0].asset_id == timeline.shots[1].asset_id
    track = timeline.audio_tracks[0]
    assert (track.start_ms, track.duration_ms, track.source_start_ms, track.gain, track.role) == (
        0,
        3000,
        0,
        1.0,
        "narration",
    )
    assert (track.fade_in_ms, track.fade_out_ms) == (0, 0)
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in timeline.subtitles] == [(0, 3000, "月亮升起来了。")]
    assert assemble_timeline(storyboard(), request, probes()).model_dump() == timeline.model_dump()


def test_short_narration_keeps_planned_image_hold_and_uses_actual_subtitle_end():
    timeline = assemble_timeline(
        storyboard(), AssemblyRequest.model_validate(request_payload()), probes(voice_duration=800)
    )
    assert timeline.shots[0].duration_ms == 2000
    assert timeline.audio_tracks[0].duration_ms == timeline.subtitles[0].end_ms == 800


@pytest.mark.parametrize("planned,voice,video", [(2000, 1000, 1999), (2000, 3000, 2999)])
def test_video_insufficient_for_plan_or_complete_narration_requires_review(planned, voice, video):
    media = probes(voice_duration=voice)
    media[(VIDEO, 0)] = ProbedAsset(VIDEO, 0, "video", video)
    with pytest.raises(AssemblyReviewRequired):
        assemble_timeline(
            storyboard(duration_ms=planned), AssemblyRequest.model_validate(request_payload(visual=VIDEO)), media
        )


def test_video_uses_actual_visual_stream_duration_and_audio_track_keeps_full_narration():
    media = probes()
    media[(VIDEO, 0)] = ProbedAsset(VIDEO, 0, "video", 9000, visual_video_duration_ms=3000)
    timeline = assemble_timeline(storyboard(), AssemblyRequest.model_validate(request_payload(visual=VIDEO)), media)
    assert timeline.shots[0].duration_ms == 3000
    assert timeline.audio_tracks[0].duration_ms == 3000
    media[(VIDEO, 0)] = ProbedAsset(VIDEO, 0, "video", 9000, visual_video_duration_ms=2999)
    with pytest.raises(AssemblyReviewRequired):
        assemble_timeline(storyboard(), AssemblyRequest.model_validate(request_payload(visual=VIDEO)), media)


def test_video_audio_binding_requires_an_audio_stream_and_uses_its_duration():
    media = probes()
    media[(VIDEO, 2)] = ProbedAsset(VIDEO, 2, "video", 9000, audio_duration_ms=3500)
    request = AssemblyRequest.model_validate(request_payload(audio=VIDEO))
    timeline = assemble_timeline(storyboard(), request, media)
    assert timeline.audio_tracks[0].duration_ms == 3500
    assert timeline.shots[0].duration_ms == 3500
    media[(VIDEO, 2)] = ProbedAsset(VIDEO, 2, "video", 9000)
    with pytest.raises(ValueError, match="audio"):
        assemble_timeline(storyboard(), request, media)


@pytest.mark.parametrize(
    "change", ["missing_visual", "missing_audio", "foreign_identity", "visual_audio", "audio_image"]
)
def test_missing_or_incompatible_probe_references_are_rejected(change):
    media = probes()
    if change == "missing_visual":
        del media[(IMAGE, 0)]
    elif change == "missing_audio":
        del media[(VOICE, 2)]
    elif change == "foreign_identity":
        media[(IMAGE, 0)] = ProbedAsset(VIDEO, 0, "image", 0)
    elif change == "visual_audio":
        media[(IMAGE, 0)] = ProbedAsset(IMAGE, 0, "audio", 3000)
    else:
        media[(VOICE, 2)] = ProbedAsset(VOICE, 2, "image", 0)
    with pytest.raises(ValueError):
        assemble_timeline(storyboard(), AssemblyRequest.model_validate(request_payload()), media)


@pytest.mark.parametrize("change", ["reverse", "missing", "foreign"])
def test_bindings_must_match_every_storyboard_shot_in_order(change):
    payload = request_payload()
    if change == "reverse":
        payload["shots"].reverse()
    elif change == "missing":
        payload["shots"].pop()
    else:
        payload["shots"][1]["shot_id"] = "foreign"
    with pytest.raises(ValueError, match="order"):
        assemble_timeline(storyboard(), AssemblyRequest.model_validate(payload), probes())


def test_subtitle_whitespace_is_normalized_without_cutting_chinese_dialogue():
    timeline = assemble_timeline(
        storyboard(dialogue="  月亮\r\n升起来了。\t 我们回家。  "),
        AssemblyRequest.model_validate(request_payload()),
        probes(),
    )
    assert timeline.subtitles[0].text == "月亮 升起来了。 我们回家。"


@pytest.mark.parametrize("dialogue", ["hello\x00world", "hello\u202eworld", "中" * 2001])
def test_subtitle_unsafe_or_oversized_text_requires_review_without_truncation(dialogue):
    with pytest.raises(AssemblyReviewRequired):
        assemble_timeline(storyboard(dialogue=dialogue), AssemblyRequest.model_validate(request_payload()), probes())


def test_subtitle_full_cue_limit_is_preserved():
    timeline = assemble_timeline(
        storyboard(dialogue="中" * 2000), AssemblyRequest.model_validate(request_payload()), probes()
    )
    assert timeline.subtitles[0].text == "中" * 2000


@pytest.mark.parametrize(
    "subtitles,audio,dialogue", [(False, VOICE, "对白"), (True, None, "对白"), (True, VOICE, " \n ")]
)
def test_subtitles_only_emit_for_enabled_nonblank_narration(subtitles, audio, dialogue):
    payload = request_payload(audio=audio)
    payload["subtitles"] = subtitles
    assert (
        assemble_timeline(storyboard(dialogue=dialogue), AssemblyRequest.model_validate(payload), probes()).subtitles
        == []
    )


def test_zero_duration_audio_is_rejected():
    with pytest.raises(ValueError, match="audio"):
        assemble_timeline(storyboard(), AssemblyRequest.model_validate(request_payload()), probes(voice_duration=0))


def test_music_and_effects_become_positioned_timeline_tracks():
    payload = request_payload()
    payload.update(
        music_version_id=MUSIC,
        music_block_position=0,
        effects=[
            {"shot_id": "opening", "version_id": EFFECT_ONE, "block_position": 0},
            {"shot_id": "closing", "version_id": EFFECT_TWO, "block_position": 0},
        ],
    )
    media = probes(voice_duration=3000)
    media[(MUSIC, 0)] = ProbedAsset(MUSIC, 0, "audio", 6000)
    media[(EFFECT_ONE, 0)] = ProbedAsset(EFFECT_ONE, 0, "audio", 1400)
    media[(EFFECT_TWO, 0)] = ProbedAsset(EFFECT_TWO, 0, "audio", 2000)

    timeline = assemble_timeline(storyboard(), AssemblyRequest.model_validate(payload), media)

    tracks = {track.track_id: track for track in timeline.audio_tracks}
    assert (
        tracks["music"].start_ms,
        tracks["music"].duration_ms,
        tracks["music"].role,
        tracks["music"].gain,
    ) == (0, 4000, "music", 0.25)
    assert (
        tracks["effect-opening"].start_ms,
        tracks["effect-opening"].duration_ms,
        tracks["effect-opening"].role,
        tracks["effect-opening"].gain,
    ) == (
        0,
        1400,
        "effect",
        0.85,
    )
    assert (
        tracks["effect-closing"].start_ms,
        tracks["effect-closing"].duration_ms,
        tracks["effect-closing"].role,
        tracks["effect-closing"].gain,
    ) == (
        3000,
        1000,
        "effect",
        0.85,
    )
    assert len(timeline.assets) == 5


def test_music_and_effect_references_must_be_frozen_and_match_shot_ids():
    payload = request_payload()
    payload["effects"] = [{"shot_id": "foreign", "version_id": EFFECT_ONE, "block_position": 0}]
    with pytest.raises(ValidationError, match="shot"):
        AssemblyRequest.model_validate(payload)


def test_total_duration_limit_requires_review_instead_of_truncation():
    request = AssemblyRequest.model_validate(request_payload())
    assert assemble_timeline(storyboard(duration_ms=599000), request, probes()).duration_ms == 600000
    with pytest.raises(AssemblyReviewRequired):
        assemble_timeline(storyboard(duration_ms=600000), request, probes())


@pytest.mark.parametrize(
    "field,value",
    [("width", 1279), ("height", 1920), ("fps", 0), ("fps", True), ("subtitles", "true"), ("url", "file:///tmp/x")],
)
def test_request_render_contract_rejects_invalid_or_executable_parameters(field, value):
    payload = request_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        AssemblyRequest.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("shot_id", "../shot"),
        ("visual_version_id", "https://example.com/a"),
        ("visual_version_id", "12345678123456789234567812345678"),
        ("audio_version_id", "87654321-4321-8765-9321-87654321876A"),
        ("visual_block_position", -1),
        ("audio_block_position", True),
        ("command", "ffmpeg"),
    ],
)
def test_shot_bindings_require_safe_typed_canonical_references(field, value):
    payload = request_payload()
    payload["shots"][0][field] = value
    with pytest.raises(ValidationError):
        AssemblyRequest.model_validate(payload)


def test_request_bounds_shots_and_rejects_duplicate_bindings_or_orphan_audio_position():
    payload = request_payload()
    payload["shots"].append(copy.deepcopy(payload["shots"][0]))
    with pytest.raises(ValidationError):
        AssemblyRequest.model_validate(payload)
    payload["shots"] = []
    with pytest.raises(ValidationError):
        AssemblyRequest.model_validate(payload)
    payload["shots"] = [dict(shot_id=f"shot-{i}", visual_version_id=IMAGE) for i in range(33)]
    with pytest.raises(ValidationError):
        AssemblyRequest.model_validate(payload)
    payload = request_payload(audio=None)
    payload["shots"][0]["audio_block_position"] = 1
    with pytest.raises(ValidationError):
        AssemblyRequest.model_validate(payload)


def test_assembly_revalidates_mutated_request_lists():
    request = AssemblyRequest.model_validate(request_payload())
    request.shots.append(request.shots[0])
    with pytest.raises(ValueError):
        assemble_timeline(storyboard(), request, probes())


@pytest.mark.parametrize("kind,duration", [("image", -1), ("video", True), ("executable", 10)])
def test_probed_asset_rejects_untyped_or_invalid_probe_values(kind, duration):
    with pytest.raises(ValueError):
        ProbedAsset(IMAGE, 0, kind, duration)
