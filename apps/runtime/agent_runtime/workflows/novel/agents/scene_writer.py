"""正文写作 Agent(文档 3.5):章节正文生成,风格档案控制输出。"""

import asyncio
import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.execution.models import get_llm
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.agents.scene_planner import fallback_scene_plan
from agent_runtime.workflows.novel.memory.access import NovelMemory
from agent_runtime.workflows.novel.memory.canon import ensure_canon, format_canon
from agent_runtime.workflows.novel.memory.hierarchical import format_hierarchical_memory
from agent_runtime.workflows.novel.resources.brief import format_creative_brief
from agent_runtime.workflows.novel.resources.styles import get_style_prompt
from agent_runtime.workflows.novel.scenes import join_scene_drafts, segment_scene_content

logger = logging.getLogger(__name__)

# 注入 prompt 的上下文长度上限(字符),防止超长 prompt 撑爆 token 预算
_WORLD_LIMIT = 3000
_MEMORY_LIMIT = 4


class SceneWriterAgent:
    """专业小说家:展示而非告知,按风格档案逐章写作。"""

    def __init__(self, llm: BaseChatModel | None = None, novel_id: str = ""):
        self.llm = llm or get_llm(temperature=0.8)
        self.novel_id = novel_id

    async def write_chapter(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """撰写当前章节。

        Args:
            state: NovelState(chapter_plan/style/chapters/current_chapter/...)

        Returns:
            {"chapter_number", "title", "content", "summary"} 章节记录
        """
        number = int(state.get("current_chapter", 1))
        plan: dict[str, Any] = state.get("chapter_plan") or {}
        style_prompt = get_style_prompt(state.get("style", ""))

        # 组装上下文:世界观(截断)+ 相关记忆(向量检索)
        world = str(state.get("world_bible", ""))[:_WORLD_LIMIT]
        canon = ensure_canon(
            state.get("canon"),
            world_bible=str(state.get("world_bible", "")),
            characters=state.get("characters") or [],
            outline=state.get("outline") or [],
            chapters=state.get("chapters") or [],
        )
        canon_text = format_canon(canon, max_chars=2500, current_chapter=number)
        hierarchy_text = format_hierarchical_memory(
            state.get("memory_index"),
            current_chapter=number,
            max_chars=3500,
        )
        memory_snippets: list[str] = []
        if self.novel_id:
            try:
                memory = NovelMemory(self.novel_id)
                memory_snippets = await asyncio.to_thread(memory.get_chapter_memory, number, k=_MEMORY_LIMIT)
            except Exception as exc:
                logger.warning("章节记忆检索失败(%s)", type(exc).__name__)
        memory_text = "\n".join(f"- {s[:300]}" for s in memory_snippets) or "无"

        # 重写指引:一致性 high 问题 / 人工修改意见(首次写作为空)
        revision_notes = str(state.get("revision_notes", "")).strip()
        revision_block = ""
        if revision_notes:
            original = str((state.get("current_draft") or {}).get("content", "")).strip()
            original_block = f"\n\n## 当前终稿或草稿(以此为返修基础)\n{original[:6000]}" if original else ""
            revision_block = f"\n\n## 重写指引(必须修正以下问题后重写本章)\n{revision_notes}{original_block}"

        # 上一章结尾 600 字用于衔接
        chapters = state.get("chapters") or []
        prev_ending = "这是第一章,无需衔接。"
        previous = [item for item in chapters if int(item.get("chapter_number", item.get("chapter", 0)) or 0) < number]
        if previous:
            prev_content = str(previous[-1].get("content", ""))
            prev_ending = prev_content[-600:] if prev_content else "(上一章为空)"

        max_words = max(int(state.get("max_chapter_words") or 6000), 1)
        try:
            requested_words = int(plan.get("estimated_words") or max_words)
        except TypeError, ValueError:
            requested_words = max_words
        target_words = min(max(requested_words, 1), max_words)
        scene_plan = state.get("scene_plan") or fallback_scene_plan(plan, target_words)
        prompt = fill_template(
            "scene_writer",
            target_words=target_words,
            style_prompt=style_prompt,
            context=(
                f"## 世界观摘要\n{world}\n\n## 结构化 Canon\n{canon_text}"
                f"\n\n{format_creative_brief(state.get('creative_brief'))}"
                f"\n\n## 分层全书记忆\n{hierarchy_text}"
                f"\n\n## 相关记忆\n{memory_text}{revision_block}"
            ),
            chapter_plan=str(plan),
            scene_plan=scene_plan,
            previous_ending=prev_ending,
            chapter_number=number,
        )
        logger.info("SceneWriterAgent 开始撰写第 %s 章(目标 %s 字)", number, target_words)
        resp = await self.llm.ainvoke(prompt)
        raw_content = resp.content if isinstance(resp.content, str) else str(resp.content)
        scene_drafts = segment_scene_content(raw_content, scene_plan)
        content = join_scene_drafts(scene_drafts)

        chapter = {
            "chapter": number,
            "chapter_number": number,
            "title": str(plan.get("title", f"第{number}章")),
            "content": content,
            "summary": str(plan.get("summary", ""))[:300],  # 大纲摘要先占位,后续可精炼
            "word_count": len(content),
            "status": "draft",
            "scene_plan": scene_plan,
            "scene_drafts": scene_drafts,
        }
        for key in (
            "time_days",
            "emotion",
            "characters",
            "locations",
            "events",
            "foreshadowing",
            "narrative_beats",
            "conflict",
            "cliffhanger",
        ):
            if key in plan:
                chapter[key] = plan[key]

        return chapter
