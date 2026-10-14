import pytest


def test_hierarchy_uses_policy_and_ir_unit_references_for_screenplay():
    from agent_runtime.domain.hierarchy import MemoryUnit, build_hierarchy
    from agent_runtime.domain.policies import MemoryPolicy

    units = [
        MemoryUnit(id=f"scene-{i}", position=i, title=f"Scene {i}", summary="Scene summary", source_version="v1")
        for i in range(5)
    ]
    first = build_hierarchy(units, MemoryPolicy(group_size=2))
    assert [group["unit_ids"] for group in first["groups"]] == [
        ["scene-0", "scene-1"],
        ["scene-2", "scene-3"],
        ["scene-4"],
    ]
    assert all(unit["source_version"] == "v1" for unit in first["units"])
    with pytest.raises(ValueError, match="duplicate"):
        build_hierarchy([units[0], units[0]], MemoryPolicy())


def test_canonical_core_requires_evidence_and_preserves_project_scope():
    from agent_runtime.domain.canon import CanonFact, CanonState, Evidence, commit_facts
    from agent_runtime.domain.policies import CanonPolicy

    state = CanonState(workspace_id="workspace", project_id="project")
    fact = CanonFact(id="fact", kind="event", subject="product", value="launched", evidence=[])
    with pytest.raises(ValueError, match="evidence"):
        commit_facts(state, [fact], CanonPolicy(), source_id="block-1", source_version="v1")
    fact = fact.model_copy(update={"evidence": [Evidence(source_id="block-1", source_version="v1")]})
    updated = commit_facts(state, [fact], CanonPolicy(), source_id="block-1", source_version="v1")
    assert updated.project_id == state.project_id and updated.version == 2
    assert updated.facts[0].evidence[0].source_version == "v1"
    assert state.facts == []


def test_generic_constraints_validate_advertisement_fields():
    from agent_runtime.domain.constraints import format_constraints, normalize_constraints
    from agent_runtime.domain.policies import ConstraintPolicy

    policy = ConstraintPolicy(
        schema={
            "type": "object",
            "properties": {"brand": {"type": "string"}, "max_words": {"type": "integer", "minimum": 1}},
            "required": ["brand", "max_words"],
            "additionalProperties": False,
        },
        defaults={"brand": "Example", "max_words": 30},
        labels={"brand": "Brand", "max_words": "Words"},
    )
    assert normalize_constraints({"brand": "ACME"}, policy) == {"brand": "ACME", "max_words": 30}
    assert "ACME" in format_constraints({"brand": "ACME"}, policy)
    with pytest.raises(ValueError, match="schema"):
        normalize_constraints({"max_words": -1}, policy)


def test_policy_validation_rejects_remote_schema_and_unknown_executable_checks():
    from agent_runtime.domain.policies import CreationPolicy
    from tests.fixtures.policies import policy_payload

    payload = policy_payload()
    payload["evaluation"]["rules"][0]["key"] = "python.eval@1"
    with pytest.raises(ValueError):
        CreationPolicy.model_validate(payload)
    payload = policy_payload()
    payload["constraints"]["schema"]["properties"]["title"] = {"$ref": "https://untrusted.example/schema"}
    with pytest.raises(ValueError, match="reference"):
        CreationPolicy.model_validate(payload)


def test_evaluation_weights_and_thresholds_are_frozen_configuration():
    from agent_runtime.domain.evaluation import evaluate, quality_gate
    from agent_runtime.domain.policies import EvaluationPolicy, EvaluationRule

    policy = EvaluationPolicy(
        rules=[
            EvaluationRule(
                key="text.length@1", dimension="length", weight=3, critical=False, parameters={"scale": 100}
            ),
            EvaluationRule(
                key="text.consistency@1", dimension="consistency", weight=1, critical=True, parameters={"high": 80}
            ),
        ]
    )
    report = evaluate({"text": "Text", "target_chars": 4, "issues": [{"severity": "high"}]}, policy)
    assert report["overall_score"] == 80
    assert quality_gate(report, policy)["critical_failures"] == ["consistency"]
