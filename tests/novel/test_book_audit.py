from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from agent_runtime.workflows.novel.agents.book_auditor import BookAuditorAgent
from agent_runtime.workflows.novel.analysis.book import evaluate_book_deterministic, manuscript_hash
from agent_runtime.workflows.novel.graph import nodes


def _chapters() -> list[dict]:
    return [
        {"chapter_number": 1, "title": "起", "content": "开端正文", "summary": "进入城中"},
        {"chapter_number": 2, "title": "终", "content": "结局正文更长", "summary": "谜题解决"},
    ]


def _canon() -> dict:
    return {
        "version": 3,
        "world_facts": [],
        "characters": {"林寒": {"appearances": [1, 2]}},
        "timeline": [
            {"chapter": 1, "status": "final"},
            {"chapter": 2, "status": "final"},
        ],
        "facts": [],
        "aliases": {},
        "audit": [],
        "narrative_threads": [
            {"title": "身份之谜", "priority": "major", "status": "resolved"},
        ],
    }


def test_deterministic_book_audit_scores_completed_manuscript():
    report = evaluate_book_deterministic(
        chapters=_chapters(),
        canon=_canon(),
        total_chapters=2,
    )

    assert report["scores"]["chapter_completion"] == 100
    assert report["scores"]["narrative_resolution"] == 100
    assert report["scores"]["timeline_integrity"] == 100
    assert len(manuscript_hash(_chapters())) == 64


async def test_book_auditor_validates_structured_output():
    fake = FakeListChatModel(
        responses=[
            """[{
      "scores": {
        "plot_coherence": 91,
        "character_arc": 87,
        "theme_payoff": 83,
        "style_consistency": 90,
        "ending_satisfaction": 88,
        "unresolved_promises": 85
      },
      "findings": ["主线完整"],
      "revision_priorities": ["压缩中段重复信息"]
    }]"""
        ]
    )

    report = await BookAuditorAgent(llm=fake).evaluate(
        novel={"title": "雾中剑", "genre": "武侠", "style": "gu_long"},
        chapters=_chapters(),
        canon=_canon(),
        deterministic_report={"overall_score": 100},
    )

    assert report["scores"]["plot_coherence"] == 91
    assert report["revision_priorities"] == ["压缩中段重复信息"]


async def test_book_auditor_receives_hierarchical_memory():
    class CaptureModel:
        prompt = ""

        async def ainvoke(self, prompt):
            self.prompt = prompt
            return AIMessage(
                content="""[{
              "scores": {
                "plot_coherence": 80,
                "character_arc": 80,
                "theme_payoff": 80,
                "style_consistency": 80,
                "ending_satisfaction": 80,
                "unresolved_promises": 80
              },
              "findings": [],
              "revision_priorities": []
            }]"""
            )

    model = CaptureModel()
    await BookAuditorAgent(llm=model).evaluate(
        novel={
            "title": "雾中剑",
            "creative_brief": {
                "target_audience": "成年武侠读者",
                "ending_tone": "bittersweet",
            },
        },
        chapters=_chapters(),
        canon=_canon(),
        deterministic_report={"overall_score": 100},
        memory_index={
            "chapters": [{"chapter": 1, "title": "起", "summary": "王印失踪"}],
            "arcs": [
                {
                    "arc": 1,
                    "start_chapter": 1,
                    "end_chapter": 2,
                    "summary": "王印谜题从建立到解决",
                }
            ],
        },
    )

    assert "王印谜题从建立到解决" in model.prompt
    assert "目标读者：成年武侠读者" in model.prompt
    assert "结局基调：苦乐参半" in model.prompt


async def test_book_auditor_node_finishes_when_model_fails(monkeypatch):
    class BrokenAuditor:
        async def evaluate(self, **kwargs):
            raise RuntimeError("provider down")

    monkeypatch.setattr(nodes, "BookAuditorAgent", BrokenAuditor)
    result = await nodes.book_auditor_node(
        {
            "novel_id": "",
            "title": "雾中剑",
            "total_chapters": 2,
            "current_chapter": 3,
            "chapters": _chapters(),
            "canon": _canon(),
        }
    )

    assert result["book_audit_completed"] is True
    assert result["current_phase"] == "completed"
    assert result["book_audit"]["deterministic_scores"]
    assert result["book_audit"]["judge_scores"] == {}
    assert result["book_audit"]["judge_error"] == "模型终审失败:RuntimeError"
