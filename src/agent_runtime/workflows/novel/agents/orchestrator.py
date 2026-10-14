"""主控 Orchestrator Agent(文档 3.1):状态机核心,决定下一个执行者。

决策逻辑(确定性规则,不耗 LLM 调用):
    世界观未建 → world_builder
    角色未建 → character_designer
    大纲未建 → plot_planner
    按阶段路由:writing → scene_planner(新章)或 scene_writer(修订) /
    style_editing → style_editor /
    consistency_check → consistency_checker
    章节全部完成 → book_auditor → END
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class OrchestratorAgent:
    """工作流调度中枢:读取全局状态,输出下一个 Agent 名。"""

    async def decide_next(self, state: dict[str, Any]) -> str:
        """根据当前状态决定下一步。

        Args:
            state: NovelState

        Returns:
            下一个节点名;完成时返回 END(由 edges 翻译为 LangGraph 终止)
        """
        current = int(state.get("current_chapter", 1))
        total = int(state.get("total_chapters", 10))

        # 阶段 1-3:设定构建流水线
        if not state.get("world_bible"):
            logger.info("Orchestrator: → world_builder")
            return "world_builder"
        if not state.get("characters"):
            logger.info("Orchestrator: → character_designer")
            return "character_designer"
        if not state.get("outline"):
            logger.info("Orchestrator: → plot_planner")
            return "plot_planner"

        # 阶段 4-6:逐章创作循环
        phase = state.get("current_phase", "writing")
        if current > total:
            if not state.get("book_audit_completed", False):
                logger.info("Orchestrator: 全书 %s 章完成 → book_auditor", total)
                return "book_auditor"
            logger.info("Orchestrator: 全书 %s 章终审完成 → END", total)
            return "END"

        scene_plan = state.get("scene_plan")
        has_scene_plan = isinstance(scene_plan, list) and bool(scene_plan)
        writing_agent = "scene_writer" if has_scene_plan else "scene_planner"
        route = {
            "writing": writing_agent,
            "style_editing": "style_editor",
            "consistency_check": "consistency_checker",
        }.get(phase, writing_agent)
        logger.info("Orchestrator: 第 %s 章 阶段=%s → %s", current, phase, route)
        return route

    async def chapter_complete(self, state: dict[str, Any]) -> bool:
        """判断全书是否完成。"""
        total = int(state.get("total_chapters", 10))
        return int(state.get("current_chapter", 1)) > total
