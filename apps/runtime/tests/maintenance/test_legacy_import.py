import json
import os
import sqlite3
from uuid import uuid4

import psycopg
import pytest

from scripts.platform_legacy_export import export_legacy, stable_id
from scripts.platform_legacy_import import import_bundle


def test_legacy_import_is_idempotent_and_preserves_manuscript_and_password(tmp_path):
    control, runtime = os.getenv("MQ_TEST_CONTROL_DATABASE_URL"), os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not control or not runtime:
        pytest.skip("two test database URLs required")
    identity = uuid4().hex
    tenant, user, novel = "tenant-" + identity, "user-" + identity, "novel-" + identity
    source, destination = tmp_path / "legacy.db", tmp_path / "export.json"
    with sqlite3.connect(source) as connection:
        connection.executescript("""CREATE TABLE tenants(id text,name text); CREATE TABLE users(id text,tenant_id text,
            username text,email text,display_name text,password_hash text,role text);
            CREATE TABLE novels(id text,tenant_id text,title text,genre text,
                inspiration text,style text,total_chapters int);
            CREATE TABLE chapters(novel_id text,chapter_number int,title text,content text,summary text,status text);
            CREATE TABLE progress(novel_id text,state_json text,current_chapter int,current_phase text);
            CREATE TABLE chapter_candidates(id text,novel_id text,chapter_number int,
                content text,version_number int);
            CREATE TABLE run_jobs(id text,novel_id text,action text,status text,request_json text);
            CREATE TABLE run_job_events(id int,job_id text,sequence int,event_type text,payload_json text);
            CREATE TABLE model_call_metrics(id int,novel_id text,provider text,model_name text,
                input_tokens int,output_tokens int,success int);""")
        connection.execute("INSERT INTO tenants VALUES(?,?)", (tenant, "Legacy Workspace"))
        connection.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?,?)",
            (user, tenant, identity, identity + "@example.test", "Owner", "scrypt$preserved", "owner"),
        )
        connection.execute(
            "INSERT INTO chapter_candidates VALUES(?,?,?,?,?)", ("candidate-legacy", novel, 1, "Candidate", 1)
        )
        connection.execute(
            "INSERT INTO run_jobs VALUES(?,?,?,?,?)", ("job-legacy-" + identity, novel, "run", "completed", "{}")
        )
        connection.execute(
            "INSERT INTO run_job_events VALUES(?,?,?,?,?)", (1, "job-legacy-" + identity, 1, "job.completed", "{}")
        )
        connection.execute(
            "INSERT INTO model_call_metrics VALUES(?,?,?,?,?,?,?)", (1, novel, "openai", "legacy-model", 10, 20, 1)
        )
        connection.execute(
            "INSERT INTO novels VALUES(?,?,?,?,?,?,?)",
            (novel, tenant, "Legacy Book", "Mystery", "Find the truth", "jin_yong", 1),
        )
        connection.execute(
            "INSERT INTO chapters VALUES(?,?,?,?,?,?)",
            (novel, 1, "Opening", "Preserved manuscript", "Summary", "final"),
        )
        connection.execute(
            "INSERT INTO progress VALUES(?,?,?,?)",
            (
                novel,
                json.dumps({"chapters": [{"chapter_number": 1, "content": "Preserved manuscript"}]}),
                2,
                "completed",
            ),
        )
    export_legacy(source, destination)
    bundle = json.loads(destination.read_text())
    result = import_bundle(bundle, control, runtime)
    repeated = import_bundle(bundle, control, runtime)
    assert result["projects"] == repeated["projects"] == 1
    assert result["archived_rows"] == result["source_rows"]
    changed = json.loads(json.dumps(bundle))
    changed["tables"]["novels"][0]["title"] = "Changed book"
    with pytest.raises(ValueError, match="different"):
        import_bundle(changed, control, runtime)
    project, workspace = stable_id("project", novel), stable_id("workspace", tenant)
    with psycopg.connect(control) as connection:
        connection.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
        assert connection.execute("SELECT count(*) FROM project_projections WHERE id=%s", (project,)).fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT record_key FROM project_records WHERE project_id=%s AND kind='candidate'", (project,)
            ).fetchone()[0]
            == "candidate-legacy"
        )
        request = connection.execute(
            "SELECT request FROM jobs WHERE id=%s", (stable_id("job", "job-legacy-" + identity),)
        ).fetchone()[0]
        assert request["project_id"] == project and request["payload"]["legacy_action"] == "run"
        assert (
            connection.execute("SELECT password_hash FROM users WHERE id=%s", (stable_id("user", user),)).fetchone()[0]
            == "scrypt$preserved"
        )
        assert (
            connection.execute(
                "SELECT state->'chapters'->0->>'content' FROM project_projections WHERE id=%s", (project,)
            ).fetchone()[0]
            == "Preserved manuscript"
        )
    with psycopg.connect(runtime) as connection:
        connection.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
        assert (
            connection.execute(
                "SELECT state->'chapters'->0->>'content' FROM workflow_states WHERE project_id=%s "
                "AND workflow_id='novel.write@1'",
                (project,),
            ).fetchone()[0]
            == "Preserved manuscript"
        )
        assert connection.execute(
            """SELECT m.input_tokens,m.output_tokens FROM model_calls m JOIN runtime_jobs j ON j.id=m.job_id
               WHERE j.project_id=%s""",
            (project,),
        ).fetchone() == (10, 20)


