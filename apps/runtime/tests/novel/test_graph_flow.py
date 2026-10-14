"""LangGraph 图全流程冒烟测试(假 LLM,不依赖 API Key)。

覆盖:
- 完整拓扑:世界观 → 角色 → 大纲 → 写作 → 润色 → 质检 → 人工审查(暂停)
- interrupt 恢复:approve 定稿 → END;修改意见 → 回写重写循环
- chapters reducer:每章仅终稿入列
"""

from langgraph.types import Command

# 首段流会依次经过的节点(human_review 被 interrupt 打断,不产生 update 事件)
EXPECTED_NODES = [
    "world_builder",
    "character_designer",
    "plot_planner",
    "scene_planner",
    "scene_writer",
    "style_editor",
    "consistency_checker",
]


def _patch_llms(monkeypatch, fake_llm) -> None:
    """把全部 Agent 的 LLM 工厂替换为共享假模型。"""
    for mod, attr in [
        ("agent_runtime.workflows.novel.agents.world_builder", "get_llm"),
        ("agent_runtime.workflows.novel.agents.character_designer", "get_llm"),
        ("agent_runtime.workflows.novel.agents.plot_planner", "get_analyzer_llm"),
        ("agent_runtime.workflows.novel.agents.scene_planner", "get_analyzer_llm"),
        ("agent_runtime.workflows.novel.agents.scene_writer", "get_llm"),
        ("agent_runtime.workflows.novel.agents.scene_rewriter", "get_llm"),
        ("agent_runtime.workflows.novel.agents.style_editor", "get_llm"),
        ("agent_runtime.workflows.novel.agents.consistency_checker", "get_analyzer_llm"),
    ]:
        monkeypatch.setattr(f"{mod}.{attr}", lambda **kw: fake_llm)


def _initial_state(total: int = 1) -> dict:
    return {
        "title": "雾中剑",
        "genre": "武侠",
        "inspiration": "失忆剑客寻找过去",
        "total_chapters": total,
        "style": "gu_long",
        "novel_id": "",  # 空串:跳过持久化,纯内存跑图
        "current_chapter": 1,
        "current_phase": "writing",
        "max_revision_attempts": 2,
        "chapters": [],
    }


async def _drive(graph, config, payload) -> list[str]:
    """同步迭代一段流,返回本段执行的节点名序列(剔除 orchestrator 调度中枢)。"""
    visited: list[str] = []
    async for update in graph.astream(payload, config, stream_mode="updates"):
        visited.extend(n for n in (update or {}) if not n.startswith("__"))
    return [n for n in visited if n != "orchestrator"]


async def test_full_flow_with_auto_approve(monkeypatch, fake_llm):
    """1 章全流程:7 次 LLM 调用 + interrupt 暂停 + approve 定稿 → END。"""
    _patch_llms(monkeypatch, fake_llm)

    from agent_runtime.workflows.novel.graph.builder import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": "t-auto"}}

    visited = await _drive(graph, config, _initial_state())
    assert visited == EXPECTED_NODES

    # 图暂停在 human_review
    snap = await graph.aget_state(config)
    assert "human_review" in snap.next
    draft = snap.values.get("current_draft") or {}
    assert "润色" in draft["content"]  # 经过 StyleEditor
    assert draft["scene_plan"][0]["goal"] == "进入雾都"

    # approve → 定稿 → 全书终审 → END
    tail = await _drive(graph, config, Command(resume="approve"))
    assert tail == ["human_review", "book_auditor"]

    snap_final = await graph.aget_state(config)
    assert snap_final.next == ()  # 图已运行至 END
    final = snap_final.values
    assert len(final["chapters"]) == 1
    assert final["current_chapter"] == 2  # 1 章 total → 推进后 END
    assert final["chapters"][0]["status"] == "final"
    assert final["chapters"][0]["summary"].startswith("林寒穿过雾都城门")
    assert final["chapters"][0]["digest_version"] == "chapter-digest-v1"
    assert final["canon"]["timeline"][0]["status"] == "final"
    assert final["canon"]["facts"][0]["id"] == "chapter:1:summary"
    assert any(item["id"].startswith("chapter:1:extracted:") for item in final["canon"]["facts"])
    assert final["book_audit_completed"] is True
    assert final["book_audit"]["judge_scores"]["plot_coherence"] == 88


