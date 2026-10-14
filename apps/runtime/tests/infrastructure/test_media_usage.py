"""Provider terminal usage participates in scoped usage and trace reporting."""

import hashlib
import os

import pytest

from agent_runtime.domain.media import VideoRequest, VideoTaskStatus
from agent_runtime.infrastructure.postgres.knowledge import KnowledgeOperations
from agent_runtime.infrastructure.postgres.media import PostgresMediaTaskStore
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from tests.infrastructure.test_mq_store import command


@pytest.fixture
def usage_stores():
    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    jobs = RuntimeMQStore(url)
    yield jobs, PostgresMediaTaskStore(jobs), KnowledgeOperations(jobs)
    jobs.close()


def running_video(jobs, media):
    task = command()
    jobs.accept_task(task)
    job = jobs.claim_next(task.workspace_id, "runtime", "worker", 60)
    media.reserve(
        job,
        "worker",
        VideoRequest(prompt="private manuscript"),
        {
            "provider": "volcengine",
            "model_name": "frozen-video",
            "base_url": "https://ark.example/api/v3",
            "profile_version_id": "profile-frozen",
        },
        review={"approved": True},
    )
    media.begin_submit(job, "worker")
    media.submitted(job, "worker", "remote-task")
    return job


@pytest.mark.parametrize(
    "state,model_status", [("succeeded", "completed"), ("failed", "failed"), ("cancelled", "cancelled")]
)
def test_video_terminal_usage_is_recorded_once_across_repoll_and_restart(usage_stores, state, model_status):
    jobs, media, reporting = usage_stores
    job = running_video(jobs, media)
    status = VideoTaskStatus(
        state=state,
        content_url="https://media.example/video?token=private" if state == "succeeded" else None,
        usage={"prompt_tokens": 12, "completion_tokens": 24, "total_tokens": 36},
    )
    media.update_status(job, "worker", status)
    media.update_status(job, "worker", status)
    jobs.finish(job, "worker", "accepted", {}, retry_seconds=0)
    resumed = jobs.claim_next(job.command.workspace_id, "runtime", "replacement", 60)
    media.update_status(resumed, "replacement", status)
    usage = reporting.usage(job.command.workspace_id, job.command.project_id)
    assert usage["calls"] == 1 and usage["input_tokens"] == 12 and usage["output_tokens"] == 24
    assert float(usage["cost"]) == 0
    traces = reporting.traces(job.command.workspace_id, job.command.project_id)
    assert len(traces) == 1
    trace = traces[0]
    assert trace["provider"] == "volcengine" and trace["model"] == "frozen-video"
    assert trace["status"] == model_status
    assert trace["prompt_hash"] == hashlib.sha256(b"private manuscript").hexdigest()
    assert trace["metadata"] == {
        "purpose": "video_generation",
        "modality": "video",
        "profile_version_id": "profile-frozen",
        "remote_task_id": "remote-task",
        "raw_usage": {"prompt_tokens": 12, "completion_tokens": 24, "total_tokens": 36},
        "fencing_token": job.fencing_token,
    }
    assert "private" not in str(trace)
    assert reporting.traces("other-workspace", job.command.project_id) == []
    assert reporting.traces(job.command.workspace_id, "other-project") == []


@pytest.mark.parametrize(
    "usage,inputs,outputs",
    [
        ({"input_tokens": 0, "prompt_tokens": 12, "output_tokens": 0, "completion_tokens": 24}, 0, 0),
        ({"input_tokens": True, "prompt_tokens": 12, "output_tokens": -1, "completion_tokens": 24}, 12, 24),
        ({"input_tokens": "12", "completion_tokens": False}, 0, 0),
    ],
)
def test_video_usage_rejects_invalid_token_counts_and_keeps_explicit_zero(usage_stores, usage, inputs, outputs):
    jobs, media, reporting = usage_stores
    job = running_video(jobs, media)
    media.update_status(job, "worker", VideoTaskStatus(state="failed", usage=usage))
    trace = reporting.traces(job.command.workspace_id, job.command.project_id)[0]
    assert trace["input_tokens"] == inputs and trace["output_tokens"] == outputs


def test_video_usage_metadata_excludes_provider_response_secrets_and_preterminal_calls(usage_stores):
    jobs, media, reporting = usage_stores
    job = running_video(jobs, media)
    media.update_status(job, "worker", VideoTaskStatus(state="running", usage={"total_tokens": 4}))
    assert reporting.usage(job.command.workspace_id, job.command.project_id)["calls"] == 0
    media.update_status(
        job,
        "worker",
        VideoTaskStatus(
            state="failed",
            usage={
                "completion_tokens": 24,
                "api_key": "private",
                "prompt": "private",
                "content_url": "https://media/?key=private",
            },
        ),
    )
    trace = reporting.traces(job.command.workspace_id, job.command.project_id)[0]
    assert trace["metadata"]["raw_usage"] == {"completion_tokens": 24}
    assert "private" not in str(trace)


def test_confirmed_provider_delete_records_terminal_usage_once(usage_stores):
    jobs, media, reporting = usage_stores
    job = running_video(jobs, media)
    media.update_status(job, "worker", VideoTaskStatus(state="running", usage={"completion_tokens": 24}))
    media.begin_cancel(job, "worker")
    media.mark_cancelled(job, "worker")
    media.mark_cancelled(job, "worker")
    traces = reporting.traces(job.command.workspace_id, job.command.project_id)
    assert len(traces) == 1 and traces[0]["status"] == "cancelled"
    assert traces[0]["metadata"]["purpose"] == "video_generation"
    assert traces[0]["metadata"]["remote_task_id"] == "remote-task"
    assert traces[0]["output_tokens"] == 24


def test_unsubmitted_local_media_cancel_does_not_record_a_provider_call(usage_stores):
    jobs, media, reporting = usage_stores
    task = command()
    jobs.accept_task(task)
    job = jobs.claim_next(task.workspace_id, "runtime", "worker", 60)
    media.reserve(
        job,
        "worker",
        VideoRequest(prompt="not submitted"),
        {
            "provider": "volcengine",
            "model_name": "frozen-video",
        },
    )
    media.mark_cancelled(job, "worker")
    media.mark_cancelled(job, "worker")
    assert reporting.usage(task.workspace_id, task.project_id)["calls"] == 0
