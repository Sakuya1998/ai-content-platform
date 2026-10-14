"""角色设计 Agent(文档 3.3):五维角色档案 + 角色弧光设计。"""

import asyncio
import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.execution.models import get_llm
from agent_runtime.execution.structured import invoke_structured, parse_yaml_block
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.memory.access import NovelMemory
from agent_runtime.workflows.novel.resources.brief import format_creative_brief

logger = logging.getLogger(__name__)


def validate_characters(items: list[dict[str, Any]]) -> None:
    """校验可持久化、可供后续 Agent 使用的角色列表。"""
    if not items:
        raise ValueError("角色列表不能为空")
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("每个角色都必须是对象")
    names = [str(item.get("name", "")).strip() for item in items]
    if any(not name for name in names):
        raise ValueError("每个角色都必须包含 name")
    normalized = [name.casefold() for name in names]
    if len(set(normalized)) != len(normalized):
        raise ValueError("角色 name 不能重复")


class CharacterDesignerAgent:
    """专业角色设计师:心理/关系/语言/行为/弧光五维建模。"""

    def __init__(self, llm: BaseChatModel | None = None, novel_id: str = ""):
        self.llm = llm or get_llm(temperature=0.7)
        self.novel_id = novel_id

    async def generate(
        self,
        world_bible: str,
        inspiration: str,
        creative_brief: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """生成主要角色列表。

        Args:
            world_bible: 世界观圣经文本
            inspiration: 用户灵感

        Returns:
            角色档案列表(name/role/personality/relationships/speech_pattern/behavior/arc)
        """
        context = (
            f"## 世界观圣经\n{world_bible}\n\n## 用户灵感\n{inspiration}\n\n{format_creative_brief(creative_brief)}"
        )
        prompt = fill_template("character_designer", context=context)
        logger.info("CharacterDesignerAgent 开始设计角色")
        _, characters = await invoke_structured(
            self.llm,
            prompt,
            parser=parse_yaml_block,
            validator=validate_characters,
            agent_name=type(self).__name__,
            format_name="YAML",
        )

        # 角色卡逐个写入向量记忆
        if self.novel_id:
            try:
                memory = NovelMemory(self.novel_id)
                for index, c in enumerate(characters, start=1):
                    card = f"角色档案:{c.get('name', '未知')}\n{c!r}"
                    await asyncio.to_thread(
                        memory.store_content,
                        card,
                        metadata={"type": "character", "name": str(c.get("name", ""))},
                        content_id=f"{self.novel_id}:character:{index}",
                    )
            except Exception as exc:
                logger.warning("角色写入向量记忆失败(%s)", type(exc).__name__)

        return characters
