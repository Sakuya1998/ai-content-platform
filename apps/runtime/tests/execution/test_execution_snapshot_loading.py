import asyncio
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

from agent_runtime.domain.protocol import ExecutionSnapshotReference, RuntimeCommand
from agent_runtime.settings import RuntimeSettings

ROOT = Path(__file__).resolve().parents[4]


def _loader_module():
    spec = importlib.util.find_spec("agent_runtime.execution.snapshots")
    assert spec is not None, "Runtime execution snapshot loader is missing"
    return importlib.import_module("agent_runtime.execution.snapshots")


def _command_and_snapshot():
    data = json.loads((ROOT / "packages/contracts/contracts/examples/execution-snapshot.json").read_text())
    body = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    command = RuntimeCommand(
        command_id="command-1",
        command_type="workflow.run",
        workspace_id=data["workspace_id"],
        project_id=data["project_id"],
        job_id=data["job_id"],
        workflow_run_id=data["job_id"],
        capability_snapshot_hash=data["snapshot"]["snapshot_hash"],
        execution_snapshot=ExecutionSnapshotReference(
            object_key="workspaces/workspace-001/projects/project-001/assets/job-001/versions/hash/execution.json",
            sha256=digest,
            size_bytes=len(body),
        ),
        trace_id="trace-1",
        idempotency_key="request-1",
    )
    return command, body


def test_runtime_loads_execution_snapshot_from_object_reference(monkeypatch):
    module = _loader_module()
    command, body = _command_and_snapshot()

    class Store:
        def __init__(self, settings):
            assert settings is not None

        def download_key(self, key, *, expected_sha256, expected_size_bytes):
            assert key == command.execution_snapshot.object_key
            assert expected_sha256 == command.execution_snapshot.sha256
            assert expected_size_bytes == len(body)
            return body

    monkeypatch.setattr(module, "S3ArtifactStore", Store)
    bundle = asyncio.run(module.load_execution_snapshot(command, RuntimeSettings(_env_file=None)))

    assert bundle["job_id"] == command.job_id
    assert bundle["source"]["document_version_id"] == "version-004"
    assert bundle["request"]["project_id"] == command.project_id


def test_runtime_rejects_snapshot_for_another_command_scope(monkeypatch):
    module = _loader_module()
    command, body = _command_and_snapshot()

    class Store:
        def __init__(self, settings):
            pass

        def download_key(self, *_args, **_kwargs):
            return body

    monkeypatch.setattr(module, "S3ArtifactStore", Store)
    wrong = command.model_copy(update={"job_id": "another-job"})

    try:
        asyncio.run(module.load_execution_snapshot(wrong, RuntimeSettings(_env_file=None)))
    except ValueError as exc:
        assert "identity conflict" in str(exc)
    else:
        raise AssertionError("snapshot for another job was accepted")
