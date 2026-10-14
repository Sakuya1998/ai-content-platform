"""条件边路由(文档 4.3)。

路由决策全部基于状态字段的确定性规则:
- route_from_orchestrator:星型调度中枢 → 专业节点 / END
- route_from_consistency:质检回写 or 人工审查
- route_from_human:人工反馈 → 定稿/回写/END
"""

from agent_runtime.workflows.novel.graph.state import NovelState


def route_from_orchestrator(state: NovelState) -> str:
    """主控决策路由:直接翻译 next_agent。"""
    next_agent = state.get("next_agent", "scene_planner")
    if next_agent == "END":
        return "end"
    return next_agent


def route_from_consistency(state: NovelState) -> str:
    """一致性检查后:存在 high 问题且未超重写上限 → 回 scene_writer;否则人工审查。

    判定依据:consistency_checker_node 将需要重写时置 current_phase=writing;
    通过时保持 consistency_check。
    """
    return "scene_writer" if state.get("current_phase") == "writing" else "human_review"


def route_after_plot_planner(state: NovelState) -> str:
    """大纲生成后按作品设置决定是否暂停审阅蓝图。"""
    return "blueprint_review" if state.get("planning_review_enabled", False) else "orchestrator"


def route_after_scene_planner(state: NovelState) -> str:
    """分镜生成后按作品设置决定是否暂停审阅当前章场景。"""
    return "scene_review" if state.get("planning_review_enabled", False) else "scene_writer"


def route_from_human(state: NovelState) -> str:
    """人工审查后:进入下一章(回主控决策,含 END 判断)或回写重写。

    判定依据:human_review_node 通过时推进 current_chapter 并置 phase=writing;
    给出修改意见时同样置 phase=writing 但章号不变。
    区分:revision_notes 非空 → 回写;为空 → 已定稿推进 → 回 orchestrator。
    """
    if state.get("persistence_error"):
        return "human_review"
    if state.get("current_phase") == "consistency_check":
        return "consistency_checker"
    if int(state.get("revision_scene_number", 0) or 0) > 0:
        return "scene_rewriter"
    if state.get("revision_notes"):
        return "scene_writer"
    return "orchestrator"
