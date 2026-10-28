"""Scoped legacy workflow execution, durable checkpoints, and fencing."""

import os
from types import SimpleNamespace

import pytest


def test_novel_dependencies_are_scoped_without_legacy_initialization(monkeypatch):
    from agent_runtime.execution import models as llm
    from agent_runtime.workflows.novel.context import novel_context
    from agent_runtime.workflows.novel.graph import nodes
    from agent_runtime.workflows.novel.memory.access import NovelMemory

    store, memory, models = object(), object(), SimpleNamespace(chat=lambda purpose: purpose)
    with novel_context(store, lambda identity: memory, models):
        assert nodes._novel_store() is store
        assert NovelMemory("project") is memory
        assert llm.get_llm(purpose="analysis") == "analysis"


def test_runtime_never_falls_back_to_legacy_sqlite_or_chroma_without_novel_scope(novel_store):
    from agent_runtime.execution.context import _EXECUTION
    from agent_runtime.workflows.novel.graph import nodes
    from agent_runtime.workflows.novel.memory.access import NovelMemory

    token = _EXECUTION.set(novel_store.execution)
    try:
        with pytest.raises(RuntimeError, match="scope"):
            nodes._novel_store()
        with pytest.raises(RuntimeError, match="scope"):
            NovelMemory(novel_store.project_id)
    finally:
        _EXECUTION.reset(token)


@pytest.fixture
def novel_store():
    from agent_runtime.execution.context import ExecutionContext
    from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
    from agent_runtime.infrastructure.postgres.novel import PostgresNovelStore
    from tests.infrastructure.test_mq_store import command

    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    runtime = RuntimeMQStore(url)
    task = command()
    runtime.accept_task(task)
    job = runtime.claim_next(task.workspace_id, "runtime", "novel-test", 60)
    store = PostgresNovelStore(
        ExecutionContext(job, runtime, SimpleNamespace(worker_id="novel-test", embedding_model="fake"))
    )
    from tests.fixtures.prompts import prompt_versions

    store.execution.prompt_versions.update(prompt_versions())
    store.initialize({"novel_id": task.project_id, "title": "Test"})
    yield store
    runtime.close()


def test_store_scope_versions_and_fencing(novel_store):
    from agent_runtime.execution.leases import FencingTokenError

    identity = novel_store.project_id
    with pytest.raises(ValueError, match="scope"):
        novel_store.save_chapter("another-project", 1, content="foreign")
    first = novel_store.save_chapter_version(identity, 1, source="review", content="draft")
    assert novel_store.save_chapter_version(identity, 1, source="review", content="draft") == first
    second = novel_store.save_chapter_version(identity, 1, source="review", content="revision")
    assert second["version_number"] == first["version_number"] + 1
    novel_store.execution.store.finish(novel_store.execution.job, "novel-test", "completed", {})
    with pytest.raises(FencingTokenError):
        novel_store.save_chapter(identity, 1, content="stale")


async def test_postgres_saver_restores_interrupt_and_pending_writes_in_fresh_worker(novel_store):
    from typing import TypedDict

    from langgraph.graph import StateGraph
    from langgraph.types import Command, interrupt

    from agent_runtime.infrastructure.postgres.checkpoints import NovelPostgresSaver

    class State(TypedDict):
        text: str

    calls = []

    def produce(state):
        calls.append("produce")
        return {"text": state["text"] + " written"}

    def review(state):
        response = interrupt({"text": state["text"]})
        return {"text": state["text"] + " " + response}

    def graph(saver):
        builder = StateGraph(State)
        builder.add_node("produce", produce)
        builder.add_node("review", review)
        builder.set_entry_point("produce")
        builder.add_edge("produce", "review")
        builder.set_finish_point("review")
        return builder.compile(checkpointer=saver)

    config = {"configurable": {"thread_id": novel_store.project_id}}
    first = graph(NovelPostgresSaver(novel_store))
    await first.ainvoke({"text": "chapter"}, config)
    checkpoint = await first.aget_state(config)
    assert checkpoint.next == ("review",)
    saver = NovelPostgresSaver(novel_store)
    assert saver.get_tuple(checkpoint.config).pending_writes
    from agent_runtime.execution.context import ExecutionContext
    from agent_runtime.infrastructure.postgres.novel import PostgresNovelStore
    from tests.infrastructure.test_mq_store import command

    runtime = novel_store.execution.store
    job = novel_store.execution.job
    runtime.finish(
        job, "novel-test", "waiting_review", {"checkpoint_id": checkpoint.config["configurable"]["checkpoint_id"]}
    )
    runtime.accept_control(command("job.resume", job_id=job.command.job_id, workspace_id=job.command.workspace_id))
    fresh = runtime.claim_next(job.command.workspace_id, "runtime", "fresh-worker", 60)
    assert fresh.fencing_token > job.fencing_token
    restarted = PostgresNovelStore(ExecutionContext(fresh, runtime, SimpleNamespace(worker_id="fresh-worker")))
    resumed = await graph(NovelPostgresSaver(restarted)).ainvoke(Command(resume="approved"), config)
    assert resumed["text"] == "chapter written approved"
    assert calls == ["produce"]


