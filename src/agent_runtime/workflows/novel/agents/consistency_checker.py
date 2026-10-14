"""一致性检查 Agent(文档 3.7):设定/角色/情节/时间线/伏笔五维检查。"""

import logging
from typing import Any

from agent_runtime.execution.models import get_analyzer_llm
from agent_runtime.execution.structured import invoke_structured, parse_json_block
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.analysis.consistency import build_consistency_diagnostics
from agent_runtime.workflows.novel.memory.canon import format_canon
from agent_runtime.workflows.novel.memory.hierarchical import format_hierarchical_memory
from agent_runtime.workflows.novel.resources.brief import format_creative_brief

logger = logging.getLogger(__name__)

# 供 prompt 注入的历史摘要上限(章)
_SUMMARY_WINDOW = 5
_CONTENT_LIMIT = 8000


class ConsistencyCheckerAgent:
    """专职质检:输出结构化 issues,驱动章节回滚重写。"""

    def __init__(self, llm=None):
        self.llm = llm or get_analyzer_llm()

    async def check(
        self,
        chapter: dict[str, Any],
        world_bible: str,
        characters: list[dict[str, Any]],
        outline: list[dict[str, Any]],
        previous_chapters: list[dict[str, Any]],
        future_chapters: list[dict[str, Any]] | None = None,
        memory_index: dict[str, Any] | None = None,
        max_chapter_words: int | None = None,
        canon: dict[str, Any] | None = None,
        total_chapters: int | None = None,
        creative_brief: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """检查章节一致性。

        Returns:
            issues 列表: [{"type", "description", "chapter", "severity", "suggestion"}]
        """
        number = chapter.get("chapter_number", chapter.get("chapter", 0))

        deterministic_issues, diagnostics = build_consistency_diagnostics(
            chapter=chapter,
            characters=characters,
            outline=outline,
            previous_chapters=previous_chapters,
            max_chapter_words=max_chapter_words,
            canon=canon,
            total_chapters=total_chapters,
            creative_brief=creative_brief,
        )

        char_lines = "\n".join(
            f"- {c.get('name', '?')}({c.get('role', '?')}):{c.get('personality', '')}" for c in characters
        )
        context = (f"## 世界观圣经(摘要)\n{world_bible[:2000]}\n\n## 角色设定\n{char_lines}\n\n## 大纲\n{outline!r}")[
            :4000
        ]
        context = (
            f"{context}\n\n## 结构化 Canon\n"
            f"{format_canon(canon, max_chars=2000, current_chapter=int(number or 0))}"
            f"\n\n{format_creative_brief(creative_brief)}"
            f"\n\n## 场景执行计划\n{chapter.get('scene_plan') or []}"
            f"\n\n## 分层全书记忆\n"
            f"{format_hierarchical_memory(memory_index, current_chapter=int(number or 0), max_chars=2500)}"
            f"\n\n## 确定性分析报告\n{diagnostics}"
        )[:8000]

        summaries = (
            "\n".join(
                f"- 第{c.get('chapter_number', '?')}章:{str(c.get('summary', ''))[:200]}"
                for c in previous_chapters[-_SUMMARY_WINDOW:]
            )
            or "暂无"
        )
        future_summaries = "\n".join(
            f"- 第{c.get('chapter_number', '?')}章:{str(c.get('summary', ''))[:200]}"
            for c in (future_chapters or [])[:_SUMMARY_WINDOW]
        )
        if future_summaries:
            summaries = f"## 前序章节\n{summaries}\n\n## 后续已定稿章节\n{future_summaries}"

        prompt = fill_template(
            "consistency_checker",
            context=context,
            chapter_summaries=summaries,
            chapter_number=number,
            chapter_content=chapter.get("content", "")[:_CONTENT_LIMIT],
        )
        logger.info("ConsistencyCheckerAgent 检查第 %s 章", number)

        def validate_issues(items: list[dict]) -> None:
            allowed = {"high", "medium", "low"}
            for item in items:
                severity = str(item.get("severity", "")).lower()
                if severity not in allowed:
                    raise ValueError(f"无效 severity:{severity or '<empty>'}")

        _, issues = await invoke_structured(
            self.llm,
            prompt,
            parser=parse_json_block,
            validator=validate_issues,
            agent_name=type(self).__name__,
            format_name="JSON",
        )
        return [*deterministic_issues, *issues]
