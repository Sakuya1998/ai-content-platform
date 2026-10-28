"""Run the forward migration against populated, isolated legacy databases."""

import os
from types import SimpleNamespace
from typing import TypedDict
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from tools.migrate.platform_migrate import apply_database, migrations

from agent_runtime.execution.context import ExecutionContext
from agent_runtime.infrastructure.postgres.jobs import _json
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from tests.infrastructure.test_mq_store import command


@pytest.fixture
def legacy_runtime_database():
    admin_url = os.getenv("MQ_TEST_ADMIN_DATABASE_URL")
    runtime_url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not admin_url or not runtime_url:
        pytest.skip("disposable PostgreSQL admin and Runtime URLs required")
    runtime_options = conninfo_to_dict(runtime_url)
    name = "test_workflow_migration_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(runtime_options["user"]))
        )
    try:
        runtime_options["dbname"] = name
        url = make_conninfo(**runtime_options)
        admin_options = conninfo_to_dict(admin_url)
        admin_options["dbname"] = name
        with psycopg.connect(make_conninfo(**admin_options)) as admin:
            admin.execute("CREATE EXTENSION vector; CREATE EXTENSION pgcrypto")
        selected = tuple(item for item in migrations() if item.component == "runtime" and item.name < "013")
        apply_database(url, "runtime", selected)
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_populated_backfill_preserves_checkpoints_records_vectors_and_tenant_scope(legacy_runtime_database):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph
    from langgraph.types import Command, interrupt

    from agent_runtime.infrastructure.postgres.checkpoints import NovelPostgresSaver
    from agent_runtime.infrastructure.postgres.knowledge import KnowledgeOperations
    from agent_runtime.infrastructure.postgres.novel import PostgresNovelStore
    from agent_runtime.infrastructure.postgres.novel_memory import PostgresNovelMemory

    class State(TypedDict):
        text: str

    def graph(saver):
        builder = StateGraph(State)
        builder.add_node("review", lambda state: {"text": state["text"] + interrupt("approval")})
        builder.set_entry_point("review")
        builder.set_finish_point("review")
        return builder.compile(checkpointer=saver)

    runtime = RuntimeMQStore(legacy_runtime_database)
    try:
        task = command()
        runtime.accept_task(task)
        job = runtime.claim_next(task.workspace_id, "runtime", "migration-worker", 60)
        config = {"configurable": {"thread_id": task.project_id}}
        source = InMemorySaver()
        graph(source).invoke({"text": "preserved"}, config)
        saved = source.get_tuple(config)
        serde = source.serde
        checkpoint_kind, checkpoint = serde.dumps_typed(saved.checkpoint)
        metadata_kind, metadata = serde.dumps_typed(saved.metadata)
        checkpoint_id = saved.checkpoint["id"]
        parent = saved.parent_config["configurable"]["checkpoint_id"] if saved.parent_config else None
        with runtime.scope(task.workspace_id) as cursor:
            cursor.execute(
                "INSERT INTO novel_projects VALUES(%s,%s,%s,%s,1,now())",
                (
                    task.workspace_id,
                    task.project_id,
                    _json({"novel_id": task.project_id, "title": "Preserved"}),
                    task.job_id,
                ),
            )
            cursor.execute(
                "INSERT INTO novel_records VALUES(%s,%s,'evaluations','report',1,1,%s,%s,%s,1,now())",
                (task.workspace_id, task.project_id, "a" * 64, _json({"id": "report", "score": 87}), task.job_id),
            )
            cursor.execute(
                "INSERT INTO novel_checkpoints VALUES(%s,%s,%s,'',%s,%s,%s,%s,%s,%s,%s,1)",
                (
                    task.workspace_id,
                    task.project_id,
                    task.project_id,
                    checkpoint_id,
                    parent,
                    checkpoint_kind,
                    checkpoint,
                    metadata_kind,
                    metadata,
                    task.job_id,
                ),
            )
            for index, (pending_task, channel, value) in enumerate(saved.pending_writes):
                value_kind, value_bytes = serde.dumps_typed(value)
                cursor.execute(
                    "INSERT INTO novel_checkpoint_writes VALUES(%s,%s,%s,'',%s,%s,%s,%s,%s,%s,'',%s,1)",
                    (
                        task.workspace_id,
                        task.project_id,
                        task.project_id,
                        checkpoint_id,
                        pending_task,
                        index,
                        channel,
                        value_kind,
                        value_bytes,
                        task.job_id,
                    ),
                )
        for scope in (task.workspace_id, str(uuid4())):
            source_job = task.job_id
            if scope != task.workspace_id:
                other = command(workspace_id=scope)
                runtime.accept_task(other)
                source_job = other.job_id
            with runtime.scope(scope) as cursor:
                cursor.execute(
                    "INSERT INTO novel_memories VALUES(%s,%s,'fact','preserved fact',%s,%s::vector,%s,'v1',1,now())",
                    (
                        scope,
                        task.project_id,
                        _json({"type": "canon", "embedding_model": "fake", "index_version": "novel-memory@1"}),
                        str([0.1] * 1536),
                        source_job,
                    ),
                )
        migration = tuple(item for item in migrations() if item.component == "runtime" and item.name >= "013")
        assert apply_database(legacy_runtime_database, "runtime", migration) == [
            f"applied runtime/{item.name}" for item in migration
        ]
        assert apply_database(legacy_runtime_database, "runtime", migration) == [
            f"skipped runtime/{item.name}" for item in migration
        ]
        store = PostgresNovelStore(ExecutionContext(job, runtime, SimpleNamespace(worker_id="migration-worker")))
        saver = NovelPostgresSaver(store)
        assert store.state()["title"] == "Preserved"
        assert store.records("evaluations")[0]["score"] == 87
        assert saver.get_tuple(config).pending_writes
        assert graph(saver).invoke(Command(resume=" approved"), config)["text"] == "preserved approved"
        with runtime.scope(task.workspace_id) as cursor:
            cursor.execute("SELECT source_count,target_count,source_hash=target_hash FROM runtime_state_backfills")
            proofs = cursor.fetchall()
            assert len(proofs) == 5 and all(source == target and equal for source, target, equal in proofs)
            cursor.execute("SELECT count(*) FROM legacy_runtime_resource_archive WHERE source_table='novel_memories'")
            assert cursor.fetchone()[0] == 1
            cursor.execute("SELECT to_regclass('novel_memories'),to_regclass('novel_checkpoints')")
            assert cursor.fetchone() == (None, None)
            cursor.execute(
                "SELECT source_count=archive_count AND source_hash=archive_hash FROM runtime_resource_archive_checks"
            )
            assert all(row[0] for row in cursor.fetchall())
            cursor.execute("SELECT count(*) FROM memory_nodes")
            assert cursor.fetchone()[0] == 1
        assert (
            KnowledgeOperations(runtime).list_memory(task.workspace_id, task.project_id)[0]["content"]
            == "preserved fact"
        )

        class Models:
            async def embed(self, texts):
                return [[0.1] * 1536 for _ in texts]

            def embedding_identity(self):
                return {"embedding_model": "fake", "index_version": "novel-memory@1"}

        memory = PostgresNovelMemory(store, Models())
        assert memory.list_records()[0]["id"] == "fact"
        assert memory.search_similar("fact")[0]["content"] == "preserved fact"
    finally:
        runtime.close()


def test_retirement_rejects_source_changes_after_verified_archive(legacy_runtime_database):
    preparation = tuple(item for item in migrations() if item.component == "runtime" and "013" <= item.name < "016")
    retirement = tuple(item for item in migrations() if item.component == "runtime" and item.name.startswith("016"))
    apply_database(legacy_runtime_database, "runtime", preparation)
    runtime = RuntimeMQStore(legacy_runtime_database)
    try:
        task = command()
        runtime.accept_task(task)
        with runtime.scope(task.workspace_id) as cursor:
            cursor.execute(
                "INSERT INTO novel_projects VALUES(%s,%s,%s,%s,1,now())",
                (task.workspace_id, task.project_id, _json({"late": "source write"}), task.job_id),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="source changed after archive"):
            apply_database(legacy_runtime_database, "runtime", retirement)
        with runtime.scope(task.workspace_id) as cursor:
            cursor.execute("SELECT state FROM novel_projects")
            assert cursor.fetchone()[0] == {"late": "source write"}
            cursor.execute("SELECT relforcerowsecurity FROM pg_class WHERE oid='novel_projects'::regclass")
            assert cursor.fetchone()[0] is True
    finally:
        runtime.close()
