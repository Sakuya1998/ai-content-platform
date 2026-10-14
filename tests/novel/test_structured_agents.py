"""结构化 Agent 输出重试与严格校验测试。"""

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from agent_runtime.execution.structured import StructuredOutputError
from agent_runtime.workflows.novel.agents.character_designer import CharacterDesignerAgent
from agent_runtime.workflows.novel.agents.consistency_checker import ConsistencyCheckerAgent
from agent_runtime.workflows.novel.agents.plot_planner import PlotPlannerAgent, validate_narrative_outline
from agent_runtime.workflows.novel.agents.scene_planner import ScenePlannerAgent, _normalize_word_budgets
from agent_runtime.workflows.novel.agents.scene_rewriter import SceneRewriterAgent
from agent_runtime.workflows.novel.agents.scene_writer import SceneWriterAgent


async def test_character_designer_retries_once_after_invalid_yaml():
    fake = FakeListChatModel(
        responses=[
            "这不是 YAML 列表",
            "- name: 林寒\n  role: 主角\n",
        ]
    )
    agent = CharacterDesignerAgent(llm=fake)

    characters = await agent.generate("世界观", "灵感")

    assert characters[0]["name"] == "林寒"


async def test_plot_planner_fails_when_retry_still_misses_chapters():
    incomplete = "- chapter: 1\n  title: 开端\n"
    fake = FakeListChatModel(responses=[incomplete, incomplete])
    agent = PlotPlannerAgent(llm=fake)

    with pytest.raises(StructuredOutputError, match="PlotPlannerAgent"):
        await agent.generate("世界观", [], total_chapters=2, inspiration="灵感")


async def test_plot_planner_injects_creative_brief_into_prompt():
    class CaptureModel:
        prompt = ""

        async def ainvoke(self, prompt):
            self.prompt = prompt
            return AIMessage(content="- chapter: 1\n  title: 雾起\n  summary: 入城\n")

    model = CaptureModel()
    await PlotPlannerAgent(llm=model).generate(
        "世界观",
        [],
        total_chapters=1,
        inspiration="灵感",
        creative_brief={
            "target_audience": "硬核推理读者",
            "point_of_view": "first_person",
            "themes": ["身份"],
        },
    )

    assert "目标读者：硬核推理读者" in model.prompt
    assert "叙事视角：第一人称" in model.prompt


def test_major_narrative_thread_requires_a_resolve_beat():
    outline = [
        {
            "chapter": 1,
            "narrative_beats": [
                {
                    "thread": "失踪王印",
                    "action": "setup",
                    "priority": "major",
                }
            ],
        }
    ]

    with pytest.raises(ValueError, match="缺少 resolve"):
        validate_narrative_outline(outline, total_chapters=1)


def test_narrative_thread_must_resolve_by_its_due_chapter():
    outline = [
        {
            "chapter": 1,
            "narrative_beats": [
                {
                    "thread": "失踪王印",
                    "action": "setup",
                    "priority": "major",
                    "due_chapter": 2,
                }
            ],
        },
        {
            "chapter": 3,
            "narrative_beats": [
                {
                    "thread": "失踪王印",
                    "action": "resolve",
                    "priority": "major",
                }
            ],
        },
    ]

    with pytest.raises(ValueError, match="晚于 due_chapter"):
        validate_narrative_outline(outline, total_chapters=3)


async def test_scene_planner_returns_sequential_scenes_with_normalized_budget():
    fake = FakeListChatModel(
        responses=[
            "- scene_number: 1\n"
            "  goal: 进入雾都\n  conflict: 城门盘查\n  turn: 发现追兵\n"
            "  location: 城门\n  characters: [林寒]\n  emotion: 紧张\n  estimated_words: 60\n"
            "- scene_number: 2\n"
            "  goal: 摆脱追兵\n  conflict: 道路封锁\n  turn: 获得神秘线索\n"
            "  location: 暗巷\n  characters: [林寒]\n  emotion: 惊疑\n  estimated_words: 90\n"
        ]
    )
    agent = ScenePlannerAgent(llm=fake)

    scenes = await agent.plan_chapter(
        {
            "current_chapter": 1,
            "chapter_plan": {"chapter": 1, "summary": "林寒入城", "estimated_words": 100},
            "max_chapter_words": 100,
            "chapters": [],
        }
    )

    assert [scene["scene_number"] for scene in scenes] == [1, 2]
    assert sum(scene["estimated_words"] for scene in scenes) == 100


