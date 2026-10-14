import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agent_runtime.workflows.novel.agents.replanner import REPLAN_VERSION, ReplannerAgent, merge_future_outline


def _outline() -> list[dict]:
    return [
        {"chapter": 1, "title": "开端", "summary": "进入城中"},
        {"chapter": 2, "title": "追踪", "summary": "发现线索"},
        {"chapter": 3, "title": "回收", "summary": "揭开真相"},
    ]


def test_merge_future_outline_preserves_completed_chapters_and_coverage():
    merged = merge_future_outline(
        _outline(),
        [{"chapter": 2, "summary": "追兵改变了调查方向", "conflict": "身份暴露"}],
        current_chapter=1,
        total_chapters=3,
    )

    assert merged[0] == _outline()[0]
    assert merged[1]["summary"] == "追兵改变了调查方向"
    assert merged[1]["title"] == "追踪"
    assert [item["chapter"] for item in merged] == [1, 2, 3]


def test_merge_future_outline_rejects_completed_or_duplicate_patch():
    with pytest.raises(ValueError, match="当前章之后"):
        merge_future_outline(
            _outline(),
            [{"chapter": 1, "summary": "不应修改"}],
            current_chapter=1,
            total_chapters=3,
        )
    with pytest.raises(ValueError, match="重复"):
        merge_future_outline(
            _outline(),
            [{"chapter": 2}, {"chapter": 2}],
            current_chapter=1,
            total_chapters=3,
        )


async def test_replanner_returns_validated_patch():
    llm = FakeListChatModel(
        responses=[
            """[
      {
        "status": "replanned",
        "impact": "medium",
        "rationale": "第一章实际揭示了身份线索，因此第二章需要提前追踪。",
        "outline_updates": [
          {"chapter": 2, "summary": "提前追踪身份线索"}
        ]
      }
    ]"""
        ]
    )

    result = await ReplannerAgent(llm=llm).analyze(
        current_chapter=1,
        total_chapters=3,
        chapter_plan=_outline()[0],
        chapter_digest={"summary": "真实摘要", "events": ["揭示线索"]},
        future_outline=_outline()[1:],
    )

    assert result["status"] == "replanned"
    assert result["impact"] == "medium"
    assert result["outline_updates"][0]["chapter"] == 2
    assert result["replan_version"] == REPLAN_VERSION


async def test_post_finalization_replan_updates_future_timeline(monkeypatch):
    from agent_runtime.workflows.novel.graph import nodes
    from agent_runtime.workflows.novel.memory.canon import build_canon

    class StubReplanner:
        async def analyze(self, **kwargs):
            return {
                "status": "replanned",
                "impact": "medium",
                "rationale": "实际成稿提前揭示了身份线索。",
                "outline_updates": [{"chapter": 2, "summary": "提前追踪身份线索"}],
                "replan_version": REPLAN_VERSION,
            }

    monkeypatch.setattr(nodes, "ReplannerAgent", StubReplanner)
    outline = _outline()
    state = {
        "current_chapter": 1,
        "total_chapters": 3,
        "outline": outline,
        "chapter_plan": outline[0],
        "novel_id": "",
    }
    final_draft = {
        "chapter_number": 1,
        "title": "开端",
        "content": "林寒进入城中并发现追踪者。",
        "summary": "真实摘要",
        "events": ["发现追踪者"],
        "digest_version": "chapter-digest-v1",
        "digest_content_hash": "hash",
    }
    canon = build_canon(
        world_bible="城市: 雾都",
        characters=[{"name": "林寒"}],
        outline=outline,
        chapters=[final_draft],
    )

    updated_outline, proposal, updated_canon = await nodes._replan_after_finalization(
        state,
        final_draft,
        canon,
    )

    assert proposal["status"] == "replanned"
    assert updated_outline[1]["summary"] == "提前追踪身份线索"
    chapter_two = next(item for item in updated_canon["timeline"] if item["chapter"] == 2)
    assert chapter_two["summary"] == "提前追踪身份线索"
    assert next(item for item in updated_canon["timeline"] if item["chapter"] == 1)["status"] == "final"
