import importlib
import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime.domain.protocol import RuntimeCommand

ROOT = Path(__file__).resolve().parents[2]


def _snapshot_model():
    spec = importlib.util.find_spec("agent_runtime.domain.execution_snapshot")
    assert spec is not None, "Runtime ExecutionSnapshot model is missing"
    module = importlib.import_module("agent_runtime.domain.execution_snapshot")
    return module.ExecutionSnapshot


def test_execution_snapshot_fixture_matches_command_scope_and_capability_hash():
    model = _snapshot_model()
    snapshot_data = json.loads((ROOT / "contracts/examples/execution-snapshot.json").read_text())
    command_data = json.loads((ROOT / "contracts/examples/command.json").read_text())

    snapshot = model.model_validate(snapshot_data)
    command = RuntimeCommand.model_validate(command_data)
    snapshot.verify_command(command)

    mismatched = command.model_copy(update={"capability_snapshot_hash": "d" * 64})
    with pytest.raises(ValueError, match="capability snapshot checksum conflict"):
        snapshot.verify_command(mismatched)


def test_execution_snapshot_rejects_unknown_schema_version():
    model = _snapshot_model()
    data = json.loads((ROOT / "contracts/examples/execution-snapshot.json").read_text())
    data["schema_version"] = "2.0"

    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_execution_snapshot_rejects_forged_declared_capability_hash():
    model = _snapshot_model()
    data = json.loads((ROOT / "contracts/examples/execution-snapshot.json").read_text())
    data["snapshot"]["snapshot_hash"] = "d" * 64
    with pytest.raises(ValidationError, match="capability snapshot checksum conflict"):
        model.model_validate(data)
