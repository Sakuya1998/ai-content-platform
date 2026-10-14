from types import SimpleNamespace

import pytest

from agent_runtime.execution.errors import WaitingReview


async def test_unknown_sync_request_cannot_create_second_provider_call():
    from agent_runtime.workflows.media.generated import execute_generated_media

    class Store:
        def get(self, *args):
            return SimpleNamespace(state="submission_unknown")

    async def create(task):
        raise AssertionError("unknown synchronous requests must not replay")

    async def recover(task):
        return None

    result = await execute_generated_media(Store(), object(), "worker", create, recover)
    assert isinstance(result, WaitingReview) and result["media_status"] == "submission_unknown"


async def test_receipt_restores_outputs_after_archive_before_database_commit():
    from agent_runtime.workflows.media.generated import execute_generated_media

    class Store:
        def get(self, *args):
            return SimpleNamespace(state="submission_unknown")

        def archived(self, job, worker, outputs, usage, *, vendor_request_id):
            return SimpleNamespace(state="outputs_ready", outputs=outputs)

    async def recover(task):
        return {"outputs": [{"object_key": "original-image"}], "usage": {}, "vendor_request_id": "receipt-1"}

    async def create(task):
        raise AssertionError("archived result must be recovered without another POST")

    result = await execute_generated_media(Store(), object(), "worker", create, recover)
    assert result.state == "outputs_ready"


async def test_rejected_sync_plan_cannot_submit_after_restart():
    from agent_runtime.workflows.media.generated import execute_generated_media

    class Store:
        def get(self, *args):
            return SimpleNamespace(state="prepared", review={"approved": False, "notes": ["review"]})

    async def create(task):
        raise AssertionError("rejected plan cannot be submitted")

    result = await execute_generated_media(Store(), object(), "worker", create, create)
    assert isinstance(result, WaitingReview) and result["media_status"] == "prepared"


async def test_database_error_after_archive_keeps_same_job_resumable():
    from agent_runtime.workflows.media.generated import execute_generated_media

    class Store:
        def get(self, *args):
            return SimpleNamespace(state="prepared", review={"approved": True})

        def begin_submit(self, *args):
            pass

        def archived(self, *args, **kwargs):
            raise ConnectionError("temporary database interruption")

    async def create(task):
        return {"outputs": [{"object_key": "archived-image"}], "usage": {}}

    result = await execute_generated_media(Store(), object(), "worker", create, create)
    assert isinstance(result, WaitingReview) and result["media_status"] == "archive_pending"


async def test_failure_recording_unknown_outcome_still_keeps_job_resumable():
    from agent_runtime.infrastructure.media.errors import SubmissionUnknown
    from agent_runtime.workflows.media.generated import execute_generated_media

    class Store:
        def get(self, *args):
            return SimpleNamespace(state="prepared", review={"approved": True})

        def begin_submit(self, *args):
            pass

        def mark_unknown(self, *args):
            raise ConnectionError("temporary database interruption")

    async def create(task):
        raise SubmissionUnknown("response lost")

    result = await execute_generated_media(Store(), object(), "worker", create, create)
    assert isinstance(result, WaitingReview) and result["media_status"] == "submission_unknown"


def test_media_source_asset_cannot_cross_projects():
    from agent_runtime.workflows.media.generated import validate_asset_scope

    value = {
        "bucket": "agent-artifacts",
        "object_key": "workspaces/ws/projects/other/assets/doc/versions/v/image.png",
        "sha256": "a" * 64,
        "size_bytes": 12,
        "content_type": "image/png",
        "etag": "tag",
    }
    with pytest.raises(ValueError, match="scope"):
        validate_asset_scope(value, "ws", "project", "agent-artifacts", 100)


@pytest.mark.parametrize(
    "modality,workflow,skill,node,purpose",
    [
        ("music", "media.music@1", "controlled-music@1", "media.music.plan", "music_generation"),
        (
            "effect",
            "media.sound_effect@1",
            "controlled-sound-effect@1",
            "media.sound_effect.plan",
            "sound_effect_generation",
        ),
    ],
)
def test_music_and_effect_workflows_require_their_frozen_contracts(modality, workflow, skill, node, purpose):
    from agent_runtime.workflows.media.generated import validate_generated_snapshot

    snapshot = SimpleNamespace(
        workflow=workflow,
        schema_versions={"content_ir": "1.0"},
        tools=[],
        mcp_policies=[],
        skills=[skill],
    )
    execution = SimpleNamespace(
        resource_versions={},
        prompt_versions={node: object(), "media.review": object(), "memory.refresh": object()},
        model_routes={purpose: {"model_name": "configured"}},
    )
    assert validate_generated_snapshot(snapshot, execution, modality) == (purpose, node)
    execution.model_routes = {}
    with pytest.raises(ValueError, match="route"):
        validate_generated_snapshot(snapshot, execution, modality)


