"""章节质量评测的确定性指标与回归比较。"""

from collections import Counter
from typing import Any

from agent_runtime.domain.evaluation import clamp as _clamp
from agent_runtime.domain.evaluation import evaluate, quality_gate
from agent_runtime.domain.policies import EvaluationPolicy, EvaluationRule
from agent_runtime.execution.context import current_execution
from agent_runtime.execution.policies import current_creation_policy

DETERMINISTIC_SCHEMA_VERSION = "chapter-quality-v1"
JUDGE_RUBRIC_VERSION = "literary-judge-v1"


def _scene_check(value: dict[str, Any], parameters: dict[str, Any]) -> tuple[float, str]:
    content, scene_plan, scene_drafts = value["text"], value["scene_plan"], value["scene_drafts"]
    planned = [int(item.get("scene_number", 0) or 0) for item in scene_plan]
    if not planned:
        return (100.0 if content.strip() else 0.0), "未设置场景计划"
    completed = [int(item.get("scene_number", 0) or 0) for item in scene_drafts if str(item.get("content", "")).strip()]
    planned_counter = Counter(planned)
    completed_counter = Counter(completed)
    covered = sum(1 for number in set(planned) if completed_counter[number] > 0)
    duplicates = sum(max(count - 1, 0) for count in completed_counter.values())
    extras = sum(count for number, count in completed_counter.items() if number not in planned_counter)
    score = _clamp(
        (covered / max(len(set(planned)), 1)) * 100 - (duplicates + extras) * parameters.get("duplicate_penalty", 12.5)
    )
    return score, f"已执行 {covered}/{len(set(planned))} 个计划场景，重复 {duplicates}，额外 {extras}"


def _narrative_check(value: dict[str, Any], parameters: dict[str, Any]) -> tuple[float, str]:
    scene_plan = value["scene_plan"]
    beats = [beat for scene in scene_plan for beat in (scene.get("narrative_beats") or []) if isinstance(beat, dict)]
    if not beats:
        return 100.0, "本章没有计划叙事 beat"
    keys = [
        (
            " ".join(str(beat.get("thread", beat.get("title", ""))).casefold().split()),
            str(beat.get("action", "")).casefold(),
        )
        for beat in beats
    ]
    invalid = sum(not title or action not in {"setup", "develop", "resolve"} for title, action in keys)
    duplicates = sum(max(count - 1, 0) for count in Counter(keys).values())
    score = _clamp(
        100 - invalid * parameters.get("invalid_penalty", 25) - duplicates * parameters.get("duplicate_penalty", 20)
    )
    return score, f"共 {len(beats)} 个 beat，无效 {invalid}，重复分配 {duplicates}"


def _legacy_policy() -> EvaluationPolicy:
    """Preserve the original workflow contract for historical snapshots."""
    return EvaluationPolicy(
        rules=[
            EvaluationRule(key=key, dimension=dimension, critical=critical)
            for key, dimension, critical in [
                ("text.length@1", "length_adherence", False),
                ("text.structure@1", "structure", True),
                ("novel.scenes@1", "scene_coverage", True),
                ("novel.narrative@1", "narrative_coverage", True),
                ("text.repetition@1", "repetition_control", False),
                ("text.consistency@1", "consistency", True),
            ]
        ]
    )


def evaluate_chapter_deterministic(
    chapter: dict[str, Any],
    *,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """对任意章节快照生成完全可重算的 0-100 分指标。"""
    frozen = current_creation_policy(required=False)
    policy = frozen.evaluation if frozen else _legacy_policy()
    scene_plan = chapter.get("scene_plan") or []
    value = {
        "text": str(chapter.get("content", "")),
        "summary": str(chapter.get("summary", "")),
        "target_chars": sum(max(int(item.get("estimated_words", 0) or 0), 0) for item in scene_plan),
        "scene_plan": scene_plan,
        "scene_drafts": chapter.get("scene_drafts") or [],
        "issues": issues or [],
    }
    report = evaluate(value, policy, extensions={"novel.scenes@1": _scene_check, "novel.narrative@1": _narrative_check})
    report["schema_version"] = DETERMINISTIC_SCHEMA_VERSION
    if frozen:
        version = current_execution().creation_policy
        report["creation_policy"] = {"version_id": version.version_id, "content_hash": version.content_hash}
    return report


def quality_gate_result(
    report: dict[str, Any],
    *,
    threshold: float | None = None,
) -> dict[str, Any]:
    """把确定性评分转换为可驱动回写的质量门结论。"""
    frozen = current_creation_policy(required=False)
    policy = frozen.evaluation if frozen else _legacy_policy()
    normalized_threshold = _clamp(float(threshold if threshold is not None else policy.gate_threshold))
    policy = policy.model_copy(update={"gate_threshold": normalized_threshold})
    gate = quality_gate(report, policy)
    critical_failures = gate["critical_failures"]
    failed = not gate["passed"]
    findings = sorted(
        (item for item in report.get("findings") or [] if float(item.get("score", 100.0)) < normalized_threshold),
        key=lambda item: float(item.get("score", 100.0)),
    )
    notes = [f"- {item.get('dimension')}: {item.get('score')} 分；{item.get('message', '')}" for item in findings[:3]]
    revision_notes = ""
    if failed:
        revision_notes = (
            f"自动质量门未通过（综合 {report.get('overall_score', 0)} / "
            f"阈值 {normalized_threshold}），请只针对以下薄弱项重写：\n"
            + ("\n".join(notes) or "- 补全章节结构、场景执行和叙事兑现")
        )
    return {
        **report,
        "threshold": normalized_threshold,
        "passed": not failed,
        "critical_failures": sorted(critical_failures),
        "revision_notes": revision_notes,
    }


def combine_quality_scores(
    deterministic_scores: dict[str, float],
    judge_scores: dict[str, float] | None = None,
) -> float:
    deterministic = sum(deterministic_scores.values()) / max(len(deterministic_scores), 1)
    if not judge_scores:
        return round(deterministic, 1)
    judge = sum(judge_scores.values()) / max(len(judge_scores), 1)
    return round(deterministic * 0.5 + judge * 0.5, 1)


def compare_evaluations(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    regression_threshold: float = 3.0,
) -> dict[str, Any]:
    """比较两次评测并给出每维变化与总体回归结论。"""
    before = {**baseline.get("deterministic_scores", {}), **baseline.get("judge_scores", {})}
    after = {**candidate.get("deterministic_scores", {}), **candidate.get("judge_scores", {})}
    dimensions = {}
    for name in sorted(set(before) | set(after)):
        if name not in before or name not in after:
            continue
        delta = round(float(after[name]) - float(before[name]), 1)
        dimensions[name] = {"from": before[name], "to": after[name], "delta": delta}
    overall_delta = round(
        float(candidate.get("overall_score", 0)) - float(baseline.get("overall_score", 0)),
        1,
    )
    if overall_delta <= -abs(regression_threshold):
        status = "regressed"
    elif overall_delta >= abs(regression_threshold):
        status = "improved"
    else:
        status = "stable"
    return {
        "from_evaluation_id": baseline.get("id"),
        "to_evaluation_id": candidate.get("id"),
        "from_version": baseline.get("version_number"),
        "to_version": candidate.get("version_number"),
        "overall_delta": overall_delta,
        "status": status,
        "regression_threshold": abs(regression_threshold),
        "dimensions": dimensions,
    }