def test_encrypted_model_import_can_be_activated_on_key_enabled_retry():
    import base64

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    control, runtime = os.getenv("MQ_TEST_CONTROL_DATABASE_URL"), os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not control or not runtime:
        pytest.skip("two test database URLs required")
    identity = uuid4().hex
    tenant = "tenant_" + identity
    user = "user_" + identity
    profile = "profile_" + identity
    legacy_key = Fernet.generate_key()
    new_key = "11" * 32
    ciphertext = Fernet(legacy_key).encrypt(b"model-import-secret")
    bundle = {
        "schema_version": "agent-platform-legacy-export@1",
        "tables": {
            "tenants": [{"id": tenant, "name": "Model import"}],
            "users": [
                {
                    "id": user,
                    "tenant_id": tenant,
                    "username": identity,
                    "email": identity + "@test.invalid",
                    "role": "owner",
                    "password_hash": "scrypt$legacy",
                }
            ],
            "model_profiles": [
                {
                    "id": profile,
                    "tenant_id": tenant,
                    "name": "Encrypted profile",
                    "provider": "openai_compatible",
                    "base_url": "http://provider.invalid/v1",
                    "api_key_encrypted": {"$binary": base64.b64encode(ciphertext).decode()},
                    "chat_models_json": '["model"]',
                    "embedding_models_json": '["embedding"]',
                }
            ],
            "model_routes": [
                {"tenant_id": tenant, "purpose": "creative", "profile_id": profile, "model_name": "model"}
            ],
        },
        "warnings": [],
    }
    import_bundle(bundle, control, runtime)
    report = import_bundle(bundle, control, runtime, legacy_model_key=legacy_key, model_encryption_key=new_key)
    with psycopg.connect(control) as connection:
        connection.execute("SELECT set_config('app.workspace_id',%s,true)", (stable_id("workspace", tenant),))
        encrypted = bytes(
            connection.execute(
                "SELECT encrypted_key FROM governance_model_profile_versions WHERE profile_id=%s",
                (stable_id("model-profile", profile),),
            ).fetchone()[0]
        )
        assert (
            AESGCM(bytes.fromhex(new_key)).decrypt(encrypted[:12], encrypted[12:], b"novel-agent:model-key:v1")
            == b"model-import-secret"
        )
    assert not report["warnings"]


def test_legacy_sqlite_interrupt_and_pending_writes_resume_after_id_remap(tmp_path):
    from types import SimpleNamespace
    from typing import TypedDict

    SqliteSaver = pytest.importorskip("langgraph.checkpoint.sqlite").SqliteSaver
    from langgraph.graph import StateGraph
    from langgraph.types import Command, interrupt

    from agent_runtime.execution.context import ExecutionContext
    from agent_runtime.infrastructure.postgres.checkpoints import NovelPostgresSaver
    from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
    from agent_runtime.infrastructure.postgres.novel import PostgresNovelStore
    from tests.infrastructure.test_mq_store import command

    control, runtime = os.getenv("MQ_TEST_CONTROL_DATABASE_URL"), os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not control or not runtime:
        pytest.skip("two test database URLs required")
    old = "novel_" + uuid4().hex

    class State(TypedDict):
        novel_id: str
        text: str

    def graph(saver):
        def review(state):
            approval = interrupt({"novel_id": state["novel_id"]})
            return {"text": state["text"] + approval}

        builder = StateGraph(State)
        builder.add_node("review", review)
        builder.set_entry_point("review")
        builder.set_finish_point("review")
        return builder.compile(checkpointer=saver)

    source, checkpoint, output = tmp_path / "legacy.db", tmp_path / "checkpoints.db", tmp_path / "bundle.json"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE novels(id text,tenant_id text,title text,genre text,style text,"
            "inspiration text,total_chapters int)"
        )
        connection.execute(
            "INSERT INTO novels VALUES(?,?,?,?,?,?,?)",
            (old, "tenant_" + old, "Imported", "fiction", "gu_long", "story", 1),
        )
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph(saver).invoke({"novel_id": old, "text": "manuscript"}, {"configurable": {"thread_id": old}})
    export_legacy(source, output, checkpoint=checkpoint)
    bundle = json.loads(output.read_text())
    import_bundle(bundle, control, runtime)
    workspace, project = stable_id("workspace", "tenant_" + old), stable_id("project", old)
    store = RuntimeMQStore(runtime)
    try:
        task = command(workspace_id=workspace).model_copy(update={"project_id": project})
        store.accept_task(task)
        job = store.claim_next(workspace, "runtime", "restore-worker", 60)
        scoped = PostgresNovelStore(ExecutionContext(job, store, SimpleNamespace(worker_id="restore-worker")))
        saver = NovelPostgresSaver(scoped)
        restored = saver.get_tuple({"configurable": {"thread_id": project}})
        assert restored.pending_writes
        result = graph(saver).invoke(Command(resume=" approved"), {"configurable": {"thread_id": project}})
        assert result["novel_id"] == project and result["text"] == "manuscript approved"
    finally:
        store.close()
