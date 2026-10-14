"""全书文学终审 Agent。"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.execution.models import get_analyzer_llm
from agent_runtime.execution.structured import invoke_structured, parse_json_block
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.analysis.book import BOOK_AUDIT_RUBRIC_VERSION
from agent_runtime.workflows.novel.memory.canon import format_canon
from agent_runtime.workflows.novel.memory.hierarchical import format_hierarchical_memory
from agent_runtime.workflows.novel.resources.brief import format_creative_brief

BOOK_AUDIT_DIMENSIONS = {
    "plot_coherence",
    "character_arc",
    "theme_payoff",
    "style_consistency",
    "ending_satisfaction",
    "unresolved_promises",
}


class BookAuditorAgent:
    """在全书完结时评估跨章结构与结局兑现。"""

    def __init__(self, llm: BaseChatModel | None = None):
        self.llm = llm or get_analyzer_llm()

    async def evaluate(
        self,
        *,
        novel: dict[str, Any],
        chapters: list[dict[str, Any]],
        canon: dict[str, Any],
        deterministic_report: dict[str, Any],
        memory_index: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        chapter_context = []
        for chapter in chapters:
            content = str(chapter.get("content", ""))
            chapter_context.append(
                f"第{chapter.get('chapter_number', chapter.get('chapter', '?'))}章 "
                f"《{chapter.get('title', '')}》\n"
                f"摘要:{chapter.get('summary', '')}\n"
                f"开头:{content[:300]}\n结尾:{content[-300:]}"
            )
        prompt = fill_template(
            "book_auditor",
            rubric_version=BOOK_AUDIT_RUBRIC_VERSION,
            novel_title=novel.get("title", ""),
            genre=novel.get("genre", ""),
            style=novel.get("style", ""),
            inspiration=novel.get("inspiration", ""),
            creative_brief=format_creative_brief(novel.get("creative_brief")),
            canon_context=format_canon(canon, max_chars=6000),
            deterministic_report=repr(deterministic_report),
            hierarchical_memory=format_hierarchical_memory(
                memory_index,
                current_chapter=0,
                max_chars=6000,
            ),
            chapter_context="\n\n".join(chapter_context)[:18000],
        )

        def validate(items: list[dict[str, Any]]) -> None:
            if len(items) != 1:
                raise ValueError("必须只返回一个全书审计对象")
            item = items[0]
            scores = item.get("scores")
            if not isinstance(scores, dict) or set(scores) != BOOK_AUDIT_DIMENSIONS:
                raise ValueError("全书审计 scores 维度不完整")
            for name, value in scores.items():
                if not isinstance(value, (int, float)) or not 0 <= float(value) <= 100:
                    raise ValueError(f"{name} 必须是 0-100 分")
            for field in ("findings", "revision_priorities"):
                values = item.get(field)
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise ValueError(f"{field} 必须是字符串列表")

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
            "rubric_version": BOOK_AUDIT_RUBRIC_VERSION,
            "scores": {name: round(float(value), 1) for name, value in result["scores"].items()},
            "findings": [str(item)[:500] for item in result["findings"][:12]],
            "revision_priorities": [str(item)[:500] for item in result["revision_priorities"][:8]],
        }
