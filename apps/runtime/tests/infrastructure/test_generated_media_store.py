import os
from dataclasses import replace

import pytest

from agent_runtime.domain.generated_media import StoredMediaRequest
from agent_runtime.execution.leases import FencingTokenError
from agent_runtime.infrastructure.postgres.generated_media import PostgresGeneratedMediaStore
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from tests.infrastructure.test_mq_store import command


@pytest.fixture
def media_stores():
    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    jobs = RuntimeMQStore(url)
    yield jobs, PostgresGeneratedMediaStore(jobs)
    jobs.close()


def reserve(stores, modality="image"):
    from agent_runtime.domain.generated_media import StoredMediaRequest

    jobs, media = stores
    task = command()
    jobs.accept_task(task)
    job = jobs.claim_next(task.workspace_id, "runtime", "worker", 60)
    request = StoredMediaRequest(prompt="A station", parameters={"format": "png"})
    identity = {"provider": "openai_compatible", "model_name": "image-test", "profile_version_id": "frozen"}
    media.reserve(job, "worker", modality, request, identity, review={"approved": True})
    return job


def test_sync_submission_marker_survives_restart_without_replay(media_stores):
    jobs, media = media_stores
    job = reserve(media_stores)
    media.begin_submit(job, "worker")
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (job.command.job_id,)
        )
    recovered = jobs.claim_next(job.command.workspace_id, "runtime", "replacement", 60)
    assert media.get(recovered, "replacement").state == "submission_unknown"
    with pytest.raises(ValueError, match="prepared"):
        media.begin_submit(recovered, "replacement")


def test_archived_result_and_usage_are_idempotent_and_preserve_frozen_request(media_stores):
    jobs, media = media_stores
    job = reserve(media_stores)
    media.begin_submit(job, "worker")
    outputs = [
        {
            "bucket": "agent-artifacts",
            "object_key": (
                f"workspaces/{job.command.workspace_id}/projects/{job.command.project_id}"
                "/assets/image/versions/v1/image.png"
            ),
            "sha256": "a" * 64,
            "size_bytes": 10,
            "content_type": "image/png",
            "etag": "tag",
        }
    ]
    first = media.archived(job, "worker", outputs, {"input_tokens": 4}, vendor_request_id="request-1")
    again = media.archived(job, "worker", outputs, {"input_tokens": 4}, vendor_request_id="request-1")
    assert first.state == again.state == "outputs_ready"
    assert first.request.prompt == "A station"
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT count(*),sum(input_tokens) FROM model_calls WHERE job_id=%s", (job.command.job_id,))
        assert cursor.fetchone() == (1, 4)
    with pytest.raises(ValueError, match="conflict"):
        media.archived(job, "worker", [{**outputs[0], "sha256": "b" * 64}], {})


def test_failed_review_cannot_submit_and_other_project_cannot_read(media_stores):
    from dataclasses import replace

    from agent_runtime.domain.generated_media import StoredMediaRequest
    from agent_runtime.execution.leases import FencingTokenError

    jobs, media = media_stores
    task = command()
    jobs.accept_task(task)
    job = jobs.claim_next(task.workspace_id, "runtime", "worker", 60)
    media.reserve(
        job,
        "worker",
        "audio",
        StoredMediaRequest(prompt="Hello"),
        {"provider": "openai", "model_name": "speech"},
        review={"approved": False},
    )
    with pytest.raises(ValueError, match="approved"):
        media.begin_submit(job, "worker")
    forged = replace(job, command=job.command.model_copy(update={"project_id": "other-project"}))
    with pytest.raises(FencingTokenError):
        media.get(forged, "worker")
    media.approve_plan(job, "worker")
    assert media.begin_submit(job, "worker").state == "submitting"


def artifact(job, **changes):
    return {
        "bucket": "agent-artifacts",
        "object_key": (
            f"workspaces/{job.command.workspace_id}/projects/{job.command.project_id}"
            "/assets/image/versions/v1/image.png"
        ),
        "sha256": "a" * 64,
        "size_bytes": 10,
        "content_type": "image/png",
        "etag": "tag",
        **changes,
    }


def test_reservation_rechecks_lease_at_insert_and_rolls_back_when_expired(media_stores, monkeypatch):
    jobs, media = media_stores
    task = command()
    jobs.accept_task(task)
    job = jobs.claim_next(task.workspace_id, "runtime", "worker", 60)
    original_load = media._load

    def expire_before_insert(cursor, job):
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job.command.job_id,),
        )
        return original_load(cursor, job)

    monkeypatch.setattr(media, "_load", expire_before_insert)
    with pytest.raises(FencingTokenError):
        media.reserve(
            job,
            "worker",
            "image",
            StoredMediaRequest(prompt="image"),
            {"provider": "openai", "model_name": "image"},
            review={"approved": True},
        )
    with jobs.scope(task.workspace_id) as cursor:
        cursor.execute("SELECT count(*) FROM generated_media_tasks WHERE job_id=%s", (task.job_id,))
        assert cursor.fetchone()[0] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "Changed"},
        {"parameters": {"format": "webp"}},
        {"source_version_id": "changed"},
    ],
)
def test_reservation_hash_rejects_changed_frozen_request(media_stores, change):
    _, media = media_stores
    job = reserve(media_stores)
    with pytest.raises(ValueError, match="conflict"):
        media.reserve(
            job,
            "worker",
            "image",
            StoredMediaRequest(**{"prompt": "A station", "parameters": {"format": "png"}, **change}),
            {"provider": "openai_compatible", "model_name": "image-test", "profile_version_id": "frozen"},
            review={"approved": True},
        )


