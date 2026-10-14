"""Synchronous media cancellation records truthful outcomes without provider DELETE."""

import pytest

from tests.infrastructure.test_generated_media_store import artifact, media_stores, reserve
from tests.infrastructure.test_mq_store import command

# Export the fixture imported from the behavior suite for pytest discovery.
__all__ = ["media_stores"]

CASES = [
    ("prepared", "cancelled", False),
    ("submitting", "submission_unknown", False),
    ("submission_unknown", "submission_unknown", False),
    ("outputs_ready", "outputs_ready", True),
    ("failed", "failed", True),
]


def generated_job(stores, state):
    _, media = stores
    job = reserve(stores)
    if state == "prepared":
        return job
    media.begin_submit(job, "worker")
    if state == "submission_unknown":
        media.mark_unknown(job, "worker")
    elif state == "outputs_ready":
        media.archived(job, "worker", [artifact(job)], {}, vendor_request_id="receipt-1")
    elif state == "failed":
        media.rejected(job, "worker")
    return job


def expected_result(state, terminal):
    return {
        "media_status": state,
        "provider_request_id": "receipt-1" if state == "outputs_ready" else None,
        "remote_cancel_confirmed": False,
        "remote_work_terminal": terminal or state == "cancelled",
    }


def assert_cancelled(jobs, job, state, terminal):
    expected = expected_result(state, terminal)
    actual = jobs.get_scoped(job.command.job_id, job.command.workspace_id)
    assert actual.status == "cancelled"
    assert actual.result == expected
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT state FROM generated_media_tasks WHERE job_id=%s", (job.command.job_id,))
        assert cursor.fetchone()[0] == state
        cursor.execute(
            "SELECT payload FROM runtime_event_outbox WHERE job_id=%s AND event_type='job.cancelled'",
            (job.command.job_id,),
        )
        events = cursor.fetchall()
    assert len(events) == 1
    assert events[0][0]["payload"] == expected
    assert "A station" not in str(events)
    assert "object_key" not in str(events)
    assert "remote_task_id" not in actual.result


@pytest.mark.parametrize("local_status", ["accepted", "waiting_review"])
@pytest.mark.parametrize("state,expected_state,terminal", CASES)
def test_cancel_idle_generated_media_preserves_actual_provider_outcome(
    media_stores, local_status, state, expected_state, terminal
):
    jobs, _ = media_stores
    job = generated_job(media_stores, state)
    jobs.finish(job, "worker", local_status, {"checkpoint_id": "review"})
    cancel = command("job.cancel", job_id=job.command.job_id, workspace_id=job.command.workspace_id)
    jobs.accept_control(cancel)
    jobs.accept_control(cancel)
    assert_cancelled(jobs, job, expected_state, terminal)


@pytest.mark.parametrize("state,expected_state,terminal", CASES)
def test_worker_finish_after_cancel_reports_stored_synchronous_outcome(media_stores, state, expected_state, terminal):
    jobs, _ = media_stores
    job = generated_job(media_stores, state)
    jobs.accept_control(command("job.cancel", job_id=job.command.job_id, workspace_id=job.command.workspace_id))
    requested = jobs.get_scoped(job.command.job_id, job.command.workspace_id)
    assert requested.status == "cancel_requested"
    assert requested.result == expected_result(expected_state, terminal)
    jobs.finish(job, "worker", "completed", {"url": "https://private.example/?token=secret"})
    assert_cancelled(jobs, job, expected_state, terminal)


@pytest.mark.parametrize("state,expected_state,terminal", CASES)
def test_expired_cancelled_sync_work_remains_unknown_until_archived(media_stores, state, expected_state, terminal):
    jobs, _ = media_stores
    job = generated_job(media_stores, state)
    jobs.accept_control(command("job.cancel", job_id=job.command.job_id, workspace_id=job.command.workspace_id))
    with jobs.scope(job.command.workspace_id) as cursor:
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job.command.job_id,),
        )
    assert jobs.claim_next(job.command.workspace_id, "runtime", "replacement", 60) is None
    assert_cancelled(jobs, job, expected_state, terminal)
