"""pytest 共享夹具:确保项目根在 sys.path,并提供假 LLM。"""

import sys
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def explicit_offline_prompts(monkeypatch):
    import importlib

    from agent_runtime.domain.workflow_contracts import NOVEL_NODES
    from agent_runtime.execution.context import current_execution
    from agent_runtime.prompts.loader import fill_template
    from agent_runtime.prompts.renderer import render_prompt
    from tests.fixtures.prompts import prompt_versions

    versions = prompt_versions()

    def offline_or_claimed(node, **variables):
        if current_execution(required=False):
            return fill_template(node, **variables)
        return "\n\n".join(message["content"] for message in render_prompt(versions[node], variables))

    for node in NOVEL_NODES:
        module = importlib.import_module("agent_runtime.workflows.novel.agents." + node)
        monkeypatch.setattr(module, "fill_template", offline_or_claimed)


@pytest.fixture(autouse=True)
def fake_chapter_digest_model(monkeypatch) -> FakeListChatModel:
    """章节定稿提炼统一使用独立假模型，避免消耗其他 Agent 的响应序列。"""
    fake = FakeListChatModel(
        responses=[
            """[
      {
        "summary": "林寒穿过雾都城门，并在盘查中发现追兵逼近。",
        "events": ["林寒进入雾都", "城门追兵现身"],
        "characters": ["林寒"],
        "locations": ["雾都城门"],
        "emotion": "紧张",
        "facts": [
          {"kind": "state", "subject": "林寒", "value": "已进入雾都"}
        ]
      }
    ]"""
        ]
    )
    monkeypatch.setattr("agent_runtime.workflows.novel.agents.chapter_digest.get_analyzer_llm", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def fake_replanner_model(monkeypatch) -> FakeListChatModel:
    """重规划使用独立稳定响应，避免改变其他 Agent 的假模型序列。"""
    fake = FakeListChatModel(
        responses=[
            """[
      {
        "status": "stable",
        "impact": "low",
        "rationale": "实际成稿没有改变后续因果链。",
        "outline_updates": []
      }
    ]"""
        ]
    )
    monkeypatch.setattr("agent_runtime.workflows.novel.agents.replanner.get_analyzer_llm", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def fake_book_auditor_model(monkeypatch) -> FakeListChatModel:
    """全书终审使用独立响应，避免改变章节流水线的模型调用序列。"""
    fake = FakeListChatModel(
        responses=[
            """[
      {
        "scores": {
          "plot_coherence": 88,
          "character_arc": 84,
          "theme_payoff": 82,
          "style_consistency": 90,
          "ending_satisfaction": 86,
          "unresolved_promises": 80
        },
        "findings": ["主线因果完整，结局回应了开篇悬念。"],
        "revision_priorities": ["补强次要角色在结局前的选择。"]
      }
    ]"""
        ]
    )
    monkeypatch.setattr("agent_runtime.workflows.novel.agents.book_auditor.get_analyzer_llm", lambda: fake)
    return fake


@pytest.fixture
def fake_llm() -> FakeListChatModel:
    """按序返回固定回复的假 LLM(不依赖 API Key)。"""
    return FakeListChatModel(
        responses=[
            "```yaml\n世界观名称: 测试世界\n历史背景: 五百年前大雾封锁全城\n```",
            "- name: 林寒\n  role: 主角\n  personality:\n    core_desire: 找回记忆\n    core_fear: 真相\n",
            "- chapter: 1\n  title: 雾起\n  summary: 失忆剑客入城\n  conflict: 身份之谜\n  estimated_words: 100\n",
            "- scene_number: 1\n  goal: 进入雾都\n  conflict: 城门盘查\n"
            "  turn: 发现追兵\n  location: 城门\n  characters: [林寒]\n"
            "  emotion: 紧张\n  estimated_words: 100\n",
            "夜雾漫过城门,他握紧了剑。",
            "夜雾漫过城门,他握紧了剑。(润色)",
            "[]",
        ]
    )
