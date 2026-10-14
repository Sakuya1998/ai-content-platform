"""世界观构建 Agent(文档 3.2):输入类型+灵感,输出世界观圣经(YAML)。"""

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


class WorldBuilderAgent:
    """专业世界观架构师:构建自洽、有深度的小说世界观。"""

    def __init__(self, llm: BaseChatModel | None = None, novel_id: str = ""):
        self.llm = llm or get_llm(temperature=0.8)
        self.novel_id = novel_id

    async def generate(
        self,
        genre: str,
        inspiration: str,
        title: str = "",
        creative_brief: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """生成世界观圣经。

        Returns:
            {"world_bible": YAML 文本, "world_yaml": 解析后的顶层 dict(尽力而为)}
        """
        prompt = fill_template(
            "world_builder",
            user_input=(
                f"小说标题:{title}\n类型:{genre}\n灵感:{inspiration}\n\n{format_creative_brief(creative_brief)}"
            ),
        )
        logger.info("WorldBuilderAgent 开始生成世界观: %s / %s", title, genre)

        def validate_world(items: list[dict]) -> None:
            if not items or not items[0]:
                raise ValueError("世界观必须是非空 YAML 映射")

        world_bible, parsed = await invoke_structured(
            self.llm,
            prompt,
            parser=parse_yaml_block,
            validator=validate_world,
            agent_name=type(self).__name__,
            format_name="YAML",
        )

        # 写入向量记忆,供后续章节检索
        if self.novel_id:
            try:
                memory = NovelMemory(self.novel_id)
                await asyncio.to_thread(
                    memory.store_content,
                    world_bible,
                    metadata={"type": "world_bible", "title": title},
                    content_id=f"{self.novel_id}:world_bible",
                )
            except Exception as exc:  # 向量库故障不阻断创作主流程
                logger.warning("世界观写入向量记忆失败(%s)", type(exc).__name__)

        world_yaml: dict[str, Any] = parsed[0]
        return {"world_bible": world_bible, "world_yaml": world_yaml}
