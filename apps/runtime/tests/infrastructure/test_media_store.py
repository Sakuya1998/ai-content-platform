"""Media recovery and fencing tested against a migrated disposable PostgreSQL."""

import os

import pytest

from agent_runtime.domain.media import VideoRequest, VideoTaskStatus
from agent_runtime.execution.leases import FencingTokenError
from agent_runtime.infrastructure.postgres.media import PostgresMediaTaskStore
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from tests.infrastructure.test_mq_store import command


@pytest.fixture
def stores():
    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    jobs = RuntimeMQStore(url)
    media = PostgresMediaTaskStore(jobs)
    yield jobs, media
    media.close()
    assert jobs.ready()
    jobs.close()


def claimed(jobs):
    task = command()
    jobs.accept_task(task)
    return jobs.claim_next(task.workspace_id, "runtime", "worker", 60)


ROUTE = {
    "provider": "volcengine",
    "model_name": "seedance-version",
    "base_url": "https://ark.example/api/v3",
    "profile_version_id": "frozen-1",
    "video_capabilities": {"text_to_video": True},
}


def test_media_reservation_is_idempotent_and_rejects_changed_request(stores):
    jobs, media = stores
    job = claimed(jobs)
    request = VideoRequest(prompt="original shot", parameters={"duration": 5})
    first = media.reserve(job, "worker", request, ROUTE)
    again = media.reserve(job, "worker", request, ROUTE)
    assert first.request_hash == again.request_hash and first.state == "prepared"
    with pytest.raises(ValueError, match="conflict"):
        media.reserve(job, "worker", VideoRequest(prompt="replanned shot"), ROUTE)
    with pytest.raises(ValueError, match="conflict"):
        media.reserve(job, "worker", request, {**ROUTE, "model_name": "new-model"})
    with pytest.raises(ValueError, match="credential"):
        media.reserve(job, "worker", request, {**ROUTE, "api_key": "secret"})


def test_media_commits_before_submit_and_recovery_never_repeats_create(stores):
    jobs, media = stores
    job = claimed(jobs)
    media.reserve(job, "worker", VideoRequest(prompt="original shot"), ROUTE, review={"approved": True})
    media.begin_submit(job, "worker")
    # A separate connection observes the marker before any provider HTTP call.
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT state FROM media_tasks WHERE job_id=%s", (job.command.job_id,))
        assert cursor.fetchone()[0] == "submitting"
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job.command.job_id,),
        )
    recovered = jobs.claim_next(job.command.workspace_id, "runtime", "replacement", 60)
    persisted = media.get(recovered, "replacement")
    assert persisted.state == "submission_unknown"
    assert persisted.request.prompt == "original shot"
    with pytest.raises(ValueError, match="prepared"):
        media.begin_submit(recovered, "replacement")
    with pytest.raises(FencingTokenError):
        media.submitted(job, "worker", "late-remote")


def test_media_remote_id_survives_restart_and_completed_result_is_fenced(stores):
    jobs, media = stores
    job = claimed(jobs)
    media.reserve(job, "worker", VideoRequest(prompt="original shot"), ROUTE, review={"approved": True})
    media.begin_submit(job, "worker")
    media.submitted(job, "worker", "remote-1")
    with pytest.raises(ValueError, match="conflict"):
        media.submitted(job, "worker", "remote-2")
    jobs.finish(job, "worker", "accepted", {}, retry_seconds=0)
    recovered = jobs.claim_next(job.command.workspace_id, "runtime", "replacement", 60)
    persisted = media.get(recovered, "replacement")
    assert persisted.state == "running" and persisted.remote_task_id == "remote-1"
    complete = VideoTaskStatus(
        state="succeeded", content_url="https://media.example/result.mp4?secret=token", usage={"total_tokens": 42}
    )
    with pytest.raises(FencingTokenError):
        media.update_status(job, "worker", complete)
    result = media.update_status(recovered, "replacement", complete)
    assert result.usage == {"total_tokens": 42}
    assert result.content_url.endswith("secret=token")
    assert "secret=token" not in repr(result)
    with pytest.raises(ValueError, match="transition"):
        media.update_status(recovered, "replacement", VideoTaskStatus(state="running"))


