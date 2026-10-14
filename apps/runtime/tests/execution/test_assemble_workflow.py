from types import SimpleNamespace
from uuid import uuid4

from agent_runtime.domain.capabilities import CapabilitySnapshot
from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.domain.storyboard import Storyboard, storyboard_document
from agent_runtime.execution.context import _EXECUTION, ExecutionContext
from agent_runtime.execution.errors import WaitingReview
from agent_runtime.settings import RuntimeSettings
from tests.fixtures.prompts import prompt_versions


def bundle():
    source_id, image_id, audio_id = map(str, [uuid4(), uuid4(), uuid4()])
    board = storyboard_document(
        Storyboard(shots=[{"shot_id": "s1", "description": "画面", "duration_ms": 1000, "dialogue": "欢迎来到车站。"}]),
        document_id="board",
        project_id="project",
    )

    def media(kind):
        return ContentDocument(
            document_id=kind,
            project_id="project",
            document_type=kind,
            schema_version="1.0",
            blocks=[
                ContentBlock(
                    type=kind,
                    text=kind,
                    metadata={
                        "asset": {
                            "bucket": "agent-artifacts",
                            "object_key": f"workspaces/ws/projects/project/assets/{kind}/versions/v/input.bin",
                            "sha256": "a" * 64,
                            "size_bytes": 8,
                            "content_type": "image/png" if kind == "image" else "audio/wav",
                            "etag": "tag",
                        }
                    },
                )
            ],
        )

    return {
        "source": {"document_version_id": source_id, "document": board.model_dump(mode="json")},
        "inputs": [
            {"document_version_id": image_id, "document": media("image").model_dump(mode="json")},
            {"document_version_id": audio_id, "document": media("audio").model_dump(mode="json")},
        ],
        "request": {
            "document_id": "timeline",
            "document_type": "timeline",
            "version": 1,
            "title": "Timeline",
            "operation": "assemble",
            "input_version_ids": [image_id, audio_id],
            "arguments": {
                "assemble": {
                    "width": 320,
                    "height": 180,
                    "fps": 25,
                    "shots": [{"shot_id": "s1", "visual_version_id": image_id, "audio_version_id": audio_id}],
                }
            },
        },
    }


async def test_assemble_extends_image_shot_to_preserve_complete_narration(monkeypatch):
    from agent_runtime.workflows.media.assemble import assemble_content

    class Artifacts:
        def __init__(self, *args):
            self.bucket = "agent-artifacts"

    monkeypatch.setattr("agent_runtime.workflows.media.assemble.S3ArtifactStore", Artifacts)
    monkeypatch.setattr("agent_runtime.workflows.media.assemble.read_asset", lambda *args: b"\x89PNG\r\n\x1a\n")

    async def probe(path, **kwargs):
        return {
            "has_video": kwargs["content_type"] == "image/png",
            "has_audio": kwargs["content_type"] == "audio/wav",
            "video_duration": 0,
            "audio_duration": 1.5,
            "duration": 1.5,
        }

    async def publish(command, values, snapshot, document):
        timeline = document.metadata["timeline"]
        assert timeline["duration_ms"] == 1500 and timeline["audio_tracks"][0]["duration_ms"] == 1500
        assert timeline["subtitles"][0]["text"] == "欢迎来到车站。"
        return {"document_version_id": "timeline-version"}

    monkeypatch.setattr("agent_runtime.workflows.media.assemble.probe_media", probe)
    monkeypatch.setattr("agent_runtime.workflows.media.assemble.publish_document", publish)
    context = ExecutionContext(
        SimpleNamespace(), object(), RuntimeSettings(_env_file=None), prompt_versions=prompt_versions()
    )
    snapshot = CapabilitySnapshot(
        workflow="media.assemble@1",
        skills=["controlled-assemble@1"],
        schema_versions={"content_ir": "1.0", "storyboard": "storyboard@1", "timeline": "timeline@1"},
    )
    token = _EXECUTION.set(context)
    try:
        result = await assemble_content(
            SimpleNamespace(workspace_id="ws", project_id="project", job_id="job"), bundle(), snapshot
        )
    finally:
        _EXECUTION.reset(token)
    assert result == {"document_version_id": "timeline-version"}


async def test_assemble_probing_failure_is_reviewable_without_media_regeneration(monkeypatch):
    from agent_runtime.infrastructure.media.composition import CompositionError
    from agent_runtime.workflows.media.assemble import assemble_content

    class Artifacts:
        def __init__(self, *args):
            self.bucket = "agent-artifacts"

    async def fail(*args, **kwargs):
        raise CompositionError("invalid media")

    monkeypatch.setattr("agent_runtime.workflows.media.assemble.S3ArtifactStore", Artifacts)
    monkeypatch.setattr("agent_runtime.workflows.media.assemble.read_asset", lambda *args: b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr("agent_runtime.workflows.media.assemble.probe_media", fail)
    context = ExecutionContext(
        SimpleNamespace(), object(), RuntimeSettings(_env_file=None), prompt_versions=prompt_versions()
    )
    snapshot = CapabilitySnapshot(
        workflow="media.assemble@1",
        skills=["controlled-assemble@1"],
        schema_versions={"content_ir": "1.0", "storyboard": "storyboard@1", "timeline": "timeline@1"},
    )
    token = _EXECUTION.set(context)
    try:
        result = await assemble_content(
            SimpleNamespace(workspace_id="ws", project_id="project", job_id="job"), bundle(), snapshot
        )
    finally:
        _EXECUTION.reset(token)
    assert isinstance(result, WaitingReview)
