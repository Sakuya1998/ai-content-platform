"""Cross-service bootstrap precedence for placeholder imported state."""

import hashlib

from agent_runtime.execution.context import _EXECUTION
from agent_runtime.execution.errors import WaitingReview
from agent_runtime.infrastructure.storage.artifacts import ArtifactReference

pytest_plugins = ["tests.infrastructure.test_novel_runtime"]


async def test_placeholder_import_does_not_blank_frozen_inputs_or_require_second_approval(
    novel_store,
    monkeypatch,
    fake_llm,
):
    from agent_runtime.workflows.novel import workflow as novel_workflow
    from tests.novel.test_graph_flow import _patch_llms

    _patch_llms(monkeypatch, fake_llm)

    class Artifacts:
        def __init__(self, settings):
            pass

        def upload(self, *args, **kwargs):
            data = args[5]
            return ArtifactReference(
                "test", "object", hashlib.sha256(data).hexdigest(), len(data), "application/json", ""
            )

    class Models:
        def __init__(self, settings):
            pass

        async def embed(self, texts):
            return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr(novel_workflow, "S3ArtifactStore", Artifacts)
    monkeypatch.setattr(novel_workflow, "RuntimeModels", Models)
    command = novel_store.execution.job.command
    snapshots = {
        "content_type_snapshot": {"payload": {"tags": ["fiction"]}},
        "style_snapshot": {"name": "Custom", "payload": {"syntax": ["short sentences"]}},
        "creative_template_snapshot": {"payload": {"themes": ["memory"]}},
        "quality_policy_snapshot": {
            "payload": {
                "gate_threshold": 42,
                "regression_threshold": 3,
                "dimensions": [
                    "plot_coherence",
                    "character_arc",
                    "theme_payoff",
                    "style_consistency",
                    "ending_satisfaction",
                    "unresolved_promises",
                ],
            }
        },
    }
    request = {
        "project_id": command.project_id,
        "document_type": "novel",
        "document_id": "book",
        "title": "Test",
        "objective": "story",
        "operation": "run",
        "input": {
            "genre": "fiction",
            "style": "gu_long",
            "total_chapters": 1,
        },
        "brief": {},
        "brief_version": 3,
        "frozen_resources": snapshots,
        "imported_state": {
            "novel_id": command.project_id,
            "creative_brief": {},
            "creative_brief_version": 1,
            **{key: {} for key in snapshots},
        },
    }
    token = _EXECUTION.set(novel_store.execution)
    try:
        result = await novel_workflow.run_novel(command, {"request": request})
        assert isinstance(result, WaitingReview)
        assert result["review"]["type"] == "human_review"
        assert novel_store.state()["creative_brief_version"] == 3
        assert all(novel_store.state()[key] == value for key, value in snapshots.items())
        novel_store.execution.job.resume_input = {"action": "approve"}
        result = await novel_workflow.run_novel(command, {"request": request})
        assert not isinstance(result, WaitingReview)
        assert novel_store.state()["book_audit_completed"]
    finally:
        _EXECUTION.reset(token)


def test_frozen_quality_policy_rejects_incompatible_generated_dimensions():
    import pytest

    from agent_runtime.workflows.novel.workflow import _effective_resources

    with pytest.raises(ValueError, match="dimensions"):
        _effective_resources(
            {"quality_policy_snapshot": {"payload": {"gate_threshold": 42, "dimensions": ["arbitrary"]}}}
        )
