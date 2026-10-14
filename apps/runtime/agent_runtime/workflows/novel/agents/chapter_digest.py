"""终稿章节提炼 Agent:从实际正文生成可持久化的长篇记忆。"""

import hashlib
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.execution.models import get_analyzer_llm
from agent_runtime.execution.structured import invoke_structured, parse_json_block
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.memory.canon import format_canon

DIGEST_VERSION = "chapter-digest-v1"
_LIST_LIMIT = 16
_FACT_LIMIT = 20


def chapter_content_hash(content: str) -> str:
    """返回用于判断提炼结果是否仍匹配正文的稳定哈希。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def has_current_digest(chapter: dict[str, Any]) -> bool:
    """判断章节是否已包含与当前正文匹配的提炼结果。"""
    content = str(chapter.get("content", ""))
    return (
        bool(content)
        and chapter.get("digest_version") == DIGEST_VERSION
        and chapter.get("digest_content_hash") == chapter_content_hash(content)
        and bool(str(chapter.get("summary", "")).strip())
    )


def _clean_strings(values: Any, limit: int = _LIST_LIMIT) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()[:300]
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def normalize_digest(value: dict[str, Any], content: str) -> dict[str, Any]:
    """清理模型结果并附加可校验的版本与正文哈希。"""
    facts: list[dict[str, str]] = []
    for item in value.get("facts") or []:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject", "")).strip()[:200]
        fact_value = str(item.get("value", "")).strip()[:500]
        if not subject or not fact_value:
            continue
        facts.append(
            {
                "kind": str(item.get("kind", "event")).strip()[:80] or "event",
                "subject": subject,
                "value": fact_value,
            }
        )
        if len(facts) >= _FACT_LIMIT:
            break

    return {
        "summary": str(value.get("summary", "")).strip()[:500],
        "events": _clean_strings(value.get("events")),
        "characters": _clean_strings(value.get("characters")),
        "locations": _clean_strings(value.get("locations")),
        "emotion": str(value.get("emotion", "")).strip()[:100],
        "extracted_facts": facts,
        "digest_version": DIGEST_VERSION,
        "digest_content_hash": chapter_content_hash(content),
    }


class ChapterDigestAgent:
    """把终稿正文压缩为后续章节可稳定读取的结构化事实。"""

    def __init__(self, llm: BaseChatModel | None = None):
        self.llm = llm or get_analyzer_llm()

    async def digest(
        self,
        *,
        chapter: dict[str, Any],
        canon: dict[str, Any] | None,
    ) -> dict[str, Any]:
        content = str(chapter.get("content", ""))
        if not content.strip():
            raise ValueError("无法提炼空章节")
        prompt = fill_template(
            "chapter_digest",
            chapter_number=chapter.get("chapter_number", chapter.get("chapter", 0)),
            chapter_title=chapter.get("title", ""),
            scene_plan=repr(chapter.get("scene_plan") or []),
            canon_context=format_canon(
                canon,
                max_chars=3000,
                current_chapter=int(chapter.get("chapter_number", chapter.get("chapter", 0)) or 0),
            ),
            chapter_content=content[:16000],
        )

        def validate(items: list[dict[str, Any]]) -> None:
            if len(items) != 1:
                raise ValueError("必须只返回一个章节提炼对象")
            item = items[0]
            summary = str(item.get("summary", "")).strip()
            if not summary:
                raise ValueError("summary 不能为空")
            for field in ("events", "characters", "locations", "facts"):
                if not isinstance(item.get(field), list):
                    raise ValueError(f"{field} 必须是列表")
            for fact in item["facts"]:
                if not isinstance(fact, dict):
                    raise ValueError("facts 中的每一项都必须是对象")
                if not str(fact.get("subject", "")).strip() or not str(fact.get("value", "")).strip():
                    raise ValueError("每条 fact 必须包含 subject 和 value")

        _, items = await invoke_structured(
            self.llm,
            prompt,
            parser=parse_json_block,
            validator=validate,
            agent_name=type(self).__name__,
            format_name="JSON",
        )
        return normalize_digest(items[0], content)
