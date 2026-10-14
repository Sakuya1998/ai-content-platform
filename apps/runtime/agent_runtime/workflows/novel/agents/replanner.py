"""定稿后的未来章节重规划 Agent。"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.execution.models import get_analyzer_llm
from agent_runtime.execution.structured import invoke_structured, parse_json_block
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.agents.plot_planner import validate_outline
from agent_runtime.workflows.novel.resources.brief import format_creative_brief

REPLAN_VERSION = "replan-v1"
_IMPACTS = {"low", "medium", "high"}
_STATUSES = {"stable", "replanned"}


def merge_future_outline(
    outline: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    *,
    current_chapter: int,
    total_chapters: int,
) -> list[dict[str, Any]]:
    """只合并未来章节补丁，已完成章节和章节覆盖不可被模型改写。"""
    original = {
        int(item.get("chapter", 0) or 0): dict(item) for item in outline if int(item.get("chapter", 0) or 0) > 0
    }
    patch: dict[int, dict[str, Any]] = {}
    for item in updates:
        if not isinstance(item, dict):
            raise ValueError("重规划章节补丁必须是对象")
        chapter = int(item.get("chapter", 0) or 0)
        if chapter <= current_chapter or chapter > total_chapters:
            raise ValueError("重规划只能修改当前章之后且不超过全书范围的章节")
        if chapter in patch:
            raise ValueError(f"重规划章节补丁重复:{chapter}")
        patch[chapter] = dict(item)

    merged: list[dict[str, Any]] = []
    for chapter in range(1, total_chapters + 1):
        existing = original.get(chapter)
        if existing is None:
            raise ValueError(f"原大纲缺少第 {chapter} 章")
        merged.append({**existing, **patch.get(chapter, {})})
    validate_outline(merged, total_chapters)
    return merged


class ReplannerAgent:
    """根据实际终稿和原计划判断是否需要调整未来章节。"""

    def __init__(self, llm: BaseChatModel | None = None):
        self.llm = llm or get_analyzer_llm()

    async def analyze(
        self,
        *,
        current_chapter: int,
        total_chapters: int,
        chapter_plan: dict[str, Any],
        chapter_digest: dict[str, Any],
        future_outline: list[dict[str, Any]],
        creative_brief: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not future_outline:
            return {
                "status": "stable",
                "impact": "low",
                "rationale": "已是最终章，没有需要重规划的后续章节。",
                "outline_updates": [],
                "replan_version": REPLAN_VERSION,
            }
        prompt = fill_template(
            "replanner",
            current_chapter=current_chapter,
            total_chapters=total_chapters,
            chapter_plan=repr(chapter_plan),
            chapter_digest=repr(chapter_digest),
            future_outline=repr(future_outline),
            creative_brief=format_creative_brief(creative_brief),
        )

        def validate(items: list[dict[str, Any]]) -> None:
            if len(items) != 1:
                raise ValueError("必须只返回一个重规划对象")
            item = items[0]
            status = str(item.get("status", "")).strip().casefold()
            impact = str(item.get("impact", "")).strip().casefold()
            if status not in _STATUSES:
                raise ValueError("status 必须是 stable 或 replanned")
            if impact not in _IMPACTS:
                raise ValueError("impact 必须是 low、medium 或 high")
            if not str(item.get("rationale", "")).strip():
                raise ValueError("rationale 不能为空")
            updates = item.get("outline_updates")
            if not isinstance(updates, list):
                raise ValueError("outline_updates 必须是列表")
            if status == "stable" and updates:
                raise ValueError("stable 状态不能包含章节补丁")
            future_numbers = {int(entry.get("chapter", 0) or 0) for entry in future_outline}
            for update in updates:
                if not isinstance(update, dict):
                    raise ValueError("章节补丁必须是对象")
                number = int(update.get("chapter", 0) or 0)
                if number not in future_numbers:
                    raise ValueError(f"章节补丁不在未来大纲中:{number}")

        _, items = await invoke_structured(
            self.llm,
            prompt,
            parser=parse_json_block,
            validator=validate,
            agent_name=type(self).__name__,
            format_name="JSON",
        )
        result = items[0]
        return {
            "status": str(result["status"]).strip().casefold(),
            "impact": str(result["impact"]).strip().casefold(),
            "rationale": str(result["rationale"]).strip()[:1000],
            "outline_updates": [dict(item) for item in result.get("outline_updates") or []],
            "replan_version": REPLAN_VERSION,
        }
