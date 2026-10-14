import os
from uuid import uuid4

import pytest

from agent_runtime.domain.protocol import RuntimeCommand
from agent_runtime.execution.context import _EXECUTION, ExecutionContext
from agent_runtime.execution.operations import execute_operation
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from agent_runtime.settings import RuntimeSettings


@pytest.fixture
def store():
    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    value = RuntimeMQStore(url)
    yield value
    value.close()


def claimed_job(store):
    command = RuntimeCommand(
        command_id=str(uuid4()),
        command_type="workflow.run",
        workspace_id=str(uuid4()),
        project_id=str(uuid4()),
        job_id=str(uuid4()),
        workflow_run_id=str(uuid4()),
        capability_snapshot_hash="0" * 64,
        trace_id="knowledge-test",
        idempotency_key=str(uuid4()),
        payload={},
    )
    store.accept_task(command)
    with store.scope(command.workspace_id) as cursor:
        cursor.execute(
            """INSERT INTO memory_nodes(id,workspace_id,project_id,memory_type,source_id,source_version,
            content,content_hash) VALUES(%s,%s,%s,'summary','source',1,'A fact','fact')""",
            (str(uuid4()), command.workspace_id, command.project_id),
        )
    return store.claim_next(command.workspace_id, "runtime", "knowledge-worker", 60)


async def outcome(store, job, operation="knowledge.publish", arguments=None):
    settings = RuntimeSettings(_env_file=None, worker_id="knowledge-worker")
    context = ExecutionContext(job, store, settings)
    token = _EXECUTION.set(context)
    try:
        return await execute_operation(
            job.command,
            {
                "request": {
                    "runtime_operation": operation,
                    "arguments": arguments or {"name": "Published facts"},
                }
            },
        )
    finally:
        _EXECUTION.reset(token)


def packs(store, job):
    with store.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT id FROM knowledge_packs WHERE source_project_id=%s", (job.command.project_id,))
        return [str(row[0]) for row in cursor.fetchall()]


async def test_publish_waits_for_atomic_job_completion(store):
    job = claimed_job(store)
    result = await outcome(store, job)
    assert packs(store, job) == []
    store.finish(job, "knowledge-worker", "completed", result)
    assert len(packs(store, job)) == 1
    with store.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT result FROM runtime_jobs WHERE id=%s", (job.command.job_id,))
        committed = cursor.fetchone()[0]
        assert committed["operation_result"]["id"] == packs(store, job)[0]
        assert "_knowledge_commit" not in committed


async def test_cancelled_publication_has_no_side_effect(store):
    job = claimed_job(store)
    result = await outcome(store, job)
    cancel = job.command.model_copy(update={"command_id": str(uuid4()), "command_type": "job.cancel"})
    store.accept_control(cancel)
    store.finish(job, "knowledge-worker", "completed", result)
    assert packs(store, job) == []


async def test_lost_lease_and_crash_retry_publish_one_pack(store):
    from agent_runtime.execution.leases import FencingTokenError

    job = claimed_job(store)
    first = await outcome(store, job)
    with store.scope(job.command.workspace_id) as cursor:
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job.command.job_id,),
        )
    with pytest.raises(FencingTokenError):
        store.finish(job, "knowledge-worker", "completed", first)
    assert packs(store, job) == []
    retried = store.claim_next(job.command.workspace_id, "runtime", "knowledge-worker", 60)
    second = await outcome(store, retried)
    assert first["operation_result"]["id"] == second["operation_result"]["id"]
    store.finish(retried, "knowledge-worker", "completed", second)
    assert len(packs(store, job)) == 1


@pytest.mark.parametrize("operation", ["knowledge.bind", "knowledge.unbind"])
async def test_cancelled_bind_and_unbind_leave_sharing_unchanged(store, operation):
    from agent_runtime.infrastructure.postgres.knowledge import KnowledgeOperations

    job = claimed_job(store)
    knowledge = KnowledgeOperations(store)
    pack = knowledge.publish(job.command.workspace_id, job.command.project_id, "Source facts")
    if operation == "knowledge.unbind":
        knowledge.share(job.command.workspace_id, pack["id"], job.command.project_id)
    staged = await outcome(store, job, operation, {"pack_id": pack["id"]})
    cancel = job.command.model_copy(update={"command_id": str(uuid4()), "command_type": "job.cancel"})
    store.accept_control(cancel)
    store.finish(job, "knowledge-worker", "completed", staged)
    with store.scope(job.command.workspace_id) as cursor:
        cursor.execute("SELECT count(*) FROM knowledge_pack_bindings WHERE pack_id=%s", (pack["id"],))
        assert cursor.fetchone()[0] == (1 if operation == "knowledge.unbind" else 0)


async def test_cancel_requested_before_staging_rejects_knowledge_mutation(store):
    from agent_runtime.execution.errors import JobCancelled

    job = claimed_job(store)
    cancel = job.command.model_copy(update={"command_id": str(uuid4()), "command_type": "job.cancel"})
    store.accept_control(cancel)
    with pytest.raises(JobCancelled):
        await outcome(store, job)
    assert packs(store, job) == []


async def test_lease_expiry_during_finalize_rolls_back_pack_and_event(store, monkeypatch):
    from agent_runtime.execution.leases import FencingTokenError
    from agent_runtime.infrastructure.postgres import knowledge

    job = claimed_job(store)
    staged = await outcome(store, job)
    commit = knowledge.commit_knowledge_operation

    def expire_after_insert(cursor, command, operation, arguments, identity):
        result = commit(cursor, command, operation, arguments, identity)
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (command.job_id,),
        )
        return result

    monkeypatch.setattr(knowledge, "commit_knowledge_operation", expire_after_insert)
    with pytest.raises(FencingTokenError):
        store.finish(job, "knowledge-worker", "completed", staged)
    assert packs(store, job) == []
    with store.scope(job.command.workspace_id) as cursor:
        cursor.execute(
            "SELECT count(*) FROM runtime_event_outbox WHERE job_id=%s AND event_type='job.completed'",
            (job.command.job_id,),
        )
        assert cursor.fetchone()[0] == 0


def test_finish_rejects_fabricated_knowledge_commit(store):
    job = claimed_job(store)
    with pytest.raises(ValueError, match="identity conflict"):
        store.finish(
            job,
            "knowledge-worker",
            "completed",
            {
                "operation": "knowledge.publish",
                "operation_result": {},
                "_knowledge_commit": {"operation": "knowledge.publish", "arguments": {"name": "Forged"}},
            },
        )
    assert packs(store, job) == []


async def test_invalid_knowledge_operation_fails_before_finalization(store):
    job = claimed_job(store)
    with store.scope(job.command.workspace_id) as cursor:
        cursor.execute("DELETE FROM memory_nodes WHERE project_id=%s", (job.command.project_id,))
    with pytest.raises(ValueError, match="empty"):
        await outcome(store, job)
    with pytest.raises(KeyError, match="not found"):
        await outcome(store, job, "knowledge.bind", {"pack_id": str(uuid4())})
