"""NovelState 初始化测试。"""

from agent_runtime.workflows.novel.graph.nodes import orchestrator_node
from agent_runtime.workflows.novel.graph.state import create_initial_state, merge_chapters
from agent_runtime.workflows.novel.resources.settings import GenerationSettings


def test_initial_state_uses_generation_config(tmp_path):
    cfg = GenerationSettings(
        max_chapter_words=2345,
        max_revision_attempts=4,
    )

    state = create_initial_state(
        novel_id="novel_1",
        title="雾中剑",
        genre="武侠",
        inspiration="失忆剑客",
        total_chapters=3,
        style="gu_long",
        config=cfg,
    )

    assert state["max_chapter_words"] == 2345
    assert state["max_revision_attempts"] == 4
    assert state["revision_count"] == 0
    assert state["revision_notes"] == ""
    assert state["revision_scene_number"] == 0
    assert state["scene_plan"] == []
    assert state["planning_review_enabled"] is False
    assert state["creative_brief"]["schema_version"] == "creative-brief-v1"
    assert state["creative_brief"]["point_of_view"] == "third_limited"
    assert state["creative_brief_version"] == 1
    assert state["creative_brief_review_required"] is False
    assert state["chapters"] == []
    assert state["book_revision_mode"] is False
    assert state["canon"] == {
        "version": 3,
        "world_facts": [],
        "characters": {},
        "aliases": {},
        "timeline": [],
        "facts": [],
        "narrative_threads": [],
        "audit": [],
    }


def test_initial_state_normalizes_creative_brief():
    state = create_initial_state(
        novel_id="novel_1",
        title="雾中剑",
        genre="悬疑",
        inspiration="失忆剑客",
        total_chapters=3,
        style="gu_long",
        creative_brief={
            "target_audience": "推理读者",
            "point_of_view": "first_person",
            "intensity": {"mystery": 5, "darkness": 9},
        },
    )

    assert state["creative_brief"]["target_audience"] == "推理读者"
    assert state["creative_brief"]["point_of_view"] == "first_person"
    assert state["creative_brief"]["intensity"]["mystery"] == 5
    assert state["creative_brief"]["intensity"]["darkness"] == 5


async def test_orchestrator_rebuilds_canon_for_legacy_checkpoint():
    updates = await orchestrator_node(
        {
            "world_bible": "城市: 雾都",
            "characters": [{"name": "林寒", "role": "主角"}],
            "outline": [{"chapter": 1, "title": "雾起", "summary": "入城"}],
            "chapters": [],
            "current_chapter": 1,
            "current_phase": "writing",
            "total_chapters": 1,
        }
    )

    assert updates["next_agent"] == "scene_planner"
    assert updates["canon"]["characters"]["林寒"]["role"] == "主角"
    assert updates["canon"]["timeline"][0]["status"] == "planned"


def test_merge_chapters_replaces_a_finalized_chapter_without_duplicates():
    chapters = [
        {"chapter_number": 1, "content": "第一章"},
        {"chapter_number": 2, "content": "第二章旧稿"},
        {"chapter_number": 3, "content": "第三章"},
    ]

    merged = merge_chapters(chapters, [{"chapter_number": 2, "content": "第二章返修稿"}])

    assert [item["chapter_number"] for item in merged] == [1, 2, 3]
    assert merged[1]["content"] == "第二章返修稿"
