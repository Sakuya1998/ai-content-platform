import copy

import jsonschema
import pytest
from pydantic import ValidationError

from agent_runtime.domain.content import ContentDocument
from agent_runtime.domain.timeline import Timeline, timeline_document, timeline_from_document


def valid_timeline():
    return {
        "schema_version": "timeline@1",
        "width": 1280,
        "height": 720,
        "fps": 24,
        "duration_ms": 4000,
        "assets": [
            {"asset_id": "image-1", "document_version_id": "12345678-1234-5678-9234-567812345678", "block_position": 0},
            {"asset_id": "voice-1", "document_version_id": "87654321-4321-8765-9321-876543218765", "block_position": 2},
        ],
        "shots": [
            {"shot_id": "opening", "asset_id": "image-1", "start_ms": 0, "duration_ms": 2000, "fit": "cover"},
            {"shot_id": "closing", "asset_id": "image-1", "start_ms": 2000, "duration_ms": 2000, "fit": "contain"},
        ],
        "audio_tracks": [
            {"track_id": "voice", "asset_id": "voice-1", "start_ms": 1000, "duration_ms": 2000, "role": "narration"},
            {"track_id": "echo", "asset_id": "voice-1", "start_ms": 1000, "duration_ms": 2000, "role": "effect"},
        ],
        "subtitles": [
            {"cue_id": "line-1", "start_ms": 1000, "end_ms": 2500, "text": "月亮升起来了。"},
            {"cue_id": "line-2", "start_ms": 2000, "end_ms": 3000, "text": "我们回家。"},
        ],
    }


def test_timeline_preserves_chinese_and_overlapping_audio_subtitles_in_content_ir():
    timeline = Timeline.model_validate(valid_timeline())
    assert timeline.shots[0].source_start_ms == 0
    assert timeline.audio_tracks[0].gain == 1
    document = timeline_document(timeline, document_id="timeline-1", project_id="project-1", version=2)
    assert document.schema_version == "1.0"
    assert document.document_type == "timeline"
    assert document.metadata["timeline"]["schema_version"] == "timeline@1"
    assert document.metadata["timeline"]["subtitles"][0]["text"] == "月亮升起来了。"
    assert timeline_from_document(ContentDocument.model_validate_json(document.model_dump_json())) == timeline
    jsonschema.validate(timeline.model_dump(mode="json"), Timeline.model_json_schema())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 63),
        ("width", 1279),
        ("width", 1922),
        ("height", 1622),
        ("width", True),
        ("height", "720"),
        ("fps", True),
        ("fps", 24.0),
        ("fps", float("nan")),
        ("fps", float("inf")),
        ("fps", 0),
        ("fps", 61),
        ("duration_ms", True),
        ("duration_ms", 600001),
        ("duration_ms", 0),
        ("schema_version", "timeline@2"),
        ("url", "https://example.com"),
        ("command", "ffmpeg"),
    ],
)
def test_timeline_rejects_invalid_render_settings(field, value):
    payload = valid_timeline()
    payload[field] = value
    with pytest.raises(ValidationError):
        Timeline.model_validate(payload)


@pytest.mark.parametrize(
    ("collection", "field", "value"),
    [
        ("assets", "asset_id", "../../image"),
        ("assets", "asset_id", "镜头"),
        ("assets", "asset_id", "x" * 129),
        ("assets", "document_version_id", "https://example.com/a"),
        ("assets", "document_version_id", "12345678123456789234567812345678"),
        ("assets", "block_position", True),
        ("assets", "block_position", -1),
        ("shots", "asset_id", "unknown"),
        ("shots", "start_ms", 1),
        ("shots", "duration_ms", 0),
        ("shots", "duration_ms", 2500),
        ("shots", "source_start_ms", -1),
        ("shots", "fit", "stretch"),
        ("shots", "filter", "movie=/etc/passwd"),
        ("audio_tracks", "asset_id", "unknown"),
        ("audio_tracks", "duration_ms", 4000),
        ("audio_tracks", "source_start_ms", True),
        ("audio_tracks", "gain", True),
        ("audio_tracks", "gain", float("nan")),
        ("audio_tracks", "gain", float("inf")),
        ("audio_tracks", "gain", -0.1),
        ("audio_tracks", "gain", 2.1),
        ("audio_tracks", "role", "dialogue"),
        ("audio_tracks", "fade_in_ms", 2001),
        ("audio_tracks", "fade_out_ms", -1),
        ("subtitles", "start_ms", -1),
        ("subtitles", "end_ms", 4001),
        ("subtitles", "end_ms", 1000),
        ("subtitles", "text", "\u0000bad"),
        ("subtitles", "text", "\ntext"),
        ("subtitles", "text", "\u202eevil"),
        ("subtitles", "text", "   "),
    ],
)
def test_timeline_rejects_invalid_nested_values(collection, field, value):
    payload = valid_timeline()
    payload[collection][0][field] = value
    with pytest.raises(ValidationError):
        Timeline.model_validate(payload)


