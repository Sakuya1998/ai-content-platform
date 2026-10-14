import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent
from agent_runtime.execution.leases import FencingTokenError, InMemoryRuntimeJobStore, LeaseLostError, RuntimeWorker


def make_command(command_id: str = "cmd-worker-1") -> RuntimeCommand:
    return RuntimeCommand(
        command_id=command_id,
        command_type="workflow.run",
        workspace_id="workspace-1",
        project_id="project-1",
        job_id=f"job-{command_id}",
        workflow_run_id=f"run-{command_id}",
        capability_snapshot_hash="a" * 64,
        trace_id="trace-1",
        idempotency_key=command_id,
        payload={"document_version_id": "version-1"},
    )


def test_claim_uses_fencing_token_and_rejects_stale_worker_commit():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = InMemoryRuntimeJobStore(clock=lambda: now)
    command = make_command()
    store.accept(command)
    first = store.claim(command.job_id, "worker-a", lease_seconds=1)

    now = now + timedelta(seconds=2)
    second = store.claim(command.job_id, "worker-b", lease_seconds=30)

    assert second.fencing_token > first.fencing_token
    with pytest.raises(FencingTokenError):
        store.complete(command.job_id, "worker-a", first.fencing_token, {"stale": True})


def test_worker_emits_ordered_events_and_completes_job():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = InMemoryRuntimeJobStore(clock=lambda: now)
    command = make_command("cmd-worker-2")
    store.accept(command)
    events: list[RuntimeEvent] = []

    async def handler(_command: RuntimeCommand) -> dict:
        return {"document_version_id": "version-2"}

    async def run() -> None:
        worker = RuntimeWorker(store, events.append)
        await worker.run(command.job_id, "worker-a", handler)

    asyncio.run(run())

    assert [event.event_type for event in events] == ["job.running", "job.completed"]
    assert [event.sequence for event in events] == [1, 2]
    assert store.get(command.job_id).status == "completed"


def test_worker_reports_lease_loss_without_committing_result():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = InMemoryRuntimeJobStore(clock=lambda: now)
    command = make_command("cmd-worker-3")
    store.accept(command)
    events: list[RuntimeEvent] = []

    async def handler(_command: RuntimeCommand) -> dict:
        store.expire(command.job_id)
        return {"should_not_commit": True}

    async def run() -> None:
        worker = RuntimeWorker(store, events.append)
        await worker.run(command.job_id, "worker-a", handler)

    with pytest.raises(LeaseLostError):
        asyncio.run(run())
    assert store.get(command.job_id).status == "running"
    assert events[-1].event_type == "job.lease_lost"
