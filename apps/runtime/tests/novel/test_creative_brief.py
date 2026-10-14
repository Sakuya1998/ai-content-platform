"""Structured creative brief normalization and formatting tests."""

from agent_runtime.workflows.novel.analysis.consistency import build_consistency_diagnostics
from agent_runtime.workflows.novel.resources.brief import (
    CREATIVE_BRIEF_SCHEMA_VERSION,
    empty_creative_brief,
    format_creative_brief,
    normalize_creative_brief,
)


def test_empty_creative_brief_has_stable_defaults():
    brief = empty_creative_brief()

    assert brief["schema_version"] == CREATIVE_BRIEF_SCHEMA_VERSION
    assert brief["age_rating"] == "teen"
    assert brief["point_of_view"] == "third_limited"
    assert brief["intensity"] == {
        "romance": 2,
        "mystery": 2,
        "action": 2,
        "darkness": 2,
    }


def test_normalize_creative_brief_bounds_and_deduplicates_user_input():
    normalized = normalize_creative_brief(
        {
            "target_audience": "  喜欢 谜案 的读者  ",
            "age_rating": "MATURE",
            "point_of_view": "unsupported",
            "themes": ["身份", " 身份 ", "记忆"],
            "must_include": [f"线索 {index}" for index in range(20)],
            "avoid_content": "not-a-list",
            "intensity": {
                "romance": -4,
                "mystery": "5",
                "action": 12,
                "darkness": "invalid",
            },
            "notes": "x" * 2100,
            "unknown": "ignored",
        }
    )

    assert normalized["target_audience"] == "喜欢 谜案 的读者"
    assert normalized["age_rating"] == "mature"
    assert normalized["point_of_view"] == "third_limited"
    assert normalized["themes"] == ["身份", "记忆"]
    assert len(normalized["must_include"]) == 12
    assert normalized["avoid_content"] == []
    assert normalized["intensity"] == {
        "romance": 0,
        "mystery": 5,
        "action": 5,
        "darkness": 2,
    }
    assert len(normalized["notes"]) == 2000


def test_format_creative_brief_emits_explicit_model_constraints():
    formatted = format_creative_brief(
        {
            "target_audience": "硬核推理读者",
            "point_of_view": "first_person",
            "narrative_tense": "present",
            "ending_tone": "bittersweet",
            "themes": ["记忆与身份"],
            "must_include": ["公平线索"],
            "avoid_content": ["无依据反转"],
            "intensity": {"mystery": 5},
        }
    )

    assert "## 创作约束（必须遵守）" in formatted
    assert "目标读者：硬核推理读者" in formatted
    assert "叙事视角：第一人称" in formatted
    assert "叙事时态：现在时叙述" in formatted
    assert "结局基调：苦乐参半" in formatted
    assert "核心主题：记忆与身份" in formatted
    assert "必须包含：公平线索" in formatted
    assert "禁止或回避：无依据反转" in formatted
    assert "悬疑 5" in formatted


def test_creative_brief_boundaries_are_deterministically_enforced():
    issues, report = build_consistency_diagnostics(
        chapter={
            "chapter_number": 2,
            "content": "结局使用梦境解释一切，没有出现王印。",
        },
        characters=[],
        outline=[],
        previous_chapters=[{"chapter_number": 1, "content": "主角进入雾都。"}],
        total_chapters=2,
        creative_brief={
            "must_include": ["失踪王印"],
            "avoid_content": ["梦境解释一切"],
        },
    )

    assert {item["type"] for item in issues} >= {
        "creative_brief_boundary",
        "creative_brief_requirement",
    }
    assert all(item["severity"] == "high" for item in issues)
    assert "创作约束回避项命中:梦境解释一切" in report
    assert "全书必须包含项缺失:失踪王印" in report
