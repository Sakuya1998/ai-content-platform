"""编排路由逻辑测试:Orchestrator 决策 + 三条条件边。"""

import pytest

from agent_runtime.workflows.novel.agents.orchestrator import OrchestratorAgent
from agent_runtime.workflows.novel.graph.edges import route_from_consistency, route_from_human, route_from_orchestrator


@pytest.fixture
def orch() -> OrchestratorAgent:
    return OrchestratorAgent()


async def test_pipeline_setup_order(orch):
    """设定构建顺序:世界观 → 角色 → 大纲。"""
    empty = {}
    assert await orch.decide_next(empty) == "world_builder"

    assert await orch.decide_next({"world_bible": "x"}) == "character_designer"
    assert await orch.decide_next({"world_bible": "x", "characters": [{"name": "甲"}]}) == "plot_planner"


async def test_writing_loop_routing(orch):
    base = {
        "world_bible": "x",
        "characters": [{"name": "甲"}],
        "outline": [{"chapter": 1}],
        "current_chapter": 1,
        "total_chapters": 3,
    }

    assert await orch.decide_next({**base, "current_phase": "writing"}) == "scene_planner"
    assert await orch.decide_next({**base, "current_phase": "writing", "scene_plan": []}) == "scene_planner"
    assert await orch.decide_next({**base, "current_phase": "writing", "scene_plan": {}}) == "scene_planner"
    assert (
        await orch.decide_next(
            {
                **base,
                "current_phase": "writing",
                "scene_plan": {"scene_number": 1},
            }
        )
        == "scene_planner"
    )
    assert (
        await orch.decide_next(
            {
                **base,
                "current_phase": "writing",
                "scene_plan": [{"scene_number": 1}],
            }
        )
        == "scene_writer"
    )
    assert await orch.decide_next({**base, "current_phase": "style_editing"}) == "style_editor"
    assert await orch.decide_next({**base, "current_phase": "consistency_check"}) == "consistency_checker"
    # 未知阶段回退写作
    assert await orch.decide_next({**base, "current_phase": "???"}) == "scene_planner"


async def test_end_when_all_chapters_done(orch):
    done = {"world_bible": "x", "characters": [{}], "outline": [{}], "current_chapter": 4, "total_chapters": 3}
    assert await orch.decide_next(done) == "book_auditor"
    assert await orch.decide_next({**done, "book_audit_completed": True}) == "END"
    assert await orch.chapter_complete(done) is True


def test_route_from_orchestrator():
    assert route_from_orchestrator({"next_agent": "world_builder"}) == "world_builder"
    assert route_from_orchestrator({"next_agent": "END"}) == "end"
    assert route_from_orchestrator({}) == "scene_planner"  # 缺省安全回退


def test_route_from_consistency():
    # consistency_checker 节点要求重写时置 phase=writing → 回写
    assert route_from_consistency({"current_phase": "writing"}) == "scene_writer"
    assert route_from_consistency({"current_phase": "consistency_check"}) == "human_review"


def test_route_from_human():
    # 有修改意见 → 回写;已定稿(revision_notes 清空)→ 回主控推进下一章/END
    assert route_from_human({"revision_notes": "节奏太慢"}) == "scene_writer"
    assert (
        route_from_human(
            {
                "revision_notes": "加强追逐",
                "revision_scene_number": 2,
            }
        )
        == "scene_rewriter"
    )
    assert route_from_human({"current_phase": "consistency_check"}) == "consistency_checker"
    assert route_from_human({"revision_notes": ""}) == "orchestrator"