def test_runtime_memory_is_project_scoped_and_failed_rebuild_is_atomic(novel_store):
    from agent_runtime.execution.context import ExecutionContext
    from agent_runtime.infrastructure.postgres.novel import PostgresNovelStore
    from agent_runtime.infrastructure.postgres.novel_memory import PostgresNovelMemory
    from tests.infrastructure.test_mq_store import command

    class Models:
        fail = False

        async def embed(self, texts):
            if self.fail:
                raise OSError("fake embedding failure")
            return [[0.1] * 1536 for _ in texts]

    models = Models()
    memory = PostgresNovelMemory(novel_store, models)
    memory.store_content("own fact", metadata={"type": "canon"}, content_id="fact")
    assert memory.search_similar("fact", where={"type": "canon"})[0]["content"] == "own fact"
    models.fail = True
    with pytest.raises(OSError):
        memory.rebuild([{"id": "new", "content": "new", "metadata": {}}])
    assert memory.list_records()[0]["content"] == "own fact"
    assert memory.list_records()[0]["metadata"]["embedding_model"] == "fake"
    assert memory.list_records()[0]["metadata"]["index_version"] == "novel-memory@1"
    task = command(workspace_id=novel_store.workspace_id).model_copy(update={"project_id": "other-project"})
    runtime = novel_store.execution.store
    runtime.accept_task(task)
    job = runtime.claim_next(task.workspace_id, "runtime", "other-worker", 60)
    other = PostgresNovelStore(ExecutionContext(job, runtime, SimpleNamespace(worker_id="other-worker")))
    other.initialize({"novel_id": task.project_id})
    assert PostgresNovelMemory(other, models).list_records() == []
    with runtime.scope("another-workspace") as cursor:
        cursor.execute("SELECT count(*) FROM memory_nodes WHERE project_id=%s", (novel_store.project_id,))
        assert cursor.fetchone()[0] == 0


def test_novel_semantic_search_excludes_same_dimension_foreign_and_unknown_models(novel_store):
    from agent_runtime.infrastructure.postgres.jobs import _json
    from agent_runtime.infrastructure.postgres.novel_memory import PostgresNovelMemory

    class Models:
        async def embed(self, texts):
            return [[0.1] * 1536 for _ in texts]

    memory = PostgresNovelMemory(novel_store, Models())
    memory.store_content("compatible", content_id="known")
    memory.store_content("foreign", content_id="foreign")
    memory.store_content("legacy", content_id="legacy")
    with novel_store.transaction(write=True) as cursor:
        cursor.execute(
            "UPDATE memory_nodes SET metadata=metadata || %s WHERE project_id=%s AND source_id='foreign'",
            (_json({"embedding_model": "other-model"}), novel_store.project_id),
        )
        cursor.execute(
            "UPDATE memory_nodes SET metadata=metadata - 'embedding_model' - 'index_version' "
            "WHERE project_id=%s AND source_id='legacy'",
            (novel_store.project_id,),
        )
    assert [item["content"] for item in memory.search_similar("fact")] == ["compatible"]
    memory.rebuild(memory.list_records())
    assert {item["content"] for item in memory.search_similar("fact")} == {"compatible", "foreign", "legacy"}