async def test_digest_failure_does_not_finalize_or_advance_chapter(monkeypatch, fake_llm):
    """终稿提炼失败时保留人工审查检查点，不写入部分 Canon 或章节。"""
    from agent_runtime.execution.structured import StructuredOutputError
    from agent_runtime.workflows.novel.graph import nodes
    from agent_runtime.workflows.novel.graph.builder import build_graph

    class FailingDigest:
        async def digest(self, **kwargs):
            raise StructuredOutputError("digest invalid")

    _patch_llms(monkeypatch, fake_llm)
    monkeypatch.setattr(nodes, "ChapterDigestAgent", FailingDigest)
    graph = build_graph()
    config = {"configurable": {"thread_id": "t-digest-failure"}}
    await _drive(graph, config, _initial_state())

    await _drive(graph, config, Command(resume="approve"))

    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("human_review",)
    assert snapshot.values["current_chapter"] == 1
    assert snapshot.values.get("chapters") == []
    assert snapshot.values["canon"]["timeline"][0]["status"] == "planned"

    monkeypatch.setattr(
        nodes,
        "ChapterDigestAgent",
        __import__(
            "agent_runtime.workflows.novel.agents.chapter_digest", fromlist=["ChapterDigestAgent"]
        ).ChapterDigestAgent,
    )
    await _drive(graph, config, Command(resume="approve"))
    final = await graph.aget_state(config)
    assert final.next == ()
    assert len(final.values["chapters"]) == 1


async def test_planning_reviews_pause_before_blueprint_and_prose(monkeypatch, fake_llm):
    """启用规划审批时，蓝图与分镜都必须经人工确认后才生成正文。"""
    _patch_llms(monkeypatch, fake_llm)

    from agent_runtime.workflows.novel.graph.builder import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": "t-planning-review"}}
    initial = {**_initial_state(), "planning_review_enabled": True}

    assert await _drive(graph, config, initial) == [
        "world_builder",
        "character_designer",
        "plot_planner",
    ]
    blueprint = await graph.aget_state(config)
    assert blueprint.next == ("blueprint_review",)
    assert not blueprint.values.get("current_draft")

    blueprint_resume = {
        "world_bible": blueprint.values["world_bible"] + "\n审阅标记: 已确认",
        "characters": blueprint.values["characters"],
        "outline": blueprint.values["outline"],
    }
    assert await _drive(graph, config, Command(resume=blueprint_resume)) == [
        "blueprint_review",
        "scene_planner",
    ]
    scene = await graph.aget_state(config)
    assert scene.next == ("scene_review",)
    assert "审阅标记" in scene.values["world_bible"]
    assert not scene.values.get("current_draft")

    assert await _drive(
        graph,
        config,
        Command(resume={"scene_plan": scene.values["scene_plan"]}),
    ) == ["scene_review", "scene_writer", "style_editor", "consistency_checker"]
    assert (await graph.aget_state(config)).next == ("human_review",)

    assert await _drive(graph, config, Command(resume="approve")) == [
        "human_review",
        "book_auditor",
    ]
    assert (await graph.aget_state(config)).next == ()


