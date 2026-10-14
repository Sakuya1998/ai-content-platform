"""Novel vocabulary is confined to the format adapter, including restored state."""

import pytest


def test_publication_envelope_uses_generic_content_fields_and_round_trips():
    from agent_runtime.workflows.novel.publication import novel_envelope, project_envelope

    original = {
        "state": {
            "novel_id": "project",
            "total_chapters": 2,
            "current_chapter": 1,
            "chapters": [{"chapter_number": 1, "content": "Preserved chapter_number prose"}],
            "book_audit_completed": True,
            "book_summary": "Whole story",
            "memory_index": {"chapters": [{"chapter_number": 1, "summary": "One block"}]},
            "current_draft": {"chapter_number": 2, "content": "Draft"},
        },
        "chapter_versions": [{"id": "version", "chapter_number": 1, "version_number": 1}],
        "book_audits": [{"id": "audit", "chapter_number": 0}],
        "evaluations": [{"chapter_number": 1, "chapter_version": 1}],
    }
    public = project_envelope(original)
    assert public["state"]["project_id"] == "project"
    assert public["state"]["blocks"][0]["block_index"] == 1
    assert public["state"]["block_count"] == 2
    assert public["state"]["audit_completed"] is True
    assert public["state"]["content_summary"] == "Whole story"
    assert public["state"]["memory_index"]["blocks"][0]["summary"] == "One block"
    assert public["content_versions"][0]["block_index"] == 1
    assert public["audits"][0]["id"] == "audit"
    assert public["evaluations"][0]["content_version"] == 1
    assert novel_envelope(public) == original
    assert original["state"]["novel_id"] == "project"


def test_imported_public_state_and_records_are_adapted_to_novel_graph_fields():
    from agent_runtime.workflows.novel.workflow import adapt_novel_request

    request = adapt_novel_request(
        {
            "project_id": "project",
            "input": {"block_count": 3},
            "imported_state": {"project_id": "project", "blocks": [{"block_index": 1, "content": "Imported"}]},
            "imported_envelope": {"content_versions": [{"id": "old", "block_index": 1}], "audits": []},
        }
    )
    assert request["novel_input"]["total_chapters"] == 3
    assert request["imported_state"]["novel_id"] == "project"
    assert request["imported_state"]["chapters"][0]["chapter_number"] == 1
    assert request["imported_envelope"]["chapter_versions"][0]["id"] == "old"


def test_publication_rejects_ambiguous_identity_fields():
    from agent_runtime.workflows.novel.publication import project_envelope

    with pytest.raises(ValueError, match="conflicting publication field"):
        project_envelope({"state": {"novel_id": "project", "project_id": "foreign"}})


def test_common_request_maps_frozen_resources_and_brief_revision_into_novel_adapter():
    from agent_runtime.workflows.novel.workflow import adapt_novel_request

    resources = {
        "style_snapshot": {"payload": {"syntax": ["Frozen style"]}},
        "creative_template_snapshot": {"payload": {"themes": ["Frozen theme"]}},
        "quality_policy_snapshot": {"payload": {"gate_threshold": 42}},
        "content_type_snapshot": {"payload": {"kind": "novel"}},
    }
    request = adapt_novel_request(
        {
            "project_id": "project",
            "title": "Common project",
            "brief_version": 3,
            "input": {"project_id": "project", "block_count": 1, "inspiration": "story"},
            "brief": {"themes": ["Edited brief"]},
            "frozen_resources": resources,
        }
    )
    inputs = request["novel_input"]
    assert inputs["creative_brief"] == {"themes": ["Edited brief"]}
    assert inputs["creative_brief_version"] == 3
    assert inputs["title"] == "Common project"
    assert "project_id" not in inputs
    assert inputs["total_chapters"] == 1
    assert all(inputs[key] == value for key, value in resources.items())