def test_expired_lease_rejects_checkpoints_and_artifact_state_events(novel_store):
    from langgraph.checkpoint.base import empty_checkpoint

    from agent_runtime.execution.leases import FencingTokenError
    from agent_runtime.infrastructure.postgres.checkpoints import NovelPostgresSaver

    with novel_store.execution.store.scope(novel_store.workspace_id) as cursor:
        cursor.execute(
            "UPDATE runtime_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (novel_store.execution.job.command.job_id,),
        )
    saver = NovelPostgresSaver(novel_store)
    config = {"configurable": {"thread_id": novel_store.project_id}}
    with pytest.raises(FencingTokenError):
        saver.put(config, empty_checkpoint(), {}, {})
    with pytest.raises(FencingTokenError):
        novel_store.publish_state({"bucket": "test", "object_key": "test", "sha256": "a" * 64}, "a" * 64)


def test_frozen_prompt_overrides_preserve_required_template_variables(novel_store):
    from agent_runtime.execution.context import _EXECUTION, ExecutionContext
    from agent_runtime.prompts.loader import fill_template
    from tests.fixtures.prompts import get_variables

    variables = {key: "value" for key in get_variables("world_builder")}
    context = ExecutionContext(
        novel_store.execution.job,
        novel_store.execution.store,
        novel_store.execution.settings,
        prompt_versions={},
    )
    token = _EXECUTION.set(context)
    try:
        from agent_runtime.prompts.renderer import PromptVersion, prompt_hash
        from tests.fixtures.prompts import prompt_versions

        raw = prompt_versions()["world_builder"].model_dump(mode="json")
        raw["messages"][0]["content"] = "Frozen " + " ".join("{" + key + "}" for key in variables)
        raw["content_hash"] = prompt_hash(raw)
        context.prompt_versions["world_builder"] = PromptVersion.model_validate(raw)
        assert fill_template("world_builder", **variables)[0]["content"].startswith("Frozen ")
        raw["messages"][0]["content"] = "Broken {title}"
        raw["content_hash"] = prompt_hash(raw)
        with pytest.raises(ValueError, match="variables"):
            PromptVersion.model_validate(raw)
    finally:
        _EXECUTION.reset(token)


def test_explicit_knowledge_pack_is_retrievable_only_by_bound_project(novel_store):
    from uuid import uuid4

    from agent_runtime.infrastructure.postgres.jobs import _json
    from agent_runtime.infrastructure.postgres.novel_memory import PostgresNovelMemory

    class Models:
        async def embed(self, texts):
            return [[0.1] * 1536 for _ in texts]

    identity = str(uuid4())
    with novel_store.execution.store.scope(novel_store.workspace_id) as cursor:
        cursor.execute(
            "INSERT INTO knowledge_packs(id,workspace_id,source_project_id,name,nodes,content_hash) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                identity,
                novel_store.workspace_id,
                "other-project",
                "Pack",
                _json([{"id": "shared", "content": "shared fact", "metadata": {"type": "canon"}}]),
                "a" * 64,
            ),
        )
    memory = PostgresNovelMemory(novel_store, Models())
    assert memory.search_similar("shared") == []
    with novel_store.execution.store.scope(novel_store.workspace_id) as cursor:
        cursor.execute(
            "INSERT INTO knowledge_pack_bindings(workspace_id,target_project_id,pack_id) VALUES(%s,%s,%s)",
            (novel_store.workspace_id, novel_store.project_id, identity),
        )
    result = memory.search_similar("shared", where={"type": "canon"})
    assert result[0]["content"] == "shared fact"
    assert result[0]["metadata"]["knowledge_pack_id"] == identity
    second_pack = str(uuid4())
    metadata = {**memory.embedding_identity(), "index_version": "memory@1", "type": "canon"}
    with novel_store.execution.store.scope(novel_store.workspace_id) as cursor:
        cursor.execute(
            "INSERT INTO knowledge_packs(id,workspace_id,source_project_id,name,nodes,content_hash) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                second_pack,
                novel_store.workspace_id,
                "generic-project",
                "Generic pack",
                _json(
                    [
                        {
                            "id": "cached",
                            "content": "generic shared fact",
                            "metadata": metadata,
                            "embedding": [0.1] * 1536,
                        }
                    ]
                ),
                "b" * 64,
            ),
        )
        cursor.execute(
            "INSERT INTO knowledge_pack_bindings(workspace_id,target_project_id,pack_id) VALUES(%s,%s,%s)",
            (novel_store.workspace_id, novel_store.project_id, second_pack),
        )
    assert "generic shared fact" in {item["content"] for item in memory.search_similar("shared")}


