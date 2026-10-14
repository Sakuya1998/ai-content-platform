"""Adapter checks must override optimistic model reviews without losing drafts."""

import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agent_runtime.domain.capabilities import CapabilitySnapshot
from agent_runtime.workflows.content.graph import create_content_graph
from tests.fixtures.prompts import prompt_versions


async def generate(blocks, *, document_type="screenplay", workflow="content.generate@1", **state):
    model = FakeListChatModel(
        responses=[json.dumps({"plan": "draft"}), json.dumps({"blocks": blocks}), '{"approved":true,"notes":[]}']
    )
    snapshot = CapabilitySnapshot(workflow=workflow, schema_versions={"content_ir": "1.0"})
    return await create_content_graph(model, model, snapshot, prompt_versions=prompt_versions()).ainvoke(
        {
            "objective": "Create content",
            "document_type": document_type,
            "document_id": "target",
            "project_id": "project",
            "version": 1,
            **state,
        }
    )


def source_document():
    return {
        "document_id": "source",
        "document_type": "novel",
        "version": 2,
        "schema_version": "1.0",
        "blocks": [
            {"type": "paragraph", "text": "Alice opens the door.", "metadata": {"block_id": "source-1"}},
            {"type": "paragraph", "text": "Bob waits outside.", "metadata": {"block_id": "source-2"}},
        ],
    }


def findings(result):
    return {finding["code"] for finding in result["review"]["adapter_findings"]}


async def test_legacy_scene_receives_stable_id_across_versions_and_keeps_approval():
    blocks = [{"type": "scene", "text": "INT. STUDIO - DAY"}]
    first = await generate(blocks)
    second = await generate(blocks, version=2)
    first_id = first["document"]["blocks"][0]["metadata"]["block_id"]
    assert first_id
    assert second["document"]["blocks"][0]["metadata"]["block_id"] == first_id
    assert first["review"]["approved"] is True
    assert first["document"]["schema_version"] == "1.0"


async def test_duplicate_block_ids_require_human_review_and_preserve_draft():
    result = await generate(
        [
            {"type": "scene", "text": "First", "metadata": {"block_id": "same"}},
            {"type": "scene", "text": "Second", "metadata": {"block_id": "same"}},
        ]
    )
    assert result["review"]["approved"] is False
    assert result["review"]["human_review_required"] is True
    assert "duplicate_block_id" in findings(result)
    assert result["document"]["blocks"][1]["text"] == "Second"


async def test_structured_screenplay_validates_character_dialogue_and_scene_references():
    result = await generate(
        [
            {"type": "character", "text": "Alice", "metadata": {"character_id": "alice"}},
            {
                "type": "scene",
                "text": "INT. ROOM - DAY",
                "metadata": {"block_id": "room", "scene": {"number": 1, "duration_seconds": 10}},
            },
            {"type": "dialogue", "text": "Hello", "metadata": {"character_id": "alice", "scene_id": "room"}},
        ]
    )
    assert result["review"]["approved"] is True
    assert result["document"]["blocks"][1]["metadata"]["scene"]["duration_seconds"] == 10


@pytest.mark.parametrize(
    ("block", "code"),
    [
        ({"type": "scene", "text": "Room", "metadata": {"scene": {"number": "1"}}}, "invalid_scene_metadata"),
        (
            {"type": "scene", "text": "Room", "metadata": {"scene": {"number": 1, "duration_seconds": 0}}},
            "invalid_scene_metadata",
        ),
        ({"type": "dialogue", "text": "Hello", "metadata": {"character_id": "ghost"}}, "unknown_character_reference"),
        ({"type": "dialogue", "text": "Hello"}, "missing_character_reference"),
        ({"type": "scene", "text": "Room", "metadata": {"scene": {"number": 2}}}, "invalid_scene_numbering"),
        ({"type": "episode", "text": "Episode", "metadata": {"episode": {"number": 2}}}, "invalid_episode_numbering"),
    ],
)
async def test_invalid_typed_structure_blocks_model_approval(block, code):
    result = await generate([block], document_type="short_drama")
    assert result["review"]["approved"] is False
    assert code in findings(result)


