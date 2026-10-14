"""场景局部重写 Agent:只替换人工指定的一个场景。"""

import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.execution.models import get_llm
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.memory.canon import ensure_canon, format_canon
from agent_runtime.workflows.novel.resources.brief import format_creative_brief
from agent_runtime.workflows.novel.resources.styles import get_style_prompt
from agent_runtime.workflows.novel.scenes import ensure_scene_drafts, join_scene_drafts, segment_scene_content

logger = logging.getLogger(__name__)


class SceneRewriterAgent:
    """依据人工意见重写单个场景，同时保持其他场景逐字不变。"""

    def __init__(self, llm: BaseChatModel | None = None):
        self.llm = llm or get_llm(temperature=0.75)

    async def rewrite_scene(self, state: dict[str, Any]) -> dict[str, Any]:
        draft = dict(state.get("current_draft") or {})
        scene_plan = draft.get("scene_plan") or state.get("scene_plan") or []
        scene_number = int(state.get("revision_scene_number") or 0)
        target_plan = next(
            (item for item in scene_plan if int(item.get("scene_number", 0)) == scene_number),
            None,
        )
        if target_plan is None:
            raise ValueError(f"场景 {scene_number} 不存在,无法局部重写")

        scene_drafts = ensure_scene_drafts({**draft, "scene_plan": scene_plan})
        target_index = next(
            index for index, item in enumerate(scene_drafts) if int(item.get("scene_number", 0)) == scene_number
        )
        canon = ensure_canon(
            state.get("canon"),
            world_bible=str(state.get("world_bible", "")),
            characters=state.get("characters") or [],
            outline=state.get("outline") or [],
            chapters=state.get("chapters") or [],
        )
        prompt = fill_template(
            "scene_rewriter",
            chapter_number=draft.get("chapter_number", state.get("current_chapter", 0)),
            scene_number=scene_number,
            scene_plan=target_plan,
            feedback=str(state.get("revision_notes") or "").strip(),
            original_scene=scene_drafts[target_index].get("content", ""),
            previous_context=(
                str(scene_drafts[target_index - 1].get("content", ""))[-600:]
                if target_index > 0
                else "本章开场,没有前置场景。"
            ),
            next_context=(
                str(scene_drafts[target_index + 1].get("content", ""))[:600]
                if target_index + 1 < len(scene_drafts)
                else "本章结尾,没有后续场景。"
            ),
            canon_context=format_canon(
                canon,
                max_chars=2500,
                current_chapter=int(state.get("current_chapter", 1)),
            ),
            creative_brief=format_creative_brief(state.get("creative_brief")),
            style_prompt=get_style_prompt(state.get("style", "")),
        )
        logger.info(
            "SceneRewriterAgent 开始重写第 %s 章场景 %s",
            draft.get("chapter_number"),
            scene_number,
        )
        response = await self.llm.ainvoke(prompt)
        replacement = response.content if isinstance(response.content, str) else str(response.content)
        replacement = segment_scene_content(replacement, [target_plan])[0]["content"].strip()
        if not replacement:
            raise ValueError(f"场景 {scene_number} 局部重写返回空正文")

        updated_drafts = [dict(item) for item in scene_drafts]
        updated_drafts[target_index]["content"] = replacement
        content = join_scene_drafts(updated_drafts)
        return {
            **draft,
            "content": content,
            "word_count": len(content),
            "status": "revised",
            "scene_plan": scene_plan,
            "scene_drafts": updated_drafts,
        }
