import hashlib
import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agent_runtime.domain.capabilities import CapabilitySnapshot
from agent_runtime.workflows.content.graph import create_content_graph
from tests.fixtures.prompts import prompt_versions


async def test_controlled_graph_generates_screenplay_ir_and_applies_review():
    model = FakeListChatModel(
        responses=[
            json.dumps({"plan": "A two-scene screenplay"}),
            json.dumps({"blocks": [{"type": "scene", "text": "INT. STUDIO - DAY", "position": 0}]}),
            json.dumps({"approved": True, "notes": []}),
        ]
    )
    snapshot = CapabilitySnapshot(
        workflow="content.generate@1",
        skills=["controlled-text@1"],
        prompt_packs=["content@1"],
        schema_versions={"content_ir": "1.0"},
    )
    graph = create_content_graph(model, model, snapshot, prompt_versions=prompt_versions())
    result = await graph.ainvoke(
        {
            "objective": "Write a screenplay",
            "document_type": "screenplay",
            "context": "",
            "document_id": "doc-1",
            "project_id": "p-1",
            "version": 1,
        }
    )
    assert result["document"]["document_type"] == "screenplay"
    assert result["document"]["blocks"][0]["text"] == "INT. STUDIO - DAY"
    assert result["review"]["approved"] is True


async def test_frozen_catalog_metadata_preserves_registered_graph_execution():
    from agent_runtime.domain.workflow_contracts import WORKFLOWS

    definition = {"template": "content.generate@1", "nodes": list(WORKFLOWS["content.generate@1"])}
    content_hash = hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    snapshot = CapabilitySnapshot(
        workflow="content.generate@1",
        schema_versions={"content_ir": "1.0"},
        workflow_definition={
            "version_id": "frozen-v2",
            "name": "Writer",
            "version": 2,
            "content_hash": content_hash,
            "definition": definition,
        },
    )
    model = FakeListChatModel(
        responses=[
            '{"plan":"one scene"}',
            '{"blocks":[{"type":"text","text":"Written"}]}',
            '{"approved":true,"notes":[]}',
        ]
    )
    graph = create_content_graph(model, model, snapshot, prompt_versions=prompt_versions())
    result = await graph.ainvoke(
        {
            "objective": "Write",
            "document_type": "copy",
            "context": "",
            "document_id": "doc",
            "project_id": "project",
            "version": 1,
        }
    )
    assert result["document"]["blocks"][0]["text"] == "Written"
    assert result["review"]["approved"] is True


def test_graph_rejects_unknown_nodes_and_unimplemented_capabilities_before_model_call():
    snapshot = CapabilitySnapshot(workflow="content.generate@1", schema_versions={"content_ir": "1.0"})
    model = FakeListChatModel(responses=["never"])
    with pytest.raises(ValueError, match="node"):
        create_content_graph(model, model, snapshot, nodes=["shell.execute"])
    with pytest.raises(ValueError, match="capability"):
        create_content_graph(model, model, snapshot.model_copy(update={"tools": ["unknown@1"]}))


async def test_adaptation_includes_source_and_rejects_invalid_review_output():
    model = FakeListChatModel(
        responses=[
            '{"plan":"adapt"}',
            '{"blocks":[{"type":"copy","text":"Product copy"}]}',
            '{"approved":false,"notes":["needs review"]}',
        ]
    )
    snapshot = CapabilitySnapshot(workflow="content.adapt@1", schema_versions={"content_ir": "1.0"})
    result = await create_content_graph(model, model, snapshot, prompt_versions=prompt_versions()).ainvoke(
        {
            "objective": "Adapt",
            "document_type": "copy",
            "context": "source material",
            "document_id": "doc",
            "project_id": "project",
            "version": 1,
        }
    )
    assert not result["review"]["approved"]


def test_model_configuration_missing_key_fails_without_touching_legacy_sqlite():
    from agent_runtime.infrastructure.models.client import RuntimeModels
    from agent_runtime.settings import RuntimeSettings

    with pytest.raises(ValueError, match="configured"):
        RuntimeModels(RuntimeSettings(_env_file=None)).chat("creative")