async def test_short_drama_checks_episode_duration_and_scene_episode_reference():
    result = await generate(
        [
            {"type": "episode", "text": "First", "metadata": {"episode": {"number": 1, "duration_seconds": 5}}},
            {
                "type": "scene",
                "text": "First scene",
                "metadata": {
                    "scene": {
                        "number": 1,
                        "episode_number": 1,
                        "duration_seconds": 6,
                    }
                },
            },
            {"type": "scene", "text": "Orphan", "metadata": {"scene": {"number": 1, "episode_number": 2}}},
        ],
        document_type="short_drama",
    )
    assert {"episode_duration_exceeded", "unknown_episode_reference"} <= findings(result)
    assert result["review"]["approved"] is False


async def test_conversion_preserves_explicit_references_and_frozen_source_version():
    result = await generate(
        [{"type": "scene", "text": "INT. ROOM - DAY", "metadata": {"source_block_ids": ["source-1", "source-2"]}}],
        workflow="content.adapt@1",
        source_document=source_document(),
        source_version_id="frozen-source-v2",
    )
    metadata = result["document"]["blocks"][0]["metadata"]
    assert metadata["source_block_ids"] == ["source-1", "source-2"]
    assert metadata["source_version_id"] == "frozen-source-v2"
    assert result["document"]["metadata"]["source_version_id"] == "frozen-source-v2"
    assert result["review"]["approved"] is True
    assert "factual_equivalence_verified" not in metadata


@pytest.mark.parametrize(
    ("metadata", "codes"),
    [
        ({}, {"missing_source_references", "omitted_source_blocks"}),
        ({"source_block_ids": ["invented"]}, {"unknown_source_reference", "omitted_source_blocks"}),
        ({"source_block_ids": ["source-1"]}, {"omitted_source_blocks"}),
        ({"source_block_ids": "source-1"}, {"invalid_source_references", "omitted_source_blocks"}),
        ({"source_block_ids": ["source-1", "source-2"], "source_version_id": "other"}, {"source_version_mismatch"}),
    ],
)
async def test_conversion_requires_review_for_unverifiable_or_incomplete_lineage(metadata, codes):
    result = await generate(
        [{"type": "scene", "text": "Adapted draft", "metadata": metadata}],
        workflow="content.adapt@1",
        source_document=source_document(),
        source_version_id="frozen-source-v2",
    )
    assert result["review"]["approved"] is False
    assert result["review"]["human_review_required"] is True
    assert codes <= findings(result)


async def test_conversion_without_source_cannot_be_approved():
    result = await generate([{"type": "copy", "text": "Draft"}], document_type="copy", workflow="content.adapt@1")
    assert result["review"]["approved"] is False
    assert {"missing_source_document", "missing_source_version"} <= findings(result)


@pytest.mark.parametrize(
    ("metadata", "code"),
    [
        ({"block_id": []}, "invalid_block_id"),
        ({"character_id": "alice", "scene_id": ["invalid"]}, "unknown_scene_reference"),
        ({"scene": {"number": True}}, "invalid_scene_metadata"),
        ({"scene": {"number": 1, "duration_seconds": float("inf")}}, "invalid_scene_metadata"),
    ],
)
async def test_invalid_ids_and_nonfinite_metadata_return_findings_instead_of_crashing(metadata, code):
    result = await generate([{"type": "dialogue", "text": "Draft", "metadata": metadata}])
    assert code in findings(result)
    assert result["review"]["approved"] is False


async def test_conversion_rejects_duplicate_source_ids_even_when_coverage_appears_complete():
    source = source_document()
    source["blocks"][1]["metadata"]["block_id"] = "source-1"
    result = await generate(
        [{"type": "scene", "text": "Draft", "metadata": {"source_block_ids": ["source-1"]}}],
        workflow="content.adapt@1",
        source_document=source,
        source_version_id="v2",
    )
    assert "source_duplicate_block_id" in findings(result)
    assert result["review"]["approved"] is False


async def test_conversion_reports_invalid_source_and_duplicate_references_without_discarding_draft():
    result = await generate(
        [{"type": "copy", "text": "Draft", "metadata": {"source_block_ids": ["source-1", "source-1"]}}],
        document_type="copy",
        workflow="content.adapt@1",
        source_document={"blocks": []},
        source_version_id="v2",
    )
    assert {"invalid_source_document", "duplicate_source_reference"} <= findings(result)
    assert result["document"]["blocks"][0]["text"] == "Draft"