def test_project_execution_lock_rejects_concurrent_attempt_and_cancellation_blocks_writes(novel_store):
    from agent_runtime.execution.errors import JobCancelled, RetryableTaskError

    with novel_store.project_lock(), pytest.raises(RetryableTaskError), novel_store.project_lock():
        raise AssertionError("second attempt acquired project lock")
    with novel_store.execution.store.scope(novel_store.workspace_id) as cursor:
        cursor.execute(
            "UPDATE runtime_jobs SET status='cancel_requested' WHERE id=%s", (novel_store.execution.job.command.job_id,)
        )
    with pytest.raises(JobCancelled):
        novel_store.save_chapter(novel_store.project_id, 1, content="cancelled")


async def test_synchronous_memory_embedding_does_not_block_worker_heartbeat(monkeypatch):
    import asyncio
    import threading

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from agent_runtime.workflows.novel.agents.world_builder import WorldBuilderAgent

    release = threading.Event()
    observed = []

    class Memory:
        def __init__(self, identity):
            pass

        def store_content(self, *args, **kwargs):
            observed.append(release.wait(1))

    monkeypatch.setattr("agent_runtime.workflows.novel.agents.world_builder.NovelMemory", Memory)

    async def heartbeat():
        await asyncio.sleep(0.01)
        release.set()

    pulse = asyncio.create_task(heartbeat())
    await WorldBuilderAgent(llm=FakeListChatModel(responses=["world: city"]), novel_id="project").generate(
        title="Test", genre="fiction", inspiration="story"
    )
    await pulse
    assert release.is_set()
    assert observed == [True]


def test_novel_migration_enforces_rls_and_persists_pending_writes():
    from pathlib import Path

    migration = Path(__file__).resolve().parents[4] / "infra/migrations/migrations/runtime/006_novel_workflows.sql"
    sql = migration.read_text()
    for table in ("novel_projects", "novel_records", "novel_checkpoints", "novel_checkpoint_writes", "novel_memories"):
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
    assert "vector(1536)" in sql


def test_saver_channel_versions_accept_imported_sqlite_versions(novel_store):
    from agent_runtime.infrastructure.postgres.checkpoints import NovelPostgresSaver

    saver = NovelPostgresSaver(novel_store)
    previous = "00000000000000000000000000000009.0.5000000000000000"
    assert saver.get_next_version(previous, None) > previous


def test_state_envelope_contains_scoped_usage_and_sanitized_call_traces(novel_store):
    runtime = novel_store.execution.store
    runtime.record_model_call(
        novel_store.execution.job,
        "novel-test",
        "analysis",
        "fake",
        "test-model",
        "a" * 64,
        {"input_tokens": 12, "output_tokens": 4},
        {"agent": "checker", "prompt": "SECRET TEXT"},
    )
    envelope = novel_store.envelope()
    assert envelope["usage"][0]["total_tokens"] == 16
    assert envelope["traces"][0]["prompt_hash"] == "a" * 64
    assert "SECRET TEXT" not in str(envelope["traces"])


