import hashlib
import json

import pytest
from pydantic import ValidationError

from agent_runtime.domain.capabilities import CapabilitySnapshot
from agent_runtime.domain.workflow_contracts import WORKFLOWS


def test_capability_snapshot_captures_runtime_surfaces_and_schema_versions():
    snapshot = CapabilitySnapshot(
        workflow="novel-writing",
        skills=["outline", "draft"],
        prompt_packs=["writer-v2"],
        tools=["search"],
        mcp_policies=["library-readonly@1"],
        model_routes={"writer": "route-a"},
        schema_versions={"content_ir": "1.0", "context_pack": "1.0"},
    )

    assert snapshot.workflow == "novel-writing"
    assert snapshot.schema_versions["content_ir"] == "1.0"
    assert snapshot.snapshot_hash


def test_capability_snapshot_requires_schema_versions():
    with pytest.raises(ValidationError):
        CapabilitySnapshot(workflow="novel-writing")


def test_capability_snapshot_freezes_catalog_workflow_identity_and_rejects_tampering():
    definition = {"template": "content.generate@1", "nodes": list(WORKFLOWS["content.generate@1"])}
    digest = hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    reference = {
        "version_id": "catalog-v2",
        "name": "Writer",
        "version": 2,
        "content_hash": digest,
        "definition": definition,
    }
    snapshot = CapabilitySnapshot(
        workflow="content.generate@1", workflow_definition=reference, schema_versions={"content_ir": "1.0"}
    )
    assert snapshot.workflow_definition.version == 2
    assert CapabilitySnapshot.model_validate_json(snapshot.model_dump_json()).snapshot_hash == snapshot.snapshot_hash
    reference["definition"] = {"template": "content.generate@1", "nodes": ["unapproved"]}
    with pytest.raises(ValidationError, match="workflow definition checksum conflict"):
        CapabilitySnapshot(
            workflow="content.generate@1", workflow_definition=reference, schema_versions={"content_ir": "1.0"}
        )


@pytest.mark.parametrize(
    "nodes", [["unapproved"], ["content.write"], [*WORKFLOWS["content.generate@1"], "content.write"]]
)
def test_frozen_definition_rejects_signed_but_unregistered_node_contract(nodes):
    definition = {"template": "content.generate@1", "nodes": nodes}
    digest = hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(ValidationError, match="unregistered workflow node contract"):
        CapabilitySnapshot(
            workflow="content.generate@1",
            schema_versions={"content_ir": "1.0"},
            workflow_definition={
                "version_id": "catalog",
                "name": "Writer",
                "version": 1,
                "content_hash": digest,
                "definition": definition,
            },
        )


def test_frozen_definition_rejects_another_workflow_template():
    definition = {"template": "content.adapt@1", "nodes": list(WORKFLOWS["content.adapt@1"])}
    digest = hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(ValidationError, match="workflow definition template conflict"):
        CapabilitySnapshot(
            workflow="content.generate@1",
            schema_versions={"content_ir": "1.0"},
            workflow_definition={
                "version_id": "catalog",
                "name": "Adapter",
                "version": 1,
                "content_hash": digest,
                "definition": definition,
            },
        )
