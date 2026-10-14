"""LangGraph 图构建(文档 4.3)。

拓扑:
    entry → orchestrator ─(星型调度)→ world_builder / character_designer /
              plot_planner / scene_planner / scene_writer / style_editor ─→ 回 orchestrator
    scene_planner → scene_writer → style_editor → consistency_checker
    consistency_checker ─(high 问题未超限)→ scene_writer   [回写循环]
                      └─(通过)→ human_review ─(整章意见)→ scene_writer
                                            ├─(场景意见)→ scene_rewriter → consistency_checker
                                            └─(approve)→ orchestrator → book_auditor → END

人工审查节点使用 langgraph.types.interrupt 暂停,配合 checkpointer 断点续跑。
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_runtime.workflows.novel.graph import nodes
from agent_runtime.workflows.novel.graph.edges import (
    route_after_plot_planner,
    route_after_scene_planner,
    route_from_consistency,
    route_from_human,
    route_from_orchestrator,
)
from agent_runtime.workflows.novel.graph.state import NovelState


def build_graph(checkpointer=None) -> CompiledStateGraph:
    """构建并编译小说创作状态机。

    Args:
        checkpointer: LangGraph 检查点器(文档 9.1 传入 MemorySaver;
        生产使用 NovelPostgresSaver 实现持久化断点续跑)。默认每次新建 MemorySaver 供离线测试。

    Returns:
        编译后的图,支持 invoke / stream / astream_events 与 interrupt-resume。
    """
    workflow = StateGraph(NovelState)

    workflow.add_node("orchestrator", nodes.orchestrator_node)
    workflow.add_node("world_builder", nodes.world_builder_node)
    workflow.add_node("character_designer", nodes.character_designer_node)
    workflow.add_node("plot_planner", nodes.plot_planner_node)
    workflow.add_node("blueprint_review", nodes.blueprint_review_node)
    workflow.add_node("scene_planner", nodes.scene_planner_node)
    workflow.add_node("scene_review", nodes.scene_review_node)
    workflow.add_node("scene_writer", nodes.scene_writer_node)
    workflow.add_node("scene_rewriter", nodes.scene_rewriter_node)
    workflow.add_node("style_editor", nodes.style_editor_node)
    workflow.add_node("consistency_checker", nodes.consistency_checker_node)
    workflow.add_node("human_review", nodes.human_review_node)
    workflow.add_node("book_auditor", nodes.book_auditor_node)

    workflow.set_entry_point("orchestrator")

    # 星型调度:主控 → 各专业节点 / END;各专业节点完成后回主控
    workflow.add_conditional_edges(
        "orchestrator",
        route_from_orchestrator,
        {
            "world_builder": "world_builder",
            "character_designer": "character_designer",
            "plot_planner": "plot_planner",
            "scene_planner": "scene_planner",
            "scene_writer": "scene_writer",
            "style_editor": "style_editor",
            "consistency_checker": "consistency_checker",
            "book_auditor": "book_auditor",
            "end": END,
        },
    )
    workflow.add_edge("world_builder", "orchestrator")
    workflow.add_edge("character_designer", "orchestrator")
    workflow.add_conditional_edges(
        "plot_planner",
        route_after_plot_planner,
        {"blueprint_review": "blueprint_review", "orchestrator": "orchestrator"},
    )
    workflow.add_edge("blueprint_review", "orchestrator")

    # 章节创作流水线(线性推进 + 条件回写)
    workflow.add_conditional_edges(
        "scene_planner",
        route_after_scene_planner,
        {"scene_review": "scene_review", "scene_writer": "scene_writer"},
    )
    workflow.add_edge("scene_review", "scene_writer")
    workflow.add_edge("scene_writer", "style_editor")
    workflow.add_edge("style_editor", "consistency_checker")
    workflow.add_conditional_edges(
        "consistency_checker",
        route_from_consistency,
        {"scene_writer": "scene_writer", "human_review": "human_review"},
    )
    workflow.add_conditional_edges(
        "human_review",
        route_from_human,
        {
            "human_review": "human_review",
            "consistency_checker": "consistency_checker",
            "scene_rewriter": "scene_rewriter",
            "scene_writer": "scene_writer",
            "orchestrator": "orchestrator",
        },
    )
    workflow.add_edge("scene_rewriter", "consistency_checker")
    workflow.add_edge("book_auditor", END)

    return workflow.compile(checkpointer=checkpointer or MemorySaver())


# 模块级默认图实例(文档 9.1: graph = builder(checkpointer=memory))
graph = build_graph()