async def test_runtime_executes_full_graph_reviews_candidates_canon_revisions_and_audit(
    novel_store,
    monkeypatch,
    fake_llm,
):
    import hashlib
    import json

    from agent_runtime.execution.context import _EXECUTION
    from agent_runtime.execution.errors import WaitingReview
    from agent_runtime.infrastructure.storage.artifacts import ArtifactReference
    from agent_runtime.workflows.novel import workflow as novel_workflow
    from tests.novel.test_graph_flow import _patch_llms

    _patch_llms(monkeypatch, fake_llm)
    uploaded = []

    class Artifacts:
        def __init__(self, settings):
            pass

        def upload(self, workspace, project, category, resource, filename, data, **kwargs):
            uploaded.append(json.loads(data))
            return ArtifactReference(
                "test",
                f"{workspace}/{project}/{resource}/{kwargs['version_id']}/{filename}",
                hashlib.sha256(data).hexdigest(),
                len(data),
                "application/json",
                "",
            )

    class Models:
        def __init__(self, settings):
            pass

        def chat(self, purpose):
            return fake_llm

        async def embed(self, texts):
            return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr(novel_workflow, "S3ArtifactStore", Artifacts)
    monkeypatch.setattr(novel_workflow, "RuntimeModels", Models)
    command = novel_store.execution.job.command
    request = {
        "project_id": command.project_id,
        "title": "Novel",
        "objective": "story",
        "document_id": "book",
        "version": 1,
        "document_type": "novel",
        "operation": "run",
        "input": {
            "genre": "wuxia",
            "inspiration": "fog",
            "total_chapters": 1,
            "style": "gu_long",
            "planning_review_enabled": True,
            "style_snapshot": {"name": "Frozen style", "payload": {"syntax": ["FROZEN STYLE MARKER"]}},
            "creative_template_snapshot": {"payload": {"themes": ["FROZEN THEME"]}},
            "quality_policy_snapshot": {"payload": {"gate_threshold": 42}},
        },
    }
    request["imported_state"] = {
        "novel_id": command.project_id,
        "creative_brief": {},
        "creative_brief_version": 1,
        "content_type_snapshot": {},
        "style_snapshot": {},
        "creative_template_snapshot": {},
        "quality_policy_snapshot": {},
    }
    bundle = {"request": request, "snapshot": {"workflow": "novel.write@1"}}
    token = _EXECUTION.set(novel_store.execution)
    try:
        result = await novel_workflow.run_novel(command, bundle)
        assert isinstance(result, WaitingReview)
        assert result["review"]["type"] == "blueprint_review"
        assert result["state_hash"] == novel_workflow.digest(uploaded[-1]["state"])
        assert uploaded[-1]["state"]["project_id"] == command.project_id
        assert "novel_id" not in uploaded[-1]["state"]
        for key in ("style_snapshot", "creative_template_snapshot", "quality_policy_snapshot"):
            assert uploaded[-1]["state"][key] == request["input"][key]
        assert uploaded[-1]["state"]["creative_brief"]["themes"] == ["FROZEN THEME"]
        novel_store.execution.job.resume_input = {
            "version_number": uploaded[-1]["planning_versions"][0]["version_number"],
            "review_type": "blueprint_review",
        }
        result = await novel_workflow.run_novel(command, bundle)
        assert result["review"]["type"] == "scene_review"
        scene_version = next(item for item in uploaded[-1]["planning_versions"] if item["artifact_type"] == "scene")
        novel_store.execution.job.resume_input = {
            "version_number": scene_version["version_number"],
            "review_type": "scene_review",
        }
        result = await novel_workflow.run_novel(command, bundle)
        assert result["review"]["type"] == "human_review"
        assert uploaded[-1]["state"]["quality_gate_threshold"] == 42
        assert uploaded[-1]["state"]["creative_brief"]["themes"] == ["FROZEN THEME"]
        assert uploaded[-1]["content_versions"]
        original_version = uploaded[-1]["content_versions"][0]
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        for module, response in [
            ("agent_runtime.workflows.novel.agents.scene_writer", "A complete alternate chapter."),
            ("agent_runtime.workflows.novel.agents.style_editor", "A complete polished alternate chapter."),
        ]:
            model = FakeListChatModel(responses=[response])
            monkeypatch.setattr(f"{module}.get_llm", lambda model=model, **kwargs: model)
        monkeypatch.setattr(
            "agent_runtime.workflows.novel.agents.consistency_checker.get_analyzer_llm",
            lambda: FakeListChatModel(responses=["[]"]),
        )
        novel_store.execution.job.resume_input = {}
        bundle["request"] = {**request, "operation": "candidates", "arguments": {"count": 1}}
        assert (await novel_workflow.run_novel(command, bundle))["candidate_count"] == 1
        candidate = uploaded[-1]["candidates"][0]
        bundle["request"] = request
        novel_store.execution.job.resume_input = {"action": "restore_candidate", "candidate_id": candidate["id"]}
        result = await novel_workflow.run_novel(command, bundle)
        assert isinstance(result, WaitingReview)
        assert uploaded[-1]["state"]["current_draft"]["candidate_id"] == candidate["id"]
        novel_store.execution.job.resume_input = {"version_number": original_version["version_number"]}
        result = await novel_workflow.run_novel(command, bundle)
        assert isinstance(result, WaitingReview)
        assert uploaded[-1]["state"]["current_draft"]["content"] == original_version["content"]
        novel_store.execution.job.resume_input = {"action": "revise", "feedback": "Improve scene", "scene_number": 1}
        result = await novel_workflow.run_novel(command, bundle)
        assert isinstance(result, WaitingReview)
        assert uploaded[-1]["state"]["revision_count"] > 0
        novel_store.execution.job.resume_input = {}
        bundle["request"] = {
            **request,
            "operation": "canon",
            "arguments": {
                "action": "upsert_fact",
                "target_type": "fact",
                "subject": "Guard",
                "kind": "organization",
                "value": "night",
                "reason": "clarify setting",
            },
        }
        await novel_workflow.run_novel(command, bundle)
        assert any(item["subject"] == "Guard" for item in uploaded[-1]["state"]["canon"]["facts"])
        bundle["request"] = request
        novel_store.execution.job.resume_input = {"action": "approve"}
        result = await novel_workflow.run_novel(command, bundle)
        assert not isinstance(result, WaitingReview)
        assert result["content_commit"]["document_type"] == "novel"
        assert uploaded[-2]["state"]["audit_completed"]
        assert uploaded[-2]["audits"]
        assert uploaded[-2]["memory_snapshots"]
        novel_store.execution.job.resume_input = {}
        bundle["request"] = {**request, "operation": "evaluation", "arguments": {"chapter_number": 1}}
        assert "evaluation" in await novel_workflow.run_novel(command, bundle)
        bundle["request"] = {**request, "operation": "memory_rebuild"}
        rebuilt = (await novel_workflow.run_novel(command, bundle))["memory_report"]
        assert rebuilt["record_count"] > 0
        assert rebuilt["quality"]["case_count"] > 0
        from agent_runtime.infrastructure.postgres.checkpoints import NovelPostgresSaver
        from agent_runtime.workflows.novel.graph.builder import build_graph

        checkpoint = await build_graph(NovelPostgresSaver(novel_store)).aget_state(
            {"configurable": {"thread_id": command.project_id}}
        )
        from agent_runtime.workflows.novel.publication import novel_envelope

        assert checkpoint.values["memory_index"] == novel_envelope(uploaded[-1]["state"]["memory_index"])
        bundle["request"] = {**request, "operation": "memory_rebuild", "arguments": {"evaluate": False}}
        assert (await novel_workflow.run_novel(command, bundle))["memory_report"]["quality"] is None
        bundle["request"] = {**request, "operation": "memory_evaluate"}
        assert (await novel_workflow.run_novel(command, bundle))["memory_report"]["case_count"] > 0
        bundle["request"] = {
            **request,
            "operation": "book_revision",
            "arguments": {"chapter_number": 1, "feedback": "Improve ending"},
        }
        result = await novel_workflow.run_novel(command, bundle)
        assert isinstance(result, WaitingReview)
        assert uploaded[-1]["state"]["revision_mode"]
        original_content = uploaded[-1]["state"]["current_draft"]["content"]
        request.update(brief={"themes": ["UPDATED THEME"]}, brief_version=2)
        bundle["request"] = request
        novel_store.execution.job.resume_input = {"action": "approve"}
        # Resume the saved review boundary with a newer Control brief. Approval
        # must recheck the unchanged prose and require a second confirmation.
        result = await novel_workflow.run_novel(command, bundle)
        assert isinstance(result, WaitingReview)
        revised = uploaded[-1]["state"]
        assert revised["creative_brief"]["themes"] == ["UPDATED THEME"]
        assert revised["creative_brief_version"] == 2
        assert revised["current_draft"]["content"] == original_content
        assert revised["creative_brief_review_required"] is False
        novel_store.execution.job.resume_input = {"action": "approve"}
        await novel_workflow.run_novel(command, bundle)
        assert len(uploaded[-2]["state"]["blocks"]) == 1
        assert uploaded[-2]["state"]["audit_completed"]
    finally:
        _EXECUTION.reset(token)


