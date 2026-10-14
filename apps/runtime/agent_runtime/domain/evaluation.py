"""Registered text checks and policy-driven aggregation shared by content types."""

import re
from collections import Counter

from agent_runtime.domain.policies import EvaluationPolicy


def clamp(value):
    return round(max(0.0, min(100.0, value)), 1)


def paragraphs(text):
    return [part for item in re.split(r"\n\s*\n|\n", text) if (part := re.sub(r"\s+", "", item).casefold())]


def length_check(value, params):
    actual = len(value.get("text", "").strip())
    target = value.get("target_chars", 0)
    score = clamp(100 - abs(actual / target - 1) * params.get("scale", 100)) if target else (100.0 if actual else 0.0)
    return score, f"Actual characters {actual}, target {target}"


def structure_check(value, params):
    text = value.get("text", "")
    parts = paragraphs(text)
    score = (50 if text.strip() else 0) + (20 if value.get("summary", "").strip() else 0)
    score += 20 if len(parts) >= params.get("min_paragraphs", 3) else (10 if parts else 0)
    score += 10 if len(text.strip()) >= params.get("min_chars", 200) else 0
    return clamp(score), f"Paragraphs {len(parts)}; summary {'present' if value.get('summary') else 'missing'}"


def repetition_check(value, params):
    text = value.get("text", "")
    parts = paragraphs(text)
    if not parts:
        return 0.0, "Empty text"
    duplicates = sum(max(count - 1, 0) for count in Counter(parts).values())
    compact = re.sub(r"\s+", "", text).casefold()
    size = params.get("window_size", 12)
    windows = [compact[index : index + size] for index in range(max(len(compact) - size + 1, 0))]
    ratio = sum(max(count - 1, 0) for count in Counter(windows).values()) / max(len(windows), 1)
    score = (
        100
        - duplicates * params.get("duplicate_penalty", 25)
        - min(ratio * params.get("window_penalty", 160), params.get("max_window_penalty", 50))
    )
    return clamp(score), f"Repeated paragraphs {duplicates}; repeated windows {ratio:.1%}"


def consistency_check(value, params):
    penalties = {"high": 25, "medium": 12, "low": 5, **params}
    counts = Counter(str(item.get("severity", "low")).lower() for item in value.get("issues", []))
    return clamp(
        100 - sum(penalties.get(level, penalties["low"]) * count for level, count in counts.items())
    ), f"Issue counts {dict(counts)}"


CHECKS = {
    "text.length@1": length_check,
    "text.structure@1": structure_check,
    "text.repetition@1": repetition_check,
    "text.consistency@1": consistency_check,
}


def evaluate(value: dict, policy: EvaluationPolicy, *, extensions=None):
    checks = {**CHECKS, **(extensions or {})}
    scores = {}
    findings = []
    for rule in policy.rules:
        if rule.key not in checks:
            raise ValueError("unsupported check for this workflow")
        score, message = checks[rule.key](value, rule.parameters)
        scores[rule.dimension] = clamp(score)
        findings.append(
            {
                "dimension": rule.dimension,
                "score": scores[rule.dimension],
                "message": message,
                "source": "deterministic",
                "evaluator_key": rule.key,
            }
        )
    total = sum(rule.weight for rule in policy.rules)
    overall = round(sum(scores[rule.dimension] * (rule.weight / total) for rule in policy.rules), 1)
    return {
        "schema_version": "evaluation@1",
        "scores": scores,
        "overall_score": overall,
        "findings": findings,
        "engine_version": policy.engine_version,
    }


def quality_gate(report: dict, policy: EvaluationPolicy):
    critical = [
        rule.dimension
        for rule in policy.rules
        if rule.critical and report["scores"].get(rule.dimension, 100) < policy.critical_threshold
    ]
    failed = report.get("overall_score", 0) < policy.gate_threshold or bool(critical)
    return {
        "status": "revise" if failed else "passed",
        "passed": not failed,
        "threshold": policy.gate_threshold,
        "critical_failures": critical,
        "revision_notes": "\n".join(
            item["message"] for item in report.get("findings", []) if item["score"] < policy.gate_threshold
        )
        if failed
        else "",
    }