def test_reservation_hash_rejects_changed_route_or_modality(media_stores):
    _, media = media_stores
    job = reserve(media_stores)
    request = StoredMediaRequest(prompt="A station", parameters={"format": "png"})
    for modality, identity in [
        ("audio", {"provider": "openai_compatible", "model_name": "image-test", "profile_version_id": "frozen"}),
        ("image", {"provider": "openai_compatible", "model_name": "changed", "profile_version_id": "frozen"}),
    ]:
        with pytest.raises(ValueError, match="conflict"):
            media.reserve(job, "worker", modality, request, identity, review={"approved": True})


@pytest.mark.parametrize(
    "changes",
    [
        {"object_key": "workspaces/other/projects/project-test/assets/image/versions/v1/image.png"},
        {"object_key": "workspaces/{workspace}/projects/other/assets/image/versions/v1/image.png"},
        {"object_key": "workspaces/{workspace}/projects/project-test/assets/../versions/v1/image.png"},
        {"object_key": "workspaces/{workspace}/projects/project-test/assets/image/versions/v1/image.png?token=secret"},
        {"size_bytes": True},
        {"size_bytes": "10"},
        {"content_type": "audio/mpeg"},
        {"bucket": " "},
        {"sha256": "invalid"},
    ],
)
def test_archive_rejects_unscoped_or_untyped_assets(media_stores, changes):
    _, media = media_stores
    job = reserve(media_stores)
    media.begin_submit(job, "worker")
    if "object_key" in changes:
        changes = {**changes, "object_key": changes["object_key"].replace("{workspace}", job.command.workspace_id)}
    with pytest.raises(ValueError):
        media.archived(job, "worker", [artifact(job, **changes)], {})
    assert media.get(job, "worker").state == "submitting"


def test_unknown_submission_can_archive_without_second_submit(media_stores):
    _, media = media_stores
    job = reserve(media_stores)
    media.begin_submit(job, "worker")
    media.mark_unknown(job, "worker")
    assert media.archived(job, "worker", [artifact(job)], {}, vendor_request_id="req-1").state == "outputs_ready"
    with pytest.raises(ValueError, match="prepared"):
        media.begin_submit(job, "worker")


@pytest.mark.parametrize("usage,request_id", [({"input_tokens": 5}, "req-1"), ({"input_tokens": 4}, "req-2")])
def test_repeated_archive_rejects_changed_usage_or_provider_receipt(media_stores, usage, request_id):
    jobs, media = media_stores
    job = reserve(media_stores)
    media.begin_submit(job, "worker")
    media.archived(job, "worker", [artifact(job)], {"input_tokens": 4}, vendor_request_id="req-1")
    with pytest.raises(ValueError, match="conflict"):
        media.archived(job, "worker", [artifact(job)], usage, vendor_request_id=request_id)
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT count(*),sum(input_tokens) FROM model_calls WHERE job_id=%s", (job.command.job_id,))
        assert cursor.fetchone() == (1, 4)


def test_archive_sanitizes_usage_to_finite_bounded_known_metrics(media_stores):
    jobs, media = media_stores
    job = reserve(media_stores)
    media.begin_submit(job, "worker")
    result = media.archived(
        job,
        "worker",
        [artifact(job)],
        {
            "input_tokens": 4,
            "output_tokens": True,
            "total_tokens": float("inf"),
            "text_words": 2,
            "image_tokens": float("nan"),
            "images": -1,
            "prompt_tokens": 2**40,
            "private-key": 7,
            "message": "private manuscript",
            "input_tokens_details": {"text_tokens": 4, "cached_tokens": 0, "private-key": 123},
        },
    )
    expected = {"input_tokens": 4, "text_words": 2, "input_tokens_details": {"text_tokens": 4, "cached_tokens": 0}}
    assert result.usage == expected
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute(
            "SELECT input_tokens,output_tokens,metadata FROM model_calls WHERE job_id=%s", (job.command.job_id,)
        )
        row = cursor.fetchone()
    assert row[:2] == (4, 0)
    assert row[2]["raw_usage"] == expected
    assert "private" not in str(row)


def test_generated_media_rls_hides_other_workspace_and_project(media_stores):
    jobs, media = media_stores
    job = reserve(media_stores)
    with jobs.scope("other-workspace") as cursor:
        cursor.execute("SELECT count(*) FROM generated_media_tasks WHERE job_id=%s", (job.command.job_id,))
        assert cursor.fetchone()[0] == 0
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT set_config('app.project_id','other-project',true)")
        cursor.execute("SELECT count(*) FROM generated_media_tasks WHERE job_id=%s", (job.command.job_id,))
        assert cursor.fetchone()[0] == 0
    forged = replace(job, fencing_token=job.fencing_token + 1)
    with pytest.raises(FencingTokenError):
        media.archived(forged, "worker", [artifact(job)], {})


def test_generated_media_archive_remains_immutable_at_database_boundary(media_stores):
    import psycopg

    jobs, media = media_stores
    job = reserve(media_stores)
    media.begin_submit(job, "worker")
    media.archived(job, "worker", [artifact(job)], {"input_tokens": 4}, vendor_request_id="req-1")
    with (
        pytest.raises(psycopg.errors.RaiseException, match="immutable"),
        jobs.scope(job.command.workspace_id) as cursor,
    ):
        cursor.execute("UPDATE generated_media_tasks SET usage='{}' WHERE job_id=%s", (job.command.job_id,))