def test_scoped_frozen_style_changes_actual_prompt_without_mutating_builtin():
    from agent_runtime.workflows.novel.context import novel_context
    from agent_runtime.workflows.novel.resources.styles import get_style_prompt

    original = get_style_prompt("gu_long")
    snapshot = {"name": "Custom", "payload": {"syntax": ["FROZEN STYLE MARKER"]}}
    with novel_context(object(), object(), object(), style_snapshot=snapshot):
        assert "FROZEN STYLE MARKER" in get_style_prompt("gu_long")
    assert get_style_prompt("gu_long") == original


async def test_imported_review_can_resume_directly_without_runtime_checkpoint(novel_store, monkeypatch):
    from agent_runtime.execution.context import _EXECUTION
    from agent_runtime.infrastructure.storage.artifacts import ArtifactReference
    from agent_runtime.workflows.novel import workflow as novel_workflow
    from agent_runtime.workflows.novel.graph.state import create_initial_state
    from agent_runtime.workflows.novel.publication import project_envelope

    class Artifacts:
        def __init__(self, settings):
            pass

        def upload(self, *args, **kwargs):
            return ArtifactReference("test", "object", "a" * 64, 1, "application/json", "")

    class Models:
        def __init__(self, settings):
            pass

        async def embed(self, texts):
            return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr(novel_workflow, "S3ArtifactStore", Artifacts)
    monkeypatch.setattr(novel_workflow, "RuntimeModels", Models)
    command = novel_store.execution.job.command
    imported = create_initial_state(
        novel_id="old-id", title="Imported", genre="fiction", inspiration="story", style="gu_long", total_chapters=1
    )
    imported.update(
        current_phase="human_review",
        world_bible="world: city",
        characters=[{"name": "Hero"}],
        outline=[{"chapter": 1, "title": "Chapter"}],
        current_draft={
            "chapter_number": 1,
            "title": "Chapter",
            "content": "Approved manuscript",
            "scene_plan": [],
            "scene_drafts": [],
        },
    )
    bundle = {
        "request": {
            "project_id": command.project_id,
            "document_type": "novel",
            "document_id": "book",
            "title": "Imported",
            "objective": "story",
            "operation": "resume",
            "arguments": {"feedback": "approve", "review_type": "human_review"},
            "input": {"total_chapters": 1},
            "imported_state": project_envelope(imported),
        }
    }
    token = _EXECUTION.set(novel_store.execution)
    try:
        result = await novel_workflow.run_novel(command, bundle)
        assert result["content_commit"]["document_type"] == "novel"
        assert novel_store.state()["chapters"][0]["content"] == "Approved manuscript"
        assert novel_store.state()["novel_id"] == command.project_id
    finally:
        _EXECUTION.reset(token)


async def test_evaluation_rejects_missing_explicit_version_without_scoring_current_draft(monkeypatch):
    from agent_runtime.workflows.novel import workflow as novel_workflow

    class Store:
        project_id = "project"

        def get_chapter_version(self, *args):
            return None

        def save_chapter_evaluation(self, *args, **kwargs):
            return kwargs

    class Checker:
        async def check(self, **kwargs):
            return []

    monkeypatch.setattr(novel_workflow, "ConsistencyCheckerAgent", Checker)
    with pytest.raises(ValueError, match="version.*does not exist"):
        await novel_workflow._evaluate(
            Store(),
            {"current_draft": {"chapter_number": 1, "content": "current"}},
            {"chapter_number": 1, "version_number": 999},
        )
