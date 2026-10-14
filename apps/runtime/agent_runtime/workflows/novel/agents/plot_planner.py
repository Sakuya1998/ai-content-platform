"""情节规划 Agent(文档 3.4):三幕结构 + 冲突升级 + 伏笔回收的章节大纲。"""

import asyncio
import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.execution.models import get_analyzer_llm
from agent_runtime.execution.structured import invoke_structured, parse_yaml_block
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.memory.access import NovelMemory
from agent_runtime.workflows.novel.resources.brief import format_creative_brief

logger = logging.getLogger(__name__)

_BEAT_ACTIONS = {"setup", "develop", "resolve"}


def validate_outline(items: list[dict[str, Any]], total_chapters: int) -> None:
    """校验章节完整覆盖，并校验跨章叙事线程。"""
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("每个大纲条目都必须是对象")
    chapters = [int(item.get("chapter", 0) or 0) for item in items]
    expected = list(range(1, total_chapters + 1))
    if sorted(chapters) != expected or len(chapters) != len(set(chapters)):
        raise ValueError(f"章节编号必须完整覆盖 1..{total_chapters},实际为 {sorted(chapters)}")
    validate_narrative_outline(items, total_chapters)


def validate_narrative_outline(items: list[dict[str, Any]], total_chapters: int) -> None:
    """校验结构化叙事线程，防止主要伏笔只有埋设没有回收。"""
    threads: dict[str, dict[str, Any]] = {}
    for item in items:
        chapter = int(item.get("chapter", 0) or 0)
        beats = item.get("narrative_beats") or []
        if not isinstance(beats, list):
            raise ValueError(f"第{chapter}章 narrative_beats 必须是列表")
        for beat in beats:
            if not isinstance(beat, dict):
                raise ValueError(f"第{chapter}章 narrative beat 必须是对象")
            title = str(beat.get("thread", "")).strip()
            action = str(beat.get("action", "")).strip().casefold()
            if not title:
                raise ValueError(f"第{chapter}章 narrative beat 缺少 thread")
            if action not in _BEAT_ACTIONS:
                raise ValueError(f"线程「{title}」action 必须是 setup/develop/resolve")
            due = beat.get("due_chapter")
            if due not in (None, "") and not 1 <= int(due) <= total_chapters:
                raise ValueError(f"线程「{title}」due_chapter 超出全书章节范围")
            record = threads.setdefault(
                title.casefold(),
                {
                    "title": title,
                    "priority": "minor",
                    "setup": [],
                    "resolve": [],
                    "due": [],
                },
            )
            if str(beat.get("priority", "minor")).casefold() == "major":
                record["priority"] = "major"
            if due not in (None, ""):
                record["due"].append(int(due))
            if action in {"setup", "resolve"}:
                record[action].append(chapter)

    for record in threads.values():
        setups = record["setup"]
        resolves = record["resolve"]
        if resolves and setups and min(resolves) < min(setups):
            raise ValueError(f"线程「{record['title']}」在埋设前就已回收")
        if record["priority"] == "major" and setups and not resolves:
            raise ValueError(f"主要线程「{record['title']}」缺少 resolve beat")
        if resolves and record["due"] and min(resolves) > min(record["due"]):
            raise ValueError(f"线程「{record['title']}」resolve 晚于 due_chapter")


class PlotPlannerAgent:
    """专业情节规划师:输出逐章大纲(冲突/悬念/角色/伏笔/情绪)。"""

    def __init__(self, llm: BaseChatModel | None = None, novel_id: str = ""):
        self.llm = llm or get_analyzer_llm()
        self.novel_id = novel_id

    async def generate(
        self,
        world_bible: str,
        characters: list[dict[str, Any]],
        total_chapters: int,
        inspiration: str,
        creative_brief: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """规划全书章节大纲。

        Returns:
            大纲条目列表(chapter/title/summary/conflict/cliffhanger/characters/
            foreshadowing/emotion/estimated_words)
        """
        char_lines = "\n".join(
            f"- {c.get('name', '?')}({c.get('role', '?')}):{c.get('personality', '')}" for c in characters
        )
        context = (
            f"## 世界观圣经\n{world_bible}\n\n## 角色列表\n{char_lines}\n\n"
            f"## 用户灵感\n{inspiration}\n\n## 总章节数\n{total_chapters} 章\n\n"
            f"{format_creative_brief(creative_brief)}"
        )
        prompt = fill_template("plot_planner", context=context)
        logger.info("PlotPlannerAgent 开始规划 %s 章大纲", total_chapters)
        _, outline = await invoke_structured(
            self.llm,
            prompt,
            parser=parse_yaml_block,
            validator=lambda items: validate_outline(items, total_chapters),
            agent_name=type(self).__name__,
            format_name="YAML",
        )
        # 按 chapter 字段排序,保证章节顺序稳定
        outline.sort(key=lambda c: int(c.get("chapter", 0)))

        if self.novel_id:
            try:
                memory = NovelMemory(self.novel_id)
                for ch in outline:
                    await asyncio.to_thread(
                        memory.store_content,
                        f"第{ch.get('chapter', '?')}章大纲:{ch.get('summary', '')}",
                        metadata={"type": "outline", "chapter": ch.get("chapter", 0)},
                        content_id=f"{self.novel_id}:outline:{ch.get('chapter', 0)}",
                    )
            except Exception as exc:
                logger.warning("大纲写入向量记忆失败(%s)", type(exc).__name__)

        return outline