async def test_conversion_prompt_exposes_normalized_source_ids_to_the_writer():
    from langchain_core.messages import AIMessage

    class SourceAwareModel(FakeListChatModel):
        async def ainvoke(self, input, config=None, **kwargs):
            # Act as a writer that can cite only IDs actually present in its prompt.
            if self.i == 1:
                text = "\n".join(message["content"] for message in input)
                source_line = text.split("Source Content IR (reference data):\n", 1)[1].splitlines()[0]
                source = json.loads(source_line)
                self.i += 1
                return AIMessage(
                    content=json.dumps(
                        {
                            "blocks": [
                                {
                                    "type": "scene",
                                    "text": "INT. ROOM - DAY",
                                    "metadata": {
                                        "source_block_ids": [
                                            block["metadata"]["block_id"] for block in source["blocks"]
                                        ],
                                    },
                                }
                            ]
                        }
                    )
                )
            return await super().ainvoke(input, config, **kwargs)

    source = source_document()
    for block in source["blocks"]:
        block["metadata"] = {}
    model = SourceAwareModel(responses=['{"plan":"adapt"}', "unused", '{"approved":true,"notes":[]}'])
    snapshot = CapabilitySnapshot(workflow="content.adapt@1", schema_versions={"content_ir": "1.0"})
    result = await create_content_graph(model, model, snapshot, prompt_versions=prompt_versions()).ainvoke(
        {
            "objective": "Adapt",
            "document_type": "screenplay",
            "document_id": "target",
            "project_id": "project",
            "version": 1,
            "source_document": source,
            "source_version_id": "v2",
        }
    )
    assert result["review"]["approved"] is True
    assert len(result["document"]["blocks"][0]["metadata"]["source_block_ids"]) == 2
    assert source["blocks"][0]["metadata"] == {}


async def test_generic_content_keeps_shared_review_without_screenplay_constraints():
    result = await generate([{"type": "dialogue", "text": "Marketing dialogue"}], document_type="copy")
    assert result["review"]["approved"] is True
    assert not result["review"]["adapter_findings"]


def test_explicit_block_identity_survives_reordering_and_content_hash_is_refreshed():
    from agent_runtime.domain.content import ContentDocument
    from agent_runtime.domain.content_adapters import adapt_document

    source = ContentDocument.model_validate(source_document())
    updated = source.model_copy(update={"version": 3, "blocks": list(reversed(source.blocks))})
    result = adapt_document(updated)
    assert [block.metadata["block_id"] for block in result.document.blocks] == ["source-2", "source-1"]
    assert [block.position for block in result.document.blocks] == [0, 1]
    assert result.document.content_hash != source.content_hash
    assert not result.findings


async def test_conversion_adapter_is_registered_and_its_findings_gate_review(monkeypatch):
    from types import SimpleNamespace

    from agent_runtime.domain import content_adapters

    registry = getattr(content_adapters, "WORKFLOW_ADAPTERS", {})
    assert "content.adapt@1" in registry
    monkeypatch.setitem(
        registry,
        "content.adapt@1",
        SimpleNamespace(
            contract="Custom conversion contract",
            check=lambda document, source, version: [content_adapters.AdapterFinding("custom_lineage", "Needs review")],
        ),
    )
    result = await generate([{"type": "copy", "text": "Draft"}], document_type="copy", workflow="content.adapt@1")
    assert "custom_lineage" in findings(result)
    assert result["review"]["approved"] is False


async def test_adapter_findings_remain_blocking_when_shared_evaluation_passes(monkeypatch):
    from agent_runtime.domain.policies import CreationPolicy
    from tests.fixtures.policies import policy_payload

    payload = policy_payload()
    payload["constraints"]["schema"]["properties"].update(
        {
            "objective": {"type": "string"},
            "document_type": {"type": "string"},
        }
    )
    policy = CreationPolicy.model_validate(payload)
    monkeypatch.setattr("agent_runtime.execution.policies.current_creation_policy", lambda: policy)
    result = await generate(
        [
            {"type": "scene", "text": "Scene", "metadata": {"scene": {"number": 2}}},
        ]
    )
    assert result["review"]["deterministic_report"]["overall_score"] == 100
    assert "invalid_scene_numbering" in findings(result)
    assert result["review"]["approved"] is False
