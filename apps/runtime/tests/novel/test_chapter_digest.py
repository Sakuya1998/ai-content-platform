import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agent_runtime.execution.structured import StructuredOutputError
from agent_runtime.workflows.novel.agents.chapter_digest import (
    DIGEST_VERSION,
    ChapterDigestAgent,
    chapter_content_hash,
    has_current_digest,
)


async def test_chapter_digest_uses_actual_content_and_normalizes_memory_fields():
    llm = FakeListChatModel(
        responses=[
            """[
      {
        "summary": "沈砚读完旧信，忘记了妹妹的名字，却发现信封内侧出现自己的笔迹。",
        "events": ["沈砚读信", "沈砚失去一段记忆", "沈砚读信"],
        "characters": ["沈砚", "沈砚"],
        "locations": ["档案室"],
        "emotion": "惊惧",
        "facts": [
          {"kind": "state", "subject": "沈砚", "value": "忘记了妹妹的名字"}
        ]
      }
    ]"""
        ]
    )
    chapter = {
        "chapter_number": 1,
        "title": "无字来信",
        "summary": "大纲中的旧摘要",
        "content": "沈砚读完旧信。妹妹的名字忽然从记忆里消失。信封内侧浮出他的笔迹。",
        "scene_plan": [],
    }

    digest = await ChapterDigestAgent(llm=llm).digest(chapter=chapter, canon={})

    assert digest["summary"].startswith("沈砚读完旧信")
    assert digest["events"] == ["沈砚读信", "沈砚失去一段记忆"]
    assert digest["characters"] == ["沈砚"]
    assert digest["extracted_facts"][0]["value"] == "忘记了妹妹的名字"
    assert digest["digest_version"] == DIGEST_VERSION
    assert digest["digest_content_hash"] == chapter_content_hash(chapter["content"])
    assert has_current_digest({**chapter, **digest})
    assert not has_current_digest({**chapter, **digest, "content": "正文已改变"})


async def test_chapter_digest_rejects_incomplete_structured_output():
    llm = FakeListChatModel(responses=['{"summary": "只有摘要"}', '{"summary": "仍不完整"}'])

    with pytest.raises(StructuredOutputError, match="ChapterDigestAgent"):
        await ChapterDigestAgent(llm=llm).digest(
            chapter={"chapter_number": 1, "content": "正文"},
            canon={},
        )
