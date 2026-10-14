"""章节质量评测纯函数与评审 Agent 测试。"""

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agent_runtime.workflows.novel.agents.quality_evaluator import QualityEvaluatorAgent
from agent_runtime.workflows.novel.analysis.evaluation import (
    compare_evaluations,
    evaluate_chapter_deterministic,
    quality_gate_result,
)


def test_deterministic_evaluation_scores_structure_and_scene_coverage():
    result = evaluate_chapter_deterministic(
        {
            "content": "第一段。\n\n第二段推进冲突。\n\n第三段留下悬念。" * 12,
            "summary": "主角入城并发现追兵。",
            "scene_plan": [
                {"scene_number": 1, "estimated_words": 100},
                {"scene_number": 2, "estimated_words": 100},
            ],
            "scene_drafts": [
                {"scene_number": 1, "content": "第一场"},
                {"scene_number": 2, "content": "第二场"},
            ],
        }
    )

    assert result["schema_version"] == "chapter-quality-v1"
    assert result["scores"]["scene_coverage"] == 100
    assert result["scores"]["structure"] == 100
    assert 0 <= result["overall_score"] <= 100


def test_deterministic_evaluation_penalizes_repetition_and_consistency_issues():
    clean = evaluate_chapter_deterministic({"content": "甲。\n\n乙。\n\n丙。"})
    repeated = evaluate_chapter_deterministic(
        {"content": "同一句。\n\n同一句。\n\n同一句。"},
        issues=[{"severity": "high"}, {"severity": "medium"}],
    )

    assert repeated["scores"]["repetition_control"] < clean["scores"]["repetition_control"]
    assert repeated["scores"]["consistency"] == 63


def test_compare_evaluations_classifies_regression():
    comparison = compare_evaluations(
        {"id": 1, "version_number": 1, "overall_score": 82, "deterministic_scores": {"structure": 90}},
        {"id": 2, "version_number": 2, "overall_score": 75, "deterministic_scores": {"structure": 80}},
        regression_threshold=3,
    )

    assert comparison["status"] == "regressed"
    assert comparison["overall_delta"] == -7
    assert comparison["dimensions"]["structure"]["delta"] == -10


def test_quality_gate_builds_targeted_revision_notes_for_low_scores():
    report = evaluate_chapter_deterministic({"content": "", "summary": ""})

    gate = quality_gate_result(report, threshold=70)

    assert gate["passed"] is False
    assert gate["threshold"] == 70
    assert "自动质量门未通过" in gate["revision_notes"]
    assert "structure" in gate["revision_notes"]


def test_quality_gate_passes_complete_chapter():
    content = (
        "第一段介绍主角进入陌生城市并察觉街道上的异常目光。\n\n"
        "第二段让追踪者现身，主角借市场的人群改变路线并取得线索。\n\n"
        "第三段揭示线索指向旧宅，同时以一封署名未知的信留下悬念。"
    )
    report = evaluate_chapter_deterministic(
        {
            "content": content,
            "summary": "完整摘要",
            "scene_plan": [{"scene_number": 1, "estimated_words": len(content)}],
            "scene_drafts": [{"scene_number": 1, "content": "完整场景"}],
        }
    )

    gate = quality_gate_result(report, threshold=70)

    assert gate["passed"] is True
    assert gate["revision_notes"] == ""


def test_chapter_evaluation_uses_frozen_weights_and_critical_parameters():
    from agent_runtime.domain.policies import CreationPolicyVersion, policy_hash
    from agent_runtime.execution.context import _EXECUTION, ExecutionContext
    from tests.fixtures.policies import policy_payload

    payload = policy_payload()
    payload["evaluation"].update(
        gate_threshold=85,
        critical_threshold=30,
        rules=[
            {"key": "text.length@1", "dimension": "length", "weight": 3, "critical": False, "parameters": {}},
            {
                "key": "text.consistency@1",
                "dimension": "continuity",
                "weight": 1,
                "critical": True,
                "parameters": {"high": 80},
            },
        ],
    )
    version = CreationPolicyVersion.model_validate(
        {
            "version_id": "frozen-policy",
            "key": "novel",
            "status": "published",
            "content_hash": policy_hash(payload),
            "payload": payload,
        }
    )
    token = _EXECUTION.set(ExecutionContext(job=None, store=None, settings=None, creation_policy=version))
    try:
        report = evaluate_chapter_deterministic(
            {"content": "四个字符", "scene_plan": [{"estimated_words": 4}]},
            issues=[{"severity": "high"}],
        )
        gate = quality_gate_result(report)
        override_gate = quality_gate_result(report, threshold=60)
    finally:
        _EXECUTION.reset(token)

    assert report["overall_score"] == 80
    assert report["scores"] == {"length": 100, "continuity": 20}
    assert report["creation_policy"]["content_hash"] == version.content_hash
    assert gate["threshold"] == 85 and gate["critical_failures"] == ["continuity"]
    assert not override_gate["passed"] and override_gate["threshold"] == 60


def test_novel_check_penalties_are_policy_parameters():
    from agent_runtime.domain.policies import CreationPolicyVersion, policy_hash
    from agent_runtime.execution.context import _EXECUTION, ExecutionContext
    from tests.fixtures.policies import policy_payload

    payload = policy_payload()
    payload["evaluation"]["rules"] = [
        {
            "key": "novel.scenes@1",
            "dimension": "scenes",
            "weight": 1,
            "critical": False,
            "parameters": {"duplicate_penalty": 40},
        },
        {
            "key": "novel.narrative@1",
            "dimension": "beats",
            "weight": 1,
            "critical": False,
            "parameters": {"invalid_penalty": 50, "duplicate_penalty": 40},
        },
    ]
    version = CreationPolicyVersion.model_validate(
        {
            "version_id": "frozen-policy",
            "key": "novel",
            "status": "published",
            "content_hash": policy_hash(payload),
            "payload": payload,
        }
    )
    token = _EXECUTION.set(ExecutionContext(job=None, store=None, settings=None, creation_policy=version))
    try:
        report = evaluate_chapter_deterministic(
            {
                "content": "正文",
                "scene_plan": [{"scene_number": 1, "narrative_beats": [{"thread": "", "action": "bad"}]}],
                "scene_drafts": [{"scene_number": 1, "content": "场景"}, {"scene_number": 1, "content": "重复"}],
            }
        )
    finally:
        _EXECUTION.reset(token)
    assert report["scores"] == {"scenes": 60, "beats": 50}


async def test_quality_evaluator_returns_valid_fixed_rubric():
    llm = FakeListChatModel(
        responses=[
            """{
      "scores": {
        "coherence": 80,
        "character_consistency": 81,
        "prose_style": 82,
        "pacing": 83,
        "scene_execution": 84,
        "narrative_payoff": 85
      },
      "findings": ["结尾推动力清晰"]
    }"""
        ]
    )
    result = await QualityEvaluatorAgent(llm).evaluate(
        novel={"title": "书", "genre": "武侠", "style": "gu_long"},
        chapter={"content": "正文", "scene_plan": []},
        previous_chapters=[],
        deterministic_report={},
    )

    assert result["rubric_version"] == "literary-judge-v1"
    assert result["scores"]["narrative_payoff"] == 85
    assert result["findings"] == ["结尾推动力清晰"]
