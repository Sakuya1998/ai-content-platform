import os

import pytest

from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from tests.infrastructure.test_mq_store import command


@pytest.fixture
def stores():
    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    from agent_runtime.infrastructure.postgres.composition import CompositionStore

    jobs = RuntimeMQStore(url)
    yield jobs, CompositionStore(jobs)
    jobs.close()


def claimed(stores):
    jobs, runs = stores
    task = command()
    jobs.accept_task(task)
    job = jobs.claim_next(task.workspace_id, "runtime", "worker", 60)
    return job


def test_render_request_is_immutable_and_recovery_allows_local_rerender(stores):
    jobs, runs = stores
    job = claimed(stores)
    first = runs.reserve(job, "worker", {"timeline": "frozen", "inputs": ["v1"]})
    assert first["state"] == "prepared"
    runs.begin(job, "worker")
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (job.command.job_id,)
        )
    recovered = jobs.claim_next(job.command.workspace_id, "runtime", "new-worker", 60)
    assert runs.get(recovered, "new-worker")["state"] == "prepared"
    with pytest.raises(ValueError, match="conflict"):
        runs.reserve(recovered, "new-worker", {"timeline": "changed"})
    assert runs.begin(recovered, "new-worker")["state"] == "rendering"


def test_archived_video_is_immutable_and_old_worker_cannot_commit(stores):
    from agent_runtime.execution.leases import FencingTokenError

    jobs, runs = stores
    job = claimed(stores)
    runs.reserve(job, "worker", {"timeline": "frozen"})
    runs.begin(job, "worker")
    artifact = {
        "bucket": "agent-artifacts",
        "object_key": f"workspaces/{job.command.workspace_id}/projects/{job.command.project_id}/"
        "assets/doc/versions/v/video.mp4",
        "sha256": "a" * 64,
        "size_bytes": 12,
        "content_type": "video/mp4",
        "etag": "tag",
    }
    result = runs.archived(job, "worker", artifact, {"duration_ms": 1000})
    assert result["state"] == "outputs_ready"
    with pytest.raises(ValueError, match="conflict"):
        runs.archived(job, "worker", {**artifact, "sha256": "b" * 64}, {})
    with pytest.raises(FencingTokenError):
        runs.archived(job, "wrong-worker", artifact, {})


def test_local_cancellation_does_not_claim_process_already_stopped(stores):
    jobs, runs = stores
    job = claimed(stores)
    runs.reserve(job, "worker", {"timeline": "frozen"})
    runs.begin(job, "worker")
    cancel = command("job.cancel", job_id=job.command.job_id, workspace_id=job.command.workspace_id)
    jobs.accept_control(cancel)
    assert runs.get(job, "worker")["state"] == "cancel_requested"
    runs.cancelled(job, "worker")
    jobs.finish(job, "worker", "cancelled", {})
    result = jobs.get_scoped(job.command.job_id, job.command.workspace_id).result
    assert result["local_render_terminal"] and result["media_status"] == "cancelled"
