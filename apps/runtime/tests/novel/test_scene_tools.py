"""场景正文边界工具测试。"""

from agent_runtime.workflows.novel.scenes import (
    ensure_scene_drafts,
    format_scene_drafts,
    join_scene_drafts,
    segment_scene_content,
)


def _plan() -> list[dict]:
    return [
        {"scene_number": 1, "estimated_words": 40},
        {"scene_number": 2, "estimated_words": 60},
    ]


def test_scene_markers_roundtrip_without_leaking_into_reader_content():
    raw = "<<<SCENE:1>>>\n第一场。\n\n<<<SCENE:2>>>\n第二场。"

    drafts = segment_scene_content(raw, _plan())

    assert drafts == [
        {"scene_number": 1, "content": "第一场。"},
        {"scene_number": 2, "content": "第二场。"},
    ]
    assert format_scene_drafts(drafts) == raw
    assert join_scene_drafts(drafts) == "第一场。\n\n第二场。"


def test_legacy_unmarked_content_gets_deterministic_scene_boundaries():
    drafts = segment_scene_content("甲" * 40 + "乙" * 60, _plan())

    assert [len(item["content"]) for item in drafts] == [40, 60]
    assert join_scene_drafts(drafts).replace("\n", "") == "甲" * 40 + "乙" * 60


def test_invalid_saved_scene_drafts_are_rebuilt_from_content():
    drafts = ensure_scene_drafts(
        {
            "content": "甲" * 40 + "乙" * 60,
            "scene_plan": _plan(),
            "scene_drafts": [{"scene_number": 1, "content": "不完整"}],
        }
    )

    assert [item["scene_number"] for item in drafts] == [1, 2]
