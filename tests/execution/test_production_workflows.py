import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_runtime.domain.capabilities import CapabilitySnapshot
from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.execution.context import _EXECUTION, ExecutionContext
from agent_runtime.settings import RuntimeSettings
from tests.fixtures.prompts import prompt_versions


def test_timeline_cannot_reference_unfrozen_or_non_media_blocks():
    from agent_runtime.domain.timeline import Timeline
    from agent_runtime.workflows.media.planning import validate_timeline_inputs

    identity = str(uuid4())
    value = Timeline(
        width=320,
        height=180,
        fps=25,
        duration_ms=1000,
        assets=[{"asset_id": "a", "document_version_id": identity, "block_position": 0}],
        shots=[{"shot_id": "s", "asset_id": "a", "start_ms": 0, "duration_ms": 1000}],
    )
    with pytest.raises(ValueError, match="frozen"):
        validate_timeline_inputs(value, {"request": {"input_version_ids": []}}, {})
    document = ContentDocument(
        document_id="doc", project_id="project", schema_version="1.0", blocks=[ContentBlock(type="text", text="plain")]
    )
    with pytest.raises(ValueError, match="media asset"):
        validate_timeline_inputs(value, {"request": {"input_version_ids": [identity]}}, {identity: document})


def test_timeline_visual_track_cannot_use_audio_asset():
    from agent_runtime.domain.timeline import Timeline
    from agent_runtime.workflows.media.planning import validate_timeline_inputs

    identity = str(uuid4())
    timeline = Timeline(
        width=320,
        height=180,
        fps=25,
        duration_ms=1000,
        assets=[{"asset_id": "a", "document_version_id": identity, "block_position": 0}],
        shots=[{"shot_id": "s", "asset_id": "a", "start_ms": 0, "duration_ms": 1000}],
    )
    audio = ContentDocument(
        document_id="audio",
        project_id="project",
        schema_version="1.0",
        blocks=[ContentBlock(type="audio", text="voice", metadata={"asset": {"content_type": "audio/wav"}})],
    )
    with pytest.raises(ValueError, match="visual"):
        validate_timeline_inputs(timeline, {"request": {"input_version_ids": [identity]}}, {identity: audio})


async def test_storyboard_is_planned_with_database_prompts_and_published(monkeypatch):
    from agent_runtime.workflows.media.planning import plan_production

    class Models:
        def __init__(self, *args):
            pass

        def chat(self, *args):
            return self

        async def ainvoke(self, messages):
            text = messages[-1]["content"]
            value = (
                {"shots": [{"shot_id": "shot-1", "description": "车站全景", "duration_ms": 1000}]}
                if "shots 是镜头数组" in text
                else {"approved": True, "notes": []}
            )
            return SimpleNamespace(content=json.dumps(value, ensure_ascii=False))

    class Store:
        def creation_context(self, *args):
            return []

        def save_execution_plan(self, *args):
            pass

    async def publish(command, bundle, snapshot, document):
        assert document.document_type == "storyboard"
        assert document.metadata["storyboard"]["shots"][0]["description"] == "车站全景"
        return {"document_version_id": "published"}

    monkeypatch.setattr("agent_runtime.workflows.media.planning.RuntimeModels", Models)
    monkeypatch.setattr("agent_runtime.workflows.media.planning.publish_document", publish)
    snapshot = CapabilitySnapshot(
        workflow="media.storyboard@1", skills=["controlled-storyboard@1"], schema_versions={"content_ir": "1.0"}
    )
    context = ExecutionContext(
        SimpleNamespace(result={}, resume_input={}),
        Store(),
        RuntimeSettings(_env_file=None),
        prompt_versions=prompt_versions(),
    )
    bundle = {
        "request": {
            "document_type": "storyboard",
            "document_id": "doc",
            "version": 1,
            "objective": "车站分镜",
            "input_version_ids": [],
        },
        "inputs": [],
    }
    token = _EXECUTION.set(context)
    try:
        result = await plan_production(SimpleNamespace(project_id="project"), bundle, snapshot)
    finally:
        _EXECUTION.reset(token)
    assert result == {"document_version_id": "published"}


async def test_composition_reuses_archived_video_without_rendering_again(monkeypatch):
    from agent_runtime.domain.timeline import Timeline, timeline_document
    from agent_runtime.workflows.media.compose import compose_content

    source_id, asset_id = str(uuid4()), str(uuid4())
    timeline = Timeline(
        width=320,
        height=180,
        fps=25,
        duration_ms=1000,
        assets=[{"asset_id": "a", "document_version_id": asset_id, "block_position": 0}],
        shots=[{"shot_id": "s", "asset_id": "a", "start_ms": 0, "duration_ms": 1000}],
    )
    source = timeline_document(timeline, document_id="timeline", project_id="project")
    media = ContentDocument(
        document_id="image",
        project_id="project",
        schema_version="1.0",
        blocks=[ContentBlock(type="image", text="image", metadata={"asset": {"sha256": "a" * 64}})],
    )
    reference = {
        "bucket": "agent-artifacts",
        "object_key": "output",
        "sha256": "a" * 64,
        "size_bytes": 12,
        "content_type": "video/mp4",
        "etag": "tag",
    }

    class Runs:
        def __init__(self, *args):
            pass

        def get(self, *args):
            return {"state": "outputs_ready", "artifact": reference, "probe": {"duration": 1.0}, "request_hash": "hash"}

        def reserve(self, *args):
            return self.get()

    async def no_render(*args, **kwargs):
        raise AssertionError("existing video must not be rendered again")

    async def publish(command, bundle, snapshot, document):
        assert document.document_type == "video" and document.blocks[0].metadata["asset"] == reference
        return {"document_version_id": "composed"}

    monkeypatch.setattr("agent_runtime.workflows.media.compose.CompositionStore", Runs)
    monkeypatch.setattr("agent_runtime.workflows.media.compose.compose_timeline", no_render)
    monkeypatch.setattr("agent_runtime.workflows.media.compose.publish_document", publish)
    snapshot = CapabilitySnapshot(
        workflow="media.compose@1",
        skills=["controlled-compose@1"],
        schema_versions={"content_ir": "1.0", "timeline": "timeline@1", "renderer": "ffmpeg@1"},
    )
    context = ExecutionContext(
        SimpleNamespace(fencing_token=1), object(), RuntimeSettings(_env_file=None), prompt_versions=prompt_versions()
    )
    bundle = {
        "source": {"document_version_id": source_id, "document": source.model_dump(mode="json")},
        "inputs": [{"document_version_id": asset_id, "document": media.model_dump(mode="json")}],
        "request": {
            "document_id": "doc",
            "version": 1,
            "title": "Compose",
            "input_version_ids": [asset_id],
            "arguments": {"compose": {}},
        },
    }
    token = _EXECUTION.set(context)
    try:
        result = await compose_content(
            SimpleNamespace(workspace_id="ws", project_id="project", job_id="job"), bundle, snapshot
        )
    finally:
        _EXECUTION.reset(token)
    assert result == {"document_version_id": "composed"}
