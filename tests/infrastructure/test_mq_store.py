"""Opt-in PostgreSQL behavior tests, using a migrated disposable Runtime DB."""

import os

import pytest

from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent


@pytest.fixture
def mq_store():
    from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore

    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    store = RuntimeMQStore(url)
    yield store
    store.close()


def command(kind="workflow.run", *, job_id=None, workspace_id=None):
    from uuid import uuid4

    identity = str(uuid4())
    return RuntimeCommand(
        command_id=identity,
        command_type=kind,
        workspace_id=workspace_id or str(uuid4()),
        project_id="project-test",
        job_id=job_id or identity,
        workflow_run_id=identity,
        capability_snapshot_hash="a" * 64,
        trace_id="mq-test",
        idempotency_key=identity,
    )


def test_cancel_before_task_prevents_execution_and_replay_is_idempotent(mq_store):
    task = command()
    cancel = command("job.cancel", job_id=task.job_id, workspace_id=task.workspace_id)
    mq_store.accept_control(cancel)
    mq_store.accept_task(task)
    mq_store.accept_task(task)
    assert mq_store.get_scoped(task.job_id, task.workspace_id).status == "cancelled"
    assert mq_store.claim_next(task.workspace_id, "runtime", "worker", 60) is None
    with pytest.raises(ValueError, match="conflict"):
        mq_store.accept_task(task.model_copy(update={"payload": {"different": True}}))


def test_job_completion_and_event_are_atomic_and_stale_token_is_rejected(mq_store):
    task = command()
    mq_store.accept_task(task)
    job = mq_store.claim_next(task.workspace_id, "runtime", "worker", 60)
    assert job.command.job_id == task.job_id
    with pytest.raises(TypeError):
        mq_store.finish(job, "worker", "completed", {"bad": object()})
    assert mq_store.get_scoped(task.job_id, task.workspace_id).status == "running"
    mq_store.finish(job, "worker", "completed", {"ok": True})
    assert mq_store.get_scoped(task.job_id, task.workspace_id).status == "completed"
    from agent_runtime.execution.leases import FencingTokenError

    with pytest.raises(FencingTokenError):
        mq_store.finish(job, "worker", "failed", {})
    published = []
    mq_store.relay(task.workspace_id, "events", lambda *args: published.append(args), 30)
    events = [RuntimeEvent.model_validate_json(item[3]) for item in published]
    relevant = [event for event in events if event.job_id == task.job_id]
    assert [event.event_type for event in relevant] == ["job.accepted", "job.running", "job.completed"]
    assert [event.sequence for event in relevant] == [1, 2, 3]


def test_memory_scheduler_creates_one_task_per_source_version(mq_store):
    task = command()
    mq_store.accept_task(task)
    event = RuntimeEvent(
        event_id=task.command_id,
        event_type="content.version.committed",
        workspace_id=task.workspace_id,
        project_id=task.project_id,
        job_id=task.job_id,
        workflow_run_id=task.workflow_run_id,
        sequence=4,
        operation_id="content",
        trace_id=task.trace_id,
        payload={"document_id": "document", "document_version_id": task.command_id},
    )
    mq_store.schedule_memory(event, "memory-topic")
    mq_store.schedule_memory(event, "memory-topic")
    sent = []
    mq_store.relay(task.workspace_id, "events", lambda *args: sent.append(args), 30)
    commands = [RuntimeCommand.model_validate_json(item[3]) for item in sent if item[0] == "memory-topic"]
    assert len(commands) == 1
    assert commands[0].command_type == "memory.refresh"
    assert commands[0].payload["document_version_id"] == task.command_id


def test_memory_result_is_committed_with_completion_event(mq_store):
    task = command("memory.refresh")
    task = task.model_copy(update={"payload": {"document_version_id": task.command_id}})
    mq_store.accept_task(task, "memory")
    job = mq_store.claim_next(task.workspace_id, "memory", "worker", 60)
    mq_store.finish(job, "worker", "completed", {"nodes": [{"memory_type": "summary", "content": "test summary"}]})
    assert mq_store.get_scoped(task.job_id, task.workspace_id).status == "completed"
    with mq_store.scope(task.workspace_id) as cursor:
        cursor.execute("SELECT content,document_version_id FROM memory_nodes WHERE project_id=%s", (task.project_id,))
        row = cursor.fetchone()
        assert row[0] == "test summary" and str(row[1]) == task.command_id