async def test_scene_planner_rejects_incomplete_scene_plan_after_retry():
    incomplete = "- scene_number: 1\n  goal: 入城\n"
    agent = ScenePlannerAgent(llm=FakeListChatModel(responses=[incomplete, incomplete]))

    with pytest.raises(StructuredOutputError, match="ScenePlannerAgent"):
        await agent.plan_chapter(
            {
                "current_chapter": 1,
                "chapter_plan": {"chapter": 1, "estimated_words": 100},
                "max_chapter_words": 100,
            }
        )


async def test_scene_planner_assigns_each_narrative_beat_exactly_once():
    response = (
        "- scene_number: 1\n"
        "  goal: 发现印盒\n  conflict: 守卫阻拦\n  turn: 印盒为空\n"
        "  location: 王库\n  characters: [林寒]\n  emotion: 惊疑\n  estimated_words: 100\n"
        "  narrative_beats:\n"
        "    - thread: 失踪王印\n      action: setup\n      description: 发现空印盒\n"
    )
    agent = ScenePlannerAgent(llm=FakeListChatModel(responses=[response]))

    scenes = await agent.plan_chapter(
        {
            "current_chapter": 1,
            "chapter_plan": {
                "chapter": 1,
                "estimated_words": 100,
                "narrative_beats": [
                    {
                        "thread": "失踪王印",
                        "action": "setup",
                        "description": "发现空印盒",
                    }
                ],
            },
            "max_chapter_words": 100,
        }
    )

    assert scenes[0]["narrative_beats"][0]["thread"] == "失踪王印"


async def test_scene_planner_rejects_missing_narrative_beat_assignment():
    response = (
        "- scene_number: 1\n"
        "  goal: 发现印盒\n  conflict: 守卫阻拦\n  turn: 印盒为空\n"
        "  location: 王库\n  characters: [林寒]\n  emotion: 惊疑\n  estimated_words: 100\n"
    )
    agent = ScenePlannerAgent(llm=FakeListChatModel(responses=[response, response]))

    with pytest.raises(StructuredOutputError, match="ScenePlannerAgent"):
        await agent.plan_chapter(
            {
                "current_chapter": 1,
                "chapter_plan": {
                    "chapter": 1,
                    "estimated_words": 100,
                    "narrative_beats": [
                        {
                            "thread": "失踪王印",
                            "action": "setup",
                            "description": "发现空印盒",
                        }
                    ],
                },
                "max_chapter_words": 100,
            }
        )


async def test_scene_planner_retry_receives_exact_beat_mismatch():
    incomplete = (
        "- scene_number: 1\n"
        "  goal: 发现印盒\n  conflict: 守卫阻拦\n  turn: 印盒为空\n"
        "  location: 王库\n  characters: [林寒]\n  emotion: 惊疑\n  estimated_words: 100\n"
    )
    corrected = (
        f"{incomplete}  narrative_beats:\n    - thread: 失踪王印\n      action: setup\n      description: 发现空印盒\n"
    )

    class CorrectsFromDetailedMismatch:
        async def ainvoke(self, prompt):
            content = corrected if "实际分配=[]" in prompt else incomplete
            return AIMessage(content=content)

    agent = ScenePlannerAgent(llm=CorrectsFromDetailedMismatch())

    scenes = await agent.plan_chapter(
        {
            "current_chapter": 1,
            "chapter_plan": {
                "chapter": 1,
                "estimated_words": 100,
                "narrative_beats": [
                    {
                        "thread": "失踪王印",
                        "action": "setup",
                        "description": "发现空印盒",
                    }
                ],
            },
            "max_chapter_words": 100,
        }
    )

    assert scenes[0]["narrative_beats"][0]["thread"] == "失踪王印"