@pytest.mark.parametrize(
    "modality,purpose",
    [("music", "music_generation"), ("effect", "sound_effect_generation")],
)
def test_generated_audio_usage_is_attributed_to_its_independent_route(modality, purpose):
    from agent_runtime.infrastructure.postgres.generated_media import PostgresGeneratedMediaStore

    class Cursor:
        def execute(self, query, values):
            self.values = values

    cursor = Cursor()
    task = SimpleNamespace(
        workspace_id="workspace",
        job_id="job-" + modality,
        modality=modality,
        usage={},
        route_identity={"provider": "elevenlabs", "model_name": "configured", "profile_version_id": "profile-v1"},
        vendor_request_id=None,
        fencing_token=1,
        request=SimpleNamespace(prompt="created audio"),
    )

    PostgresGeneratedMediaStore._record_usage(cursor, task)

    metadata = cursor.values[-1].obj
    assert metadata["purpose"] == purpose


async def test_rustfs_outage_during_ir_publication_preserves_archived_outputs(monkeypatch):
    from botocore.exceptions import EndpointConnectionError

    from agent_runtime.domain.capabilities import CapabilitySnapshot
    from agent_runtime.domain.generated_media import GeneratedMediaTask, StoredMediaRequest
    from agent_runtime.domain.images import ImageRoute
    from agent_runtime.execution.context import _EXECUTION, ExecutionContext
    from agent_runtime.settings import RuntimeSettings
    from agent_runtime.workflows.media.generated import generate_media_asset

    route = ImageRoute(
        provider="openai_compatible",
        model_name="image",
        base_url="https://provider.invalid/v1",
        api_key="fake-key",
        image_capabilities={"generation": True, "editing": False, "sizes": [], "formats": ["png"], "max_images": 1},
    )
    task = GeneratedMediaTask(
        workspace_id="ws",
        project_id="project",
        job_id="job",
        modality="image",
        request=StoredMediaRequest(prompt="already generated"),
        request_hash="a" * 64,
        route_identity=route.identity(),
        state="outputs_ready",
        fencing_token=1,
        outputs=[
            {
                "bucket": "agent-artifacts",
                "object_key": "workspaces/ws/projects/project/assets/doc/versions/v/image.png",
                "sha256": "b" * 64,
                "size_bytes": 8,
                "content_type": "image/png",
                "etag": "tag",
            }
        ],
    )

    class MediaStore:
        def __init__(self, *args):
            pass

        def get(self, *args):
            return task

    class Artifacts:
        def __init__(self, *args):
            pass

        def upload(self, *args, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://rustfs.invalid")

    class Provider:
        async def generate(self, *args, **kwargs):
            raise AssertionError("archived media cannot be generated again")

        async def aclose(self):
            pass

    monkeypatch.setattr("agent_runtime.workflows.media.generated.PostgresGeneratedMediaStore", MediaStore)
    monkeypatch.setattr("agent_runtime.workflows.media.generated.S3ArtifactStore", Artifacts)
    monkeypatch.setattr("agent_runtime.infrastructure.media.images.create_image_provider", lambda route: Provider())
    execution = ExecutionContext(
        SimpleNamespace(fencing_token=1, resume_input={}),
        object(),
        RuntimeSettings(_env_file=None),
        model_routes={"image_generation": route.model_dump(mode="json")},
        prompt_versions={node: object() for node in ["media.image.plan", "media.review", "memory.refresh"]},
    )
    snapshot = CapabilitySnapshot(
        workflow="media.image@1", skills=["controlled-image@1"], schema_versions={"content_ir": "1.0"}
    )
    command = SimpleNamespace(workspace_id="ws", project_id="project", job_id="job")
    bundle = {"request": {"document_id": "doc", "version": 1, "title": "Existing image"}}
    token = _EXECUTION.set(execution)
    try:
        result = await generate_media_asset(command, bundle, snapshot)
    finally:
        _EXECUTION.reset(token)
    assert isinstance(result, WaitingReview) and result["media_status"] == "infrastructure_unavailable"
