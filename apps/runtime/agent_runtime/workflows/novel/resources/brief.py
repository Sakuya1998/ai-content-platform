"""Structured creative intent shared by planning, writing, and review agents."""

from typing import Any

CREATIVE_BRIEF_SCHEMA_VERSION = "creative-brief-v1"

_ENUM_DEFAULTS = {
    "age_rating": "teen",
    "point_of_view": "third_limited",
    "narrative_tense": "past",
    "narrative_distance": "medium",
    "ending_tone": "unspecified",
}

_ENUM_VALUES = {
    "age_rating": {"all_ages", "teen", "mature"},
    "point_of_view": {
        "first_person",
        "third_limited",
        "third_omniscient",
        "multiple",
    },
    "narrative_tense": {"past", "present", "mixed"},
    "narrative_distance": {"close", "medium", "distant"},
    "ending_tone": {"unspecified", "hopeful", "bittersweet", "tragic", "open"},
}

_LIST_LIMITS = {
    "themes": 8,
    "must_include": 12,
    "avoid_content": 12,
}

_INTENSITY_KEYS = ("romance", "mystery", "action", "darkness")

_LABELS = {
    "age_rating": {
        "all_ages": "全年龄",
        "teen": "青少年及以上",
        "mature": "成人向",
    },
    "point_of_view": {
        "first_person": "第一人称",
        "third_limited": "第三人称限知",
        "third_omniscient": "第三人称全知",
        "multiple": "多视角",
    },
    "narrative_tense": {
        "past": "过去时叙述",
        "present": "现在时叙述",
        "mixed": "混合时态",
    },
    "narrative_distance": {
        "close": "贴近人物内心",
        "medium": "中等叙事距离",
        "distant": "疏离客观",
    },
    "ending_tone": {
        "unspecified": "不限定",
        "hopeful": "希望感",
        "bittersweet": "苦乐参半",
        "tragic": "悲剧",
        "open": "开放式",
    },
}


def empty_creative_brief() -> dict[str, Any]:
    from agent_runtime.execution.policies import current_creation_policy

    if policy := current_creation_policy():
        return dict(policy.constraints.defaults)
    return {
        "schema_version": CREATIVE_BRIEF_SCHEMA_VERSION,
        "target_audience": "大众类型小说读者",
        **_ENUM_DEFAULTS,
        "themes": [],
        "must_include": [],
        "avoid_content": [],
        "intensity": {key: 2 for key in _INTENSITY_KEYS},
        "notes": "",
    }


def _clean_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = " ".join(str(item).strip().split())[:200]
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def normalize_creative_brief(value: Any) -> dict[str, Any]:
    """Return a stable, bounded brief for storage and prompt injection."""
    source = value if isinstance(value, dict) else {}
    from agent_runtime.domain.constraints import normalize_constraints
    from agent_runtime.execution.policies import current_creation_policy

    if policy := current_creation_policy():
        return normalize_constraints(source, policy.constraints)
    normalized = empty_creative_brief()
    audience = " ".join(str(source.get("target_audience", "")).strip().split())
    if audience:
        normalized["target_audience"] = audience[:200]
    for field, fallback in _ENUM_DEFAULTS.items():
        candidate = str(source.get(field, "")).strip().casefold()
        normalized[field] = candidate if candidate in _ENUM_VALUES[field] else fallback
    for field, limit in _LIST_LIMITS.items():
        normalized[field] = _clean_list(source.get(field), limit)
    raw_intensity = source.get("intensity")
    intensity = raw_intensity if isinstance(raw_intensity, dict) else {}
    normalized["intensity"] = {}
    for key in _INTENSITY_KEYS:
        try:
            score = int(intensity.get(key, 2))
        except TypeError, ValueError:
            score = 2
        normalized["intensity"][key] = max(0, min(5, score))
    normalized["notes"] = str(source.get("notes", "")).strip()[:2000]
    return normalized


def format_creative_brief(value: Any, *, max_chars: int = 3500) -> str:
    """Format the brief as explicit, compact model instructions."""
    brief = normalize_creative_brief(value)
    from agent_runtime.domain.constraints import format_constraints
    from agent_runtime.execution.policies import current_creation_policy

    if policy := current_creation_policy():
        return format_constraints(brief, policy.constraints)
    intensity = brief["intensity"]
    lines = [
        "## 创作约束（必须遵守）",
        f"- 目标读者：{brief['target_audience']}",
        f"- 内容分级：{_LABELS['age_rating'][brief['age_rating']]}",
        f"- 叙事视角：{_LABELS['point_of_view'][brief['point_of_view']]}",
        f"- 叙事时态：{_LABELS['narrative_tense'][brief['narrative_tense']]}",
        f"- 叙事距离：{_LABELS['narrative_distance'][brief['narrative_distance']]}",
        f"- 结局基调：{_LABELS['ending_tone'][brief['ending_tone']]}",
        "- 类型强度（0-5）："
        f"感情 {intensity['romance']} / 悬疑 {intensity['mystery']} / "
        f"动作 {intensity['action']} / 黑暗 {intensity['darkness']}",
    ]
    if brief["themes"]:
        lines.append("- 核心主题：" + "；".join(brief["themes"]))
    if brief["must_include"]:
        lines.append("- 必须包含：" + "；".join(brief["must_include"]))
    if brief["avoid_content"]:
        lines.append("- 禁止或回避：" + "；".join(brief["avoid_content"]))
    if brief["notes"]:
        lines.append("- 补充说明：" + brief["notes"])
    return "\n".join(lines)[:max_chars]
