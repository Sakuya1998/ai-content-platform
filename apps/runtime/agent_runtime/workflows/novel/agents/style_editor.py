"""风格编辑 Agent(文档 3.6):保持情节,只改风格。"""

import logging

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.execution.models import get_llm
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.resources.brief import format_creative_brief
from agent_runtime.workflows.novel.resources.styles import get_style_prompt
from agent_runtime.workflows.novel.scenes import (
    ensure_scene_drafts,
    format_scene_drafts,
    join_scene_drafts,
    segment_scene_content,
)

logger = logging.getLogger(__name__)


class StyleEditorAgent:
    """专业文体学家:按 STYLE_PROFILES 润色章节,情节零改动。"""

    def __init__(self, llm: BaseChatModel | None = None):
        self.llm = llm or get_llm(temperature=0.7)

    async def polish(
        self,
        chapter: dict,
        style: str,
        creative_brief: dict | None = None,
    ) -> dict:
        """润色章节正文。

        Args:
            chapter: 章节记录(content 为待润色正文)
            style: 风格 key

        Returns:
            新章节记录(content 为润色后正文,status=polished)
        """
        number = chapter.get("chapter_number", chapter.get("chapter", 0))
        style_prompt = get_style_prompt(style)
        scene_plan = chapter.get("scene_plan") or []
        scene_drafts = ensure_scene_drafts(chapter)
        chapter_draft = format_scene_drafts(scene_drafts) if scene_drafts else chapter.get("content", "")
        prompt = fill_template(
            "style_editor",
            style_prompt=style_prompt,
            creative_brief=format_creative_brief(creative_brief),
            chapter_draft=chapter_draft,
        )
        logger.info("StyleEditorAgent 开始润色第 %s 章", number)
        resp = await self.llm.ainvoke(prompt)
        raw_content = resp.content if isinstance(resp.content, str) else str(resp.content)
        polished_scenes = segment_scene_content(raw_content, scene_plan) if scene_plan else []
        content = join_scene_drafts(polished_scenes) if polished_scenes else raw_content.strip()

        polished = dict(chapter)
        polished["content"] = content
        polished["word_count"] = len(content)
        polished["status"] = "polished"
        if polished_scenes:
            polished["scene_drafts"] = polished_scenes
        return polished
