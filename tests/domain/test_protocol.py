import pytest
from pydantic import ValidationError

from agent_runtime.domain.capabilities import CapabilitySnapshot
from agent_runtime.domain.execution_snapshot import ExecutionSnapshot
from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent


def test_shared_examples_match_the_actual_runtime_models():
    from pathlib import Path

    import jsonschema

    for name, model in [
        ("command", RuntimeCommand),
        ("event", RuntimeEvent),
        ("execution-snapshot", ExecutionSnapshot),
    ]:
        import json

        root = Path("contracts")
        value = json.loads((root / "examples" / f"{name}.json").read_text())
        instance = model.model_validate(value)
        schema = json.loads((root / f"{name}.schema.json").read_text())
        jsonschema.validate(instance.model_dump(mode="json"), schema)


def test_capability_snapshot_has_immutable_hash_and_mcp_policy_versions():
    snapshot = CapabilitySnapshot(
        workflow="novel-to-screenplay@1",
        skills=["adaptation@1"],
        prompt_packs=["screenplay@2"],
        tools=["canon_lookup@1"],
        mcp_policies=["research-readonly@1"],
        model_routes={"creative": "route@1"},
        schema_versions={"content_ir": "1.0"},
    )

    assert snapshot.snapshot_hash
    assert len(snapshot.snapshot_hash) == 64
    assert snapshot.mcp_policies == ["research-readonly@1"]
    assert snapshot.model_dump() == CapabilitySnapshot.model_validate_json(snapshot.model_dump_json()).model_dump()


def test_runtime_command_rejects_missing_execution_identity():
    with pytest.raises(ValidationError):
        RuntimeCommand(
            command_id="cmd-1",
            command_type="workflow.run",
            workspace_id="workspace-1",
            project_id="project-1",
            job_id="job-1",
            payload={},
        )


def test_runtime_command_preserves_execution_snapshot_reference():
    command = RuntimeCommand(
        command_id="cmd-snapshot",
        command_type="workflow.run",
        workspace_id="workspace-1",
        project_id="project-1",
        job_id="job-1",
        workflow_run_id="run-1",
        capability_snapshot_hash="a" * 64,
        trace_id="trace-1",
        idempotency_key="request-1",
        execution_snapshot={
            "object_key": "workspaces/workspace-1/projects/project-1/assets/job-1/versions/v1/execution.json",
            "sha256": "b" * 64,
            "size_bytes": 128,
        },
    )

    assert command.execution_snapshot.object_key.endswith("/execution.json")
    assert command.execution_snapshot.sha256 == "b" * 64
    assert command.execution_snapshot.size_bytes == 128


@pytest.mark.parametrize(
    ("reference", "expected_error"),
    [
        (
            {
                "object_key": "workspaces/workspace-1/projects/project-1/assets/job-1/versions/v1/execution.json",
                "sha256": "not-a-sha256",
                "size_bytes": 128,
            },
            "string_pattern_mismatch",
        ),
        (
            {
                "object_key": "workspaces/workspace-1/projects/project-1/assets/job-1/versions/v1/execution.json",
                "sha256": "b" * 64,
                "size_bytes": 0,
            },
            "greater_than_equal",
        ),
    ],
)
def test_runtime_command_rejects_malformed_execution_snapshot_reference(reference, expected_error):
    command = {
        "command_id": "cmd-snapshot",
        "command_type": "workflow.run",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "job_id": "job-1",
        "workflow_run_id": "run-1",
        "capability_snapshot_hash": "a" * 64,
        "trace_id": "trace-1",
        "idempotency_key": "request-1",
        "execution_snapshot": reference,
    }

    with pytest.raises(ValidationError) as error:
        RuntimeCommand.model_validate(command)

    assert any(item["type"] == expected_error for item in error.value.errors())


def test_runtime_command_rejects_execution_snapshot_outside_project_scope():
    command = {
        "command_id": "cmd-snapshot",
        "command_type": "workflow.run",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "job_id": "job-1",
        "workflow_run_id": "run-1",
        "capability_snapshot_hash": "a" * 64,
        "trace_id": "trace-1",
        "idempotency_key": "request-1",
        "execution_snapshot": {
            "object_key": "workspaces/workspace-1/projects/project-2/assets/job-1/versions/v1/execution.json",
            "sha256": "b" * 64,
            "size_bytes": 128,
        },
    }

    with pytest.raises(ValidationError, match="execution snapshot object key must match command scope"):
        RuntimeCommand.model_validate(command)


def test_runtime_event_carries_sequence_and_operation_identity():
    event = RuntimeEvent(
        event_id="event-1",
        event_type="job.accepted",
        workspace_id="workspace-1",
        project_id="project-1",
        job_id="job-1",
        workflow_run_id="run-1",
        sequence=1,
        operation_id="op-1",
        trace_id="trace-1",
        payload={"status": "accepted"},
    )

    assert event.sequence == 1
    assert event.operation_id == "op-1"
