import pytest

from agent_runtime.domain.protocol import RuntimeCommand
from agent_runtime.execution.inbox import CommandAlreadyAccepted, InMemoryCommandInbox


def command() -> RuntimeCommand:
    return RuntimeCommand(
        command_id="command-1",
        command_type="workflow.run",
        workspace_id="workspace-1",
        project_id="project-1",
        job_id="job-1",
        workflow_run_id="run-1",
        capability_snapshot_hash="a" * 64,
        trace_id="trace-1",
        idempotency_key="idem-1",
        payload={"input": "version-1"},
    )


def test_runtime_inbox_accepts_command_once_and_returns_same_command():
    inbox = InMemoryCommandInbox()
    accepted = inbox.accept(command())

    assert accepted.command_id == "command-1"
    assert inbox.get("command-1") == accepted


def test_runtime_inbox_rejects_same_id_with_different_payload():
    inbox = InMemoryCommandInbox()
    inbox.accept(command())
    changed = command().model_copy(update={"payload": {"input": "version-2"}})

    with pytest.raises(CommandAlreadyAccepted):
        inbox.accept(changed)