@pytest.mark.parametrize("collection", ["assets", "shots", "audio_tracks", "subtitles"])
def test_timeline_rejects_duplicate_identifiers(collection):
    payload = valid_timeline()
    payload[collection].append(copy.deepcopy(payload[collection][0]))
    with pytest.raises(ValidationError):
        Timeline.model_validate(payload)


@pytest.mark.parametrize(
    ("collection", "limit"), [("assets", 256), ("shots", 128), ("audio_tracks", 128), ("subtitles", 256)]
)
def test_timeline_bounds_collection_sizes(collection, limit):
    payload = valid_timeline()
    identifier = {"assets": "asset_id", "shots": "shot_id", "audio_tracks": "track_id", "subtitles": "cue_id"}[
        collection
    ]
    values = []
    for index in range(limit + 1):
        item = copy.deepcopy(payload[collection][0])
        item[identifier] = f"entry-{index}"
        if collection == "shots":
            item.update(start_ms=index, duration_ms=1)
        values.append(item)
    if collection == "assets":
        values[0]["asset_id"] = "image-1"
        values[1]["asset_id"] = "voice-1"
    if collection == "shots":
        payload.update(duration_ms=limit + 1, audio_tracks=[], subtitles=[])
    payload[collection] = values
    with pytest.raises(ValidationError):
        Timeline.model_validate(payload)
    payload[collection] = values[:limit]
    if collection == "shots":
        payload["duration_ms"] = limit
    assert len(getattr(Timeline.model_validate(payload), collection)) == limit


def test_timeline_accepts_full_hd_and_maximum_duration():
    payload = valid_timeline()
    payload.update(width=1920, height=1080, fps=60, duration_ms=600000)
    payload["shots"] = [{"shot_id": "full", "asset_id": "image-1", "start_ms": 0, "duration_ms": 600000}]
    timeline = Timeline.model_validate(payload)
    assert timeline.duration_ms == 600000


def test_timeline_helpers_revalidate_mutated_nested_collections():
    timeline = Timeline.model_validate(valid_timeline())
    timeline.shots.pop()
    with pytest.raises(ValidationError):
        timeline_document(timeline, document_id="timeline-1")


def test_timeline_requires_complete_continuous_visual_coverage():
    for shots in [[], valid_timeline()["shots"][:1], list(reversed(valid_timeline()["shots"]))]:
        payload = valid_timeline()
        payload["shots"] = shots
        with pytest.raises(ValidationError):
            Timeline.model_validate(payload)


def test_timeline_rejects_combined_fades_exceeding_track_duration():
    payload = valid_timeline()
    payload["audio_tracks"][0].update(fade_in_ms=1500, fade_out_ms=1000)
    with pytest.raises(ValidationError):
        Timeline.model_validate(payload)


@pytest.mark.parametrize(("field", "value"), [("document_type", "video"), ("schema_version", "2.0"), ("metadata", {})])
def test_timeline_from_document_rejects_incompatible_content_ir(field, value):
    document = timeline_document(Timeline.model_validate(valid_timeline()), document_id="timeline-1")
    payload = document.model_dump()
    payload[field] = value
    with pytest.raises(ValueError):
        timeline_from_document(ContentDocument.model_validate(payload))