async def test_revision_loop_on_human_feedback(monkeypatch):
    """人工修改意见触发回写:scene_writer 重写 → 润色 → 质检 → 再审查 → 定稿。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    responses = [
        "```yaml\n世界观名称: 测试\n```",
        "- name: 林寒\n  role: 主角\n",
        "- chapter: 1\n  title: 雾起\n  estimated_words: 100\n",
        "- scene_number: 1\n  goal: 进入雾都\n  conflict: 城门盘查\n"
        "  turn: 发现追兵\n  location: 城门\n  characters: [林寒]\n"
        "  emotion: 紧张\n  estimated_words: 100\n",
        "初稿正文。",
        "初稿润色。",
        "[]",  # 第 1 轮
        "重写正文。",
        "重写润色。",
        "[]",  # 人工反馈后重写
    ]
    fake = FakeListChatModel(responses=responses)
    _patch_llms(monkeypatch, fake)

    from agent_runtime.workflows.novel.graph.builder import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": "t-revise"}}

    visited = await _drive(graph, config, _initial_state())
    assert visited == EXPECTED_NODES
    initial_scene_plan = (await graph.aget_state(config)).values["scene_plan"]

    # 修改意见 → 回写循环(重写→润色→质检,再次于 human_review 处打断)
    visited2 = await _drive(graph, config, Command(resume="结尾太仓促,补充决战场面"))
    assert visited2 == ["human_review", "scene_writer", "style_editor", "consistency_checker"]
    assert "human_review" in (await graph.aget_state(config)).next

    snap = await graph.aget_state(config)
    assert "重写" in (snap.values.get("current_draft") or {}).get("content", "")
    assert snap.values.get("revision_count") == 1
    assert snap.values["scene_plan"] == initial_scene_plan

    # 最终 approve → 定稿
    tail = await _drive(graph, config, Command(resume="approve"))
    assert tail == ["human_review", "book_auditor"]
    final = (await graph.aget_state(config)).values
    assert len(final["chapters"]) == 1
    assert "重写" in final["chapters"][0]["content"]
    assert final["revision_count"] == 0  # 定稿清零


async def test_canon_update_rechecks_current_draft_without_rewriting(monkeypatch):
    """人工治理 Canon 后只重新质检,正文保持逐字不变并再次暂停审查。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    responses = [
        "```yaml\n城市: 雾都\n```",
        "- name: 林寒\n  role: 主角\n",
        "- chapter: 1\n  title: 雾起\n  estimated_words: 100\n",
        "- scene_number: 1\n  goal: 进入雾都\n  conflict: 城门盘查\n"
        "  turn: 发现追兵\n  location: 城门\n  characters: [林寒]\n"
        "  emotion: 紧张\n  estimated_words: 100\n",
        "初稿正文。",
        "初稿润色。",
        "[]",
        "[]",
    ]
    fake = FakeListChatModel(responses=responses)
    _patch_llms(monkeypatch, fake)

    from agent_runtime.workflows.novel.graph.builder import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": "t-canon-update"}}
    await _drive(graph, config, _initial_state())
    before = (await graph.aget_state(config)).values["current_draft"]["content"]

    visited = await _drive(
        graph,
        config,
        Command(
            resume={
                "action": "canon_update",
                "operation": {
                    "action": "upsert_fact",
                    "target_type": "fact",
                    "subject": "守夜人",
                    "kind": "organization",
                    "value": "只在午夜换岗",
                    "reason": "固定当前章节的时间约束",
                },
            }
        ),
    )

    assert visited == ["human_review", "consistency_checker"]
    snapshot = await graph.aget_state(config)
    assert "human_review" in snapshot.next
    assert snapshot.values["current_draft"]["content"] == before
    assert snapshot.values.get("chapters") == []
    assert snapshot.values["canon"]["facts"][0]["value"] == "只在午夜换岗"
    assert snapshot.values["canon"]["audit"][0]["reason"] == "固定当前章节的时间约束"