def test_scene_budget_keeps_every_scene_executable_when_target_is_tiny():
    scenes = [
        {"scene_number": 1, "estimated_words": 2},
        {"scene_number": 2, "estimated_words": 1},
        {"scene_number": 3, "estimated_words": 1},
    ]

    _normalize_word_budgets(scenes, target_words=1)

    assert [scene["estimated_words"] for scene in scenes] == [1, 1, 1]


async def test_consistency_checker_never_treats_unparseable_text_as_success():
    fake = FakeListChatModel(responses=["一致性检查通过", "仍然没有 JSON"])
    agent = ConsistencyCheckerAgent(llm=fake)

    with pytest.raises(StructuredOutputError, match="ConsistencyCheckerAgent"):
        await agent.check(
            chapter={"chapter_number": 1, "content": "正文"},
            world_bible="世界观",
            characters=[],
            outline=[],
            previous_chapters=[],
        )


async def test_consistency_checker_merges_deterministic_checks_into_llm_issues(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_invoke(llm, prompt, *, parser, validator, agent_name, format_name):
        captured["prompt"] = prompt
        return "[]", []

    monkeypatch.setattr("agent_runtime.workflows.novel.agents.consistency_checker.invoke_structured", fake_invoke)
    agent = ConsistencyCheckerAgent(llm=object())

    issues = await agent.check(
        chapter={
            "chapter_number": 2,
            "content": "01234567890",
            "summary": "失忆剑客进入城门",
            "time_days": 1,
        },
        world_bible="世界观",
        characters=[{"name": "林寒", "role": "主角", "personality": "谨慎"}],
        outline=[],
        previous_chapters=[{"chapter_number": 1, "summary": "前章", "time_days": 3}],
        max_chapter_words=10,
        canon={
            "version": 1,
            "world_facts": [{"path": "城市", "value": "雾都", "source": "world_builder"}],
            "characters": {},
            "timeline": [],
            "facts": [],
        },
        creative_brief={
            "target_audience": "严肃悬疑读者",
            "ending_tone": "open",
        },
    )

    assert {issue["type"] for issue in issues} == {"timeline", "chapter_length"}
    assert all(issue["source"] == "deterministic" for issue in issues)
    assert "## 确定性分析报告" in captured["prompt"]
    assert "时间线冲突" in captured["prompt"]
    assert "## 结构化 Canon" in captured["prompt"]
    assert "城市: 雾都" in captured["prompt"]
    assert "目标读者：严肃悬疑读者" in captured["prompt"]
    assert "结局基调：开放式" in captured["prompt"]


async def test_scene_writer_clamps_requested_length_and_normalizes_content():
    class CaptureModel:
        prompt = ""

        async def ainvoke(self, prompt):
            self.prompt = prompt
            return AIMessage(content="  正文  ")

    model = CaptureModel()
    chapter = await SceneWriterAgent(llm=model).write_chapter(
        {
            "current_chapter": 1,
            "style": "gu_long",
            "novel_id": "",
            "max_chapter_words": 20,
            "chapter_plan": {
                "title": "雾起",
                "estimated_words": 200,
                "summary": "入城",
                "time_days": 0,
                "emotion": "紧张",
                "characters": ["林寒"],
            },
            "chapters": [],
            "world_bible": "城市: 雾都",
            "characters": [{"name": "林寒", "role": "主角"}],
            "outline": [],
            "creative_brief": {
                "target_audience": "成年武侠读者",
                "point_of_view": "multiple",
            },
            "memory_index": {
                "chapters": [{"chapter": 1, "title": "前章", "summary": "发现失踪王印"}],
                "arcs": [
                    {
                        "arc": 1,
                        "start_chapter": 1,
                        "end_chapter": 1,
                        "summary": "王印谜题已经建立",
                    }
                ],
            },
        }
    )

    assert "目标字数: 20字" in model.prompt
    assert chapter["content"] == "正文"
    assert chapter["word_count"] == 2
    assert chapter["time_days"] == 0
    assert chapter["emotion"] == "紧张"
    assert chapter["characters"] == ["林寒"]
    assert chapter["scene_plan"][0]["scene_number"] == 1
    assert "## 结构化 Canon" in model.prompt
    assert "## 场景执行计划" in model.prompt
    assert "城市: 雾都" in model.prompt
    assert "王印谜题已经建立" in model.prompt
    assert "目标读者：成年武侠读者" in model.prompt
    assert "叙事视角：多视角" in model.prompt


async def test_book_revision_writer_uses_original_draft_and_actual_previous_chapter():
    class CaptureModel:
        prompt = ""

        async def ainvoke(self, prompt):
            self.prompt = prompt
            return AIMessage(content="第二章返修稿")

    model = CaptureModel()
    await SceneWriterAgent(llm=model).write_chapter(
        {
            "current_chapter": 2,
            "style": "gu_long",
            "revision_notes": "修正第二章因果",
            "current_draft": {"chapter_number": 2, "content": "第二章旧终稿"},
            "chapter_plan": {"chapter": 2, "title": "第二章", "estimated_words": 100},
            "chapters": [
                {"chapter_number": 1, "content": "第一章正确结尾"},
                {"chapter_number": 2, "content": "第二章旧终稿"},
                {"chapter_number": 3, "content": "第三章不应作为前章"},
            ],
            "world_bible": "世界观",
            "characters": [],
            "outline": [],
            "scene_plan": [{"scene_number": 1, "goal": "返修", "estimated_words": 100}],
            "max_chapter_words": 100,
        }
    )

    assert "第二章旧终稿" in model.prompt
    assert "第一章正确结尾" in model.prompt
    assert "第三章不应作为前章" not in model.prompt


async def test_scene_rewriter_changes_only_selected_scene():
    class CaptureModel:
        prompt = ""

        async def ainvoke(self, prompt):
            self.prompt = prompt
            return AIMessage(content="第二场重写后正文。")

    model = CaptureModel()
    scene_plan = [
        {"scene_number": 1, "goal": "入城", "estimated_words": 50},
        {"scene_number": 2, "goal": "脱险", "estimated_words": 50},
        {"scene_number": 3, "goal": "追踪", "estimated_words": 50},
    ]
    original_drafts = [
        {"scene_number": 1, "content": "第一场原文。"},
        {"scene_number": 2, "content": "第二场原文。"},
        {"scene_number": 3, "content": "第三场原文。"},
    ]

    chapter = await SceneRewriterAgent(llm=model).rewrite_scene(
        {
            "current_chapter": 1,
            "style": "gu_long",
            "revision_scene_number": 2,
            "revision_notes": "加强追逐冲突",
            "current_draft": {
                "chapter_number": 1,
                "content": "第一场原文。\n\n第二场原文。\n\n第三场原文。",
                "scene_plan": scene_plan,
                "scene_drafts": original_drafts,
            },
            "world_bible": "城市: 雾都",
            "characters": [],
            "outline": [],
            "chapters": [],
        }
    )

    assert chapter["scene_drafts"][0] == original_drafts[0]
    assert chapter["scene_drafts"][1]["content"] == "第二场重写后正文。"
    assert chapter["scene_drafts"][2] == original_drafts[2]
    assert chapter["content"] == "第一场原文。\n\n第二场重写后正文。\n\n第三场原文。"
    assert "加强追逐冲突" in model.prompt
    assert "第一场原文。" in model.prompt
    assert "第三场原文。" in model.prompt