def test_media_reads_and_mutations_require_matching_project_and_live_lease(stores):
    from dataclasses import replace

    jobs, media = stores
    job = claimed(jobs)
    media.reserve(job, "worker", VideoRequest(prompt="shot"), ROUTE)
    wrong_project = replace(job, command=job.command.model_copy(update={"project_id": "other-project"}))
    wrong_workspace = replace(job, command=job.command.model_copy(update={"workspace_id": "other-workspace"}))
    for forged in [wrong_project, wrong_workspace]:
        with pytest.raises(FencingTokenError):
            media.get(forged, "worker")
    with jobs.scope("other-workspace") as cursor:
        cursor.execute("SELECT count(*) FROM media_tasks")
        assert cursor.fetchone()[0] == 0
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT set_config('app.project_id','other-project',true)")
        cursor.execute("SELECT count(*) FROM media_tasks")
        assert cursor.fetchone()[0] == 0
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job.command.job_id,),
        )
    with pytest.raises(FencingTokenError):
        media.begin_submit(job, "worker")


def test_media_cancel_marker_preserves_remote_id_and_unknown_submit_is_terminal(stores):
    jobs, media = stores
    job = claimed(jobs)
    media.reserve(job, "worker", VideoRequest(prompt="shot"), ROUTE, review={"approved": True})
    media.begin_submit(job, "worker")
    media.mark_unknown(job, "worker")
    with pytest.raises(ValueError, match="prepared"):
        media.begin_submit(job, "worker")
    other = claimed(jobs)
    media.reserve(other, "worker", VideoRequest(prompt="shot"), ROUTE, review={"approved": True})
    media.begin_submit(other, "worker")
    media.submitted(other, "worker", "remote-cancel")
    jobs.accept_control(command("job.cancel", job_id=other.command.job_id, workspace_id=other.command.workspace_id))
    pending = media.begin_cancel(other, "worker")
    assert pending.state == "cancel_pending" and pending.remote_task_id == "remote-cancel"
    assert media.mark_cancelled(other, "worker").state == "cancelled"


def test_media_database_refuses_mutating_immutable_request(stores):
    import psycopg

    jobs, media = stores
    job = claimed(jobs)
    media.reserve(job, "worker", VideoRequest(prompt="shot"), ROUTE)
    with pytest.raises(psycopg.Error, match="immutable"), jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute("UPDATE media_tasks SET request_hash=%s WHERE job_id=%s", ("a" * 64, job.command.job_id))


def test_media_review_remains_rejected_after_restart_until_explicit_approval(stores):
    jobs, media = stores
    job = claimed(jobs)
    request = VideoRequest(prompt="needs review")
    media.reserve(job, "worker", request, ROUTE, review={"approved": False, "notes": ["manual review"]})
    with pytest.raises(ValueError, match="approved"):
        media.begin_submit(job, "worker")
    jobs.finish(job, "worker", "accepted", {}, retry_seconds=0)
    resumed = jobs.claim_next(job.command.workspace_id, "runtime", "replacement", 60)
    task = media.get(resumed, "replacement")
    assert task.review == {"approved": False, "notes": ["manual review"]}
    with pytest.raises(FencingTokenError):
        media.approve_plan(job, "worker")
    approved = media.approve_plan(resumed, "replacement")
    assert approved.review["approved"] is True
    assert approved.request.prompt == "needs review"
    assert media.begin_submit(resumed, "replacement").state == "submitting"


def test_media_definite_provider_rejection_fails_without_remote_id(stores):
    jobs, media = stores
    job = claimed(jobs)
    media.reserve(job, "worker", VideoRequest(prompt="shot"), ROUTE, review={"approved": True})
    media.begin_submit(job, "worker")
    failed = media.update_status(job, "worker", VideoTaskStatus(state="failed", error={"code": "Rejected"}))
    assert failed.state == "failed" and failed.remote_task_id is None
    with pytest.raises(ValueError, match="prepared"):
        media.begin_submit(job, "worker")