async def test_narrative_thread_lifecycle_runs_across_two_chapters(monkeypatch):
    """主要线程随章节定稿从 planned → open → resolved。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    responses = [
        "```yaml\n城市: 雾都\n```",
        "- name: 林寒\n  role: 主角\n",
        "- chapter: 1\n  title: 空印盒\n  summary: 林寒发现王印失踪\n"
        "  estimated_words: 100\n  narrative_beats:\n"
        "    - thread: 失踪王印\n      action: setup\n      description: 发现空印盒\n"
        "      priority: major\n      due_chapter: 2\n"
        "- chapter: 2\n  title: 剑鞘之谜\n  summary: 林寒找回王印\n"
        "  estimated_words: 100\n  narrative_beats:\n"
        "    - thread: 失踪王印\n      action: resolve\n      description: 揭示王印藏处\n"
        "      priority: major\n",
        "- scene_number: 1\n  goal: 检查印盒\n  conflict: 守卫阻拦\n"
        "  turn: 印盒为空\n  location: 王库\n  characters: [林寒]\n"
        "  emotion: 惊疑\n  estimated_words: 100\n  narrative_beats:\n"
        "    - thread: 失踪王印\n      action: setup\n      description: 发现空印盒\n",
        "第一章正文。",
        "第一章润色。",
        "[]",
        "- scene_number: 1\n  goal: 搜查剑鞘\n  conflict: 追兵逼近\n"
        "  turn: 王印现身\n  location: 暗室\n  characters: [林寒]\n"
        "  emotion: 紧张\n  estimated_words: 100\n  narrative_beats:\n"
        "    - thread: 失踪王印\n      action: resolve\n      description: 揭示王印藏处\n",
        "第二章正文。",
        "第二章润色。",
        "[]",
    ]
    models = {
        "world": FakeListChatModel(responses=[responses[0]]),
        "character": FakeListChatModel(responses=[responses[1]]),
        "plot": FakeListChatModel(responses=[responses[2]]),
        "scene": FakeListChatModel(responses=[responses[3], responses[7]]),
        "writer": FakeListChatModel(responses=[responses[4], responses[8]]),
        "style": FakeListChatModel(responses=[responses[5], responses[9]]),
        "checker": FakeListChatModel(responses=[responses[6], responses[10]]),
    }
    monkeypatch.setattr("agent_runtime.workflows.novel.agents.world_builder.get_llm", lambda **kwargs: models["world"])
    monkeypatch.setattr(
        "agent_runtime.workflows.novel.agents.character_designer.get_llm", lambda **kwargs: models["character"]
    )
    monkeypatch.setattr(
        "agent_runtime.workflows.novel.agents.plot_planner.get_analyzer_llm", lambda **kwargs: models["plot"]
    )
    monkeypatch.setattr(
        "agent_runtime.workflows.novel.agents.scene_planner.get_analyzer_llm", lambda **kwargs: models["scene"]
    )
    monkeypatch.setattr("agent_runtime.workflows.novel.agents.scene_writer.get_llm", lambda **kwargs: models["writer"])
    monkeypatch.setattr("agent_runtime.workflows.novel.agents.style_editor.get_llm", lambda **kwargs: models["style"])
    monkeypatch.setattr(
        "agent_runtime.workflows.novel.agents.consistency_checker.get_analyzer_llm",
        lambda **kwargs: models["checker"],
    )

    from agent_runtime.workflows.novel.graph.builder import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": "t-thread-lifecycle"}}
    await _drive(graph, config, _initial_state(total=2))
    first = await graph.aget_state(config)
    assert first.values["canon"]["narrative_threads"][0]["status"] == "planned"

    visited = await _drive(graph, config, Command(resume="approve"))
    second = await graph.aget_state(config)
    high_issues = [issue for issue in second.values.get("issues") or [] if issue.get("severity") == "high"]
    assert not high_issues, (
        high_issues,
        second.values.get("chapter_plan"),
        (second.values.get("current_draft") or {}).get("narrative_beats"),
        (second.values.get("current_draft") or {}).get("scene_plan"),
    )
    assert visited == [
        "human_review",
        "scene_planner",
        "scene_writer",
        "style_editor",
        "consistency_checker",
    ], (visited, second.values.get("issues"))
    thread = second.values["canon"]["narrative_threads"][0]
    assert thread["status"] == "open"
    assert thread["beats"][0]["status"] == "completed"

    await _drive(graph, config, Command(resume="approve"))
    final = await graph.aget_state(config)
    thread = final.values["canon"]["narrative_threads"][0]
    assert final.next == ()
    assert thread["status"] == "resolved"
    assert thread["resolved_chapter"] == 2


async def test_automatic_revision_count_stops_at_limit(monkeypatch):
    """连续 high 问题只允许配置次数的自动重写,随后转人工审查。"""
    from agent_runtime.workflows.novel.graph import nodes

    class AlwaysHighChecker:
        async def check(self, **kwargs):
            return [
                {
                    "type": "设定冲突",
                    "description": "持续冲突",
                    "chapter": 1,
                    "severity": "high",
                    "suggestion": "重写",
                }
            ]

    monkeypatch.setattr(nodes, "ConsistencyCheckerAgent", AlwaysHighChecker)
    state = {
        "current_draft": {"chapter_number": 1, "content": "正文"},
        "current_phase": "consistency_check",
        "revision_count": 0,
        "max_revision_attempts": 2,
    }

    first = await nodes.consistency_checker_node(state)
    assert first["revision_count"] == 1
    assert first["current_phase"] == "writing"

    state.update(first)
    state["current_phase"] = "consistency_check"
    second = await nodes.consistency_checker_node(state)
    assert second["revision_count"] == 2
    assert second["current_phase"] == "writing"

    state.update(second)
    state["current_phase"] = "consistency_check"
    third = await nodes.consistency_checker_node(state)
    assert "revision_count" not in third
    assert "current_phase" not in third


async def test_quality_gate_rewrites_low_quality_draft_and_escalates_at_limit(monkeypatch):
    """无一致性问题时，低质量稿自动回写；达到上限后交给人工审查。"""
    from agent_runtime.workflows.novel.graph import nodes

    class PassingChecker:
        async def check(self, **kwargs):
            return []

    monkeypatch.setattr(nodes, "ConsistencyCheckerAgent", PassingChecker)
    state = {
        "current_draft": {"chapter_number": 1, "content": "", "summary": ""},
        "current_phase": "consistency_check",
        "revision_count": 0,
        "max_revision_attempts": 1,
        "quality_gate_threshold": 70,
    }

    first = await nodes.consistency_checker_node(state)

    assert first["current_phase"] == "writing"
    assert first["revision_count"] == 1
    assert first["quality_report"]["status"] == "rewrite"
    assert "自动质量门未通过" in first["revision_notes"]

    state.update(first)
    state["current_phase"] = "consistency_check"
    second = await nodes.consistency_checker_node(state)

    assert second["quality_report"]["status"] == "escalated"
    assert "current_phase" not in second


async def test_chapter_vector_memory_is_written_only_after_approval(monkeypatch, fake_llm):
    """草稿不进入长期记忆,人工批准后以确定 ID 写入一次终稿。"""
    from agent_runtime.workflows.novel.graph import nodes
    from agent_runtime.workflows.novel.graph.builder import build_graph

    records: list[dict] = []
    from types import SimpleNamespace

    snapshots = []
    store = SimpleNamespace(
        save_chapter=lambda **kwargs: None,
        save_progress=lambda **kwargs: None,
        save_memory_snapshot=lambda *args, **kwargs: snapshots.append(kwargs),
    )

    class RecordingMemory:
        def __init__(self, novel_id: str):
            self.novel_id = novel_id

        def get_chapter_memory(self, chapter_number: int, k: int = 3):
            return []

        def store_content(self, content: str, metadata=None, content_id: str | None = None):
            records.append(
                {
                    "novel_id": self.novel_id,
                    "content": content,
                    "metadata": metadata or {},
                    "content_id": content_id,
                }
            )

        def store_hierarchical_memory(self, content: str, *, content_hash: str):
            records.append(
                {
                    "novel_id": self.novel_id,
                    "content": content,
                    "metadata": {"type": "hierarchical_memory", "content_hash": content_hash},
                    "content_id": f"{self.novel_id}:hierarchical-memory",
                }
            )

    _patch_llms(monkeypatch, fake_llm)
    for module in [
        "agent_runtime.workflows.novel.agents.world_builder",
        "agent_runtime.workflows.novel.agents.character_designer",
        "agent_runtime.workflows.novel.agents.plot_planner",
        "agent_runtime.workflows.novel.agents.scene_writer",
    ]:
        monkeypatch.setattr(f"{module}.NovelMemory", RecordingMemory)
    monkeypatch.setattr(nodes, "NovelMemory", RecordingMemory, raising=False)
    monkeypatch.setattr(nodes, "_novel_store", lambda: store)

    state = _initial_state()
    state["novel_id"] = "memory-test"
    graph = build_graph()
    config = {"configurable": {"thread_id": "memory-test"}}

    await _drive(graph, config, state)
    assert not [r for r in records if r["metadata"].get("type") == "chapter"]

    await _drive(graph, config, Command(resume="approve"))
    chapter_records = [r for r in records if r["metadata"].get("type") == "chapter"]
    assert len(chapter_records) == 1
    assert chapter_records[0]["metadata"]["status"] == "final"
    assert chapter_records[0]["content_id"] == "memory-test:chapter:1"
    canon_records = [r for r in records if r["metadata"].get("type") == "canon"]
    assert len(canon_records) == 1
    assert canon_records[0]["content_id"] == "memory-test:canon"
    hierarchy_records = [r for r in records if r["metadata"].get("type") == "hierarchical_memory"]
    assert len(hierarchy_records) == 1
    assert hierarchy_records[0]["content_id"] == "memory-test:hierarchical-memory"
    assert snapshots[-1]["payload"]["completed_chapters"] == 1


async def test_store_failure_prevents_chapter_finalization(
    monkeypatch,
    fake_llm,
):
    """权威存储写入失败时保留可恢复人工审查现场,稍后可重试定稿。"""
    from agent_runtime.workflows.novel.graph import nodes
    from agent_runtime.workflows.novel.graph.builder import build_graph

    digest_calls = 0
    original_digest = nodes.ChapterDigestAgent.digest

    async def counted_digest(self, **kwargs):
        nonlocal digest_calls
        digest_calls += 1
        return await original_digest(self, **kwargs)

    monkeypatch.setattr(nodes.ChapterDigestAgent, "digest", counted_digest)

    class ToggleStore:
        def __init__(self):
            self.fail = True

        def save_chapter(self, **kwargs):
            if self.fail:
                raise OSError("disk full")

        def save_progress(self, **kwargs):
            if self.fail:
                raise AssertionError("save_progress 不应在章节写入失败后执行")

    failing_store = ToggleStore()

    _patch_llms(monkeypatch, fake_llm)
    monkeypatch.setattr(nodes, "_novel_store", lambda: failing_store)
    monkeypatch.setattr(nodes, "NovelMemory", lambda novel_id: None)

    state = _initial_state()
    state["novel_id"] = "store-failure"
    graph = build_graph()
    config = {"configurable": {"thread_id": "store-failure"}}
    await _drive(graph, config, state)

    await _drive(graph, config, Command(resume="approve"))

    snapshot = await graph.aget_state(config)
    assert "human_review" in snapshot.next
    assert snapshot.values.get("chapters") == []
    assert snapshot.values["canon"]["timeline"][0]["status"] == "planned"
    assert snapshot.values["current_draft"]["digest_version"] == "chapter-digest-v1"
    assert digest_calls == 1

    failing_store.fail = False
    await _drive(graph, config, Command(resume="approve"))
    final = await graph.aget_state(config)
    assert final.next == ()
    assert len(final.values.get("chapters") or []) == 1
    assert final.values["canon"]["timeline"][0]["status"] == "final"
    assert digest_calls == 1