def test_model_call_records_frozen_prompt_provenance_without_manuscript(mq_store):
    import json

    from agent_runtime.execution.context import _EXECUTION, ExecutionContext
    from agent_runtime.prompts.renderer import render_node, rendering_metadata
    from agent_runtime.settings import RuntimeSettings
    from tests.fixtures.prompts import prompt_versions

    task = command()
    mq_store.accept_task(task)
    job = mq_store.claim_next(task.workspace_id, "runtime", "worker", 60)
    context = ExecutionContext(
        job,
        mq_store,
        RuntimeSettings(_env_file=None),
        prompt_versions=prompt_versions(),
        prompt_pack_versions={"memory.refresh": "frozen-pack"},
    )
    token = _EXECUTION.set(context)
    try:
        messages = render_node("memory.refresh", {"source_text": "Confidential manuscript"})
        metadata = rendering_metadata(messages)
        mq_store.record_model_call(
            job,
            "worker",
            "analysis",
            "test",
            "fake-model",
            "a" * 64,
            {"input_tokens": 12, "output_tokens": 4},
            metadata,
        )
        with mq_store.scope(task.workspace_id) as cursor:
            cursor.execute(
                "SELECT prompt_pack_id,prompt_version,output_schema,token_count FROM prompt_renderings WHERE job_id=%s",
                (task.job_id,),
            )
            row = cursor.fetchone()
            assert row[0] == "frozen-pack"
            assert row[1] == context.prompt_versions["memory.refresh"].version_id
            assert json.loads(row[2]) == context.prompt_versions["memory.refresh"].output_schema
            assert row[3] == 12
            cursor.execute("SELECT metadata FROM model_calls WHERE job_id=%s", (task.job_id,))
            assert "Confidential manuscript" not in str(cursor.fetchone()[0])
            cursor.execute(
                "SELECT payload FROM runtime_event_outbox WHERE job_id=%s AND event_type='project.usage.recorded'",
                (task.job_id,),
            )
            projected = cursor.fetchone()
            assert projected is not None, "model usage was not published to the Control read model"
            assert projected[0]["payload"]["input_tokens"] == 12
    finally:
        _EXECUTION.reset(token)


def test_memory_vector_and_fact_are_atomic_and_unlock_required_version(mq_store):
    from agent_runtime.execution.errors import RetryableTaskError

    task = command("memory.refresh")
    task = task.model_copy(update={"payload": {"document_version_id": task.command_id}})
    mq_store.accept_task(task, "memory")
    memory = mq_store.claim_next(task.workspace_id, "memory", "worker", 60)
    creation = command(workspace_id=task.workspace_id)
    mq_store.accept_task(creation)
    job = mq_store.claim_next(task.workspace_id, "runtime", "worker", 60)
    with pytest.raises(RetryableTaskError):
        mq_store.creation_context(job, "worker", [task.command_id])
    bad = {
        "nodes": [
            {
                "memory_type": "fact",
                "content": "Fact",
                "embedding": [0.1],
                "fact": {"kind": "event", "subject": "Person", "value": "arrived"},
            }
        ],
        "embedding_model": "test-embedding",
        "index_version": "memory@1",
    }
    with pytest.raises(ValueError, match="1536"):
        mq_store.finish(memory, "worker", "completed", bad)
    bad["nodes"][0]["embedding"] = [0.1] * 1536
    mq_store.finish(memory, "worker", "completed", bad)
    context = mq_store.creation_context(job, "worker", [task.command_id])
    assert any(item["kind"] == "canon" and "Person" in item["content"] for item in context)
    with mq_store.scope(task.workspace_id) as cursor:
        cursor.execute("SELECT vector_dims(embedding) FROM memory_embeddings WHERE project_id=%s", (task.project_id,))
        assert cursor.fetchone()[0] == 1536


def test_business_retry_waits_until_database_due_time(mq_store):
    task = command()
    mq_store.accept_task(task)
    job = mq_store.claim_next(task.workspace_id, "runtime", "worker", 60)
    mq_store.finish(job, "worker", "accepted", {"type": "RateLimit"}, retry_seconds=30)
    assert mq_store.claim_next(task.workspace_id, "runtime", "worker", 60) is None


def test_relay_does_not_mark_failed_send_as_published(mq_store):
    task = command()
    mq_store.accept_task(task)

    def unavailable(*args):
        raise ConnectionError()

    assert mq_store.relay(task.workspace_id, "events", unavailable, 30) == 0
    with mq_store.scope(task.workspace_id) as cursor:
        cursor.execute("SELECT published_at,attempts FROM runtime_event_outbox WHERE job_id=%s", (task.job_id,))
        assert cursor.fetchone() == (None, 1)


def test_runtime_rejects_memory_task_on_creation_queue(mq_store):
    task = command("memory.refresh")
    with pytest.raises(ValueError, match="queue"):
        mq_store.accept_task(task)


def test_active_cancel_overrides_a_late_worker_completion(mq_store):
    task = command()
    mq_store.accept_task(task)
    job = mq_store.claim_next(task.workspace_id, "runtime", "worker", 60)
    mq_store.accept_control(command("job.cancel", job_id=task.job_id, workspace_id=task.workspace_id))
    mq_store.finish(job, "worker", "completed", {"should_not_commit": True})
    actual = mq_store.get_scoped(task.job_id, task.workspace_id)
    assert actual.status == "cancelled" and actual.result == {}


def test_scoped_dispatch_cannot_claim_another_workspace(mq_store):
    task = command()
    mq_store.accept_task(task)
    assert mq_store.claim_next("another-workspace", "runtime", "worker", 60) is None


def test_resume_preserves_review_checkpoint(mq_store):
    task = command()
    mq_store.accept_task(task)
    job = mq_store.claim_next(task.workspace_id, "runtime", "worker", 60)
    mq_store.finish(job, "worker", "waiting_review", {"checkpoint_id": "checkpoint-1"})
    mq_store.accept_control(command("job.resume", job_id=task.job_id, workspace_id=task.workspace_id))
    resumed = mq_store.claim_next(task.workspace_id, "runtime", "worker", 60)
    assert resumed.result == {"checkpoint_id": "checkpoint-1"}
    assert resumed.fencing_token > job.fencing_token
