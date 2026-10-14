"""Local job cancellation preserves truthful, secret-free remote media status."""

import os

import pytest

from agent_runtime.domain.media import VideoRequest, VideoTaskStatus
from agent_runtime.infrastructure.postgres.media import PostgresMediaTaskStore
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from tests.infrastructure.test_mq_store import command


@pytest.fixture
def cancellation_stores():
    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    jobs = RuntimeMQStore(url)
    yield jobs, PostgresMediaTaskStore(jobs)
    jobs.close()


def video_job(jobs, media, state):
    task = command()
    jobs.accept_task(task)
    job = jobs.claim_next(task.workspace_id, "runtime", "worker", 60)
    media.reserve(
        job,
        "worker",
        VideoRequest(prompt="private manuscript"),
        {"provider": "volcengine", "model_name": "frozen", "base_url": "https://ark.example/api/v3"},
        review={"approved": True},
    )
    if state == "prepared":
        return job
    media.begin_submit(job, "worker")
    if state == "submitting":
        return job
    if state == "submission_unknown":
        media.mark_unknown(job, "worker")
        return job
    media.submitted(job, "worker", "remote-task")
    if state == "cancel_pending":
        media.begin_cancel(job, "worker")
    elif state != "running":
        media.update_status(
            job,
            "worker",
            VideoTaskStatus(
                state=state,
                content_url="https://media.example/video?token=private" if state == "succeeded" else None,
            ),
        )
    return job


CASES = [
    ("prepared", "cancelled", None, False, True),
    ("submitting", "submission_unknown", None, False, False),
    ("submission_unknown", "submission_unknown", None, False, False),
    ("running", "cancel_pending", "remote-task", False, False),
    ("cancel_pending", "cancel_pending", "remote-task", False, False),
    ("cancelled", "cancelled", "remote-task", True, True),
    ("succeeded", "succeeded", "remote-task", False, True),
    ("failed", "failed", "remote-task", False, True),
]


def assert_cancelled(jobs, job, expected_state, remote_id, confirmed, terminal):
    expected = {
        "media_status": expected_state,
        "remote_task_id": remote_id,
        "remote_cancel_confirmed": confirmed,
        "remote_work_terminal": terminal,
    }
    actual = jobs.get_scoped(job.command.job_id, job.command.workspace_id)
    assert actual.status == "cancelled" and actual.result == expected
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT state FROM media_tasks WHERE job_id=%s", (job.command.job_id,))
        assert cursor.fetchone()[0] == expected_state
        cursor.execute(
            "SELECT payload FROM runtime_event_outbox WHERE job_id=%s AND event_type='job.cancelled'",
            (job.command.job_id,),
        )
        events = cursor.fetchall()
    assert len(events) == 1 and events[0][0]["payload"] == expected
    assert "private" not in str(actual.result) + str(events)


@pytest.mark.parametrize("local_status", ["accepted", "waiting_review"])
@pytest.mark.parametrize("state,expected_state,remote_id,confirmed,terminal", CASES)
def test_cancel_idle_media_reports_remote_generation_without_claiming_it_stopped(
    cancellation_stores, local_status, state, expected_state, remote_id, confirmed, terminal
):
    jobs, media = cancellation_stores
    job = video_job(jobs, media, state)
    jobs.finish(job, "worker", local_status, {"checkpoint_id": "review"})
    cancel = command("job.cancel", job_id=job.command.job_id, workspace_id=job.command.workspace_id)
    jobs.accept_control(cancel)
    jobs.accept_control(cancel)
    assert_cancelled(jobs, job, expected_state, remote_id, confirmed, terminal)


@pytest.mark.parametrize("state,expected_state,remote_id,confirmed,terminal", CASES)
def test_expired_cancel_requested_media_preserves_unknown_or_pending_remote_work(
    cancellation_stores, state, expected_state, remote_id, confirmed, terminal
):
    jobs, media = cancellation_stores
    job = video_job(jobs, media, state)
    jobs.accept_control(command("job.cancel", job_id=job.command.job_id, workspace_id=job.command.workspace_id))
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job.command.job_id,),
        )
    assert jobs.claim_next(job.command.workspace_id, "runtime", "replacement", 60) is None
    assert_cancelled(jobs, job, expected_state, remote_id, confirmed, terminal)


@pytest.mark.parametrize("state,expected_state,remote_id,confirmed,terminal", CASES)
def test_late_worker_completion_after_cancel_uses_persisted_remote_media_outcome(
    cancellation_stores, state, expected_state, remote_id, confirmed, terminal
):
    jobs, media = cancellation_stores
    job = video_job(jobs, media, state)
    jobs.accept_control(command("job.cancel", job_id=job.command.job_id, workspace_id=job.command.workspace_id))
    jobs.finish(job, "worker", "completed", {"content_url": "https://media.example/?token=private"})
    assert_cancelled(jobs, job, expected_state, remote_id, confirmed, terminal)


def test_active_worker_confirmed_provider_cancel_overrides_earlier_pending_event(cancellation_stores):
    jobs, media = cancellation_stores
    job = video_job(jobs, media, "running")
    jobs.accept_control(command("job.cancel", job_id=job.command.job_id, workspace_id=job.command.workspace_id))
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute(
            "SELECT payload FROM runtime_event_outbox WHERE job_id=%s AND event_type='job.cancel_requested'",
            (job.command.job_id,),
        )
        pending = cursor.fetchone()[0]["payload"]
    assert pending["media_status"] == "cancel_pending" and pending["remote_cancel_confirmed"] is False
    # The provider adapter confirms cancellation before the worker finishes.
    media.mark_cancelled(job, "worker")
    jobs.finish(job, "worker", "cancelled", {"untrusted": "private"})
    assert_cancelled(jobs, job, "cancelled", "remote-task", True, True)
