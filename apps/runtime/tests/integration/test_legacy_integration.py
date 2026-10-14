"""Opt-in user-authenticated Go -> MQ -> legacy novel Runtime migration acceptance."""

import os
import time
from uuid import uuid4

import httpx
import pytest

from agent_runtime.domain.protocol import RuntimeCommand
from agent_runtime.execution.handlers import generate_content
from agent_runtime.infrastructure.mq.rocketmq import RocketMQRuntimeClient
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.settings import RuntimeSettings
from tests.integration.helpers import inspect_snapshot, run_worker
from tests.novel.test_graph_flow import _patch_llms


def until(predicate):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("legacy migration acceptance timed out")


def test_public_project_novel_workflow_pauses_restores_and_projects_full_manuscript(monkeypatch, fake_llm):
    control_url = os.getenv("LEGACY_TEST_CONTROL_URL")
    if not control_url:
        pytest.skip("LEGACY_TEST_CONTROL_URL is not configured")
    settings = RuntimeSettings(_env_file=None)
    _patch_llms(monkeypatch, fake_llm)
    monkeypatch.setattr("agent_runtime.workflows.novel.agents.quality_evaluator.get_analyzer_llm", lambda: fake_llm)

    class Models:
        def __init__(self, settings):
            pass

        def chat(self, purpose):
            return fake_llm

        async def embed(self, texts):
            return [[0.1] * 1536 for text in texts]

    monkeypatch.setattr("agent_runtime.workflows.novel.workflow.RuntimeModels", Models)
    api = httpx.Client(base_url=control_url, timeout=30)
    runtime = RuntimeMQStore(settings.database_url)
    clients = {role: RocketMQRuntimeClient(settings, role) for role in ["dispatch", "relay"]}
    started = []
    try:
        artifacts = S3ArtifactStore(settings)
        if not artifacts.ready():
            artifacts.client.create_bucket(Bucket=artifacts.bucket)
        for client in clients.values():
            client.start()
            started.append(client)
        assert api.get("/api/v2/projects").status_code == 401
        identity = "migration_" + uuid4().hex[:12]
        registration = api.post(
            "/api/auth/register",
            json={"username": identity, "password": "migration-password", "workspace_name": "Migration acceptance"},
        )
        assert registration.status_code == 201, registration.text
        value = registration.json()
        api.headers["X-CSRF-Token"] = value["csrf_token"]
        me = api.get("/api/auth/me").json()
        workspace = me.get("workspace_id") or me.get("user", {}).get("workspace_id")
        assert workspace
        profile = api.post(
            "/api/model-settings/profiles",
            json={
                "name": "Frozen test model",
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:19999/v1",
                "api_key": "fake-model-secret",
                "chat_models": ["fake-chat"],
                "embedding_models": ["fake-embedding"],
            },
        )
        assert profile.status_code == 201, profile.text
        profile_id = profile.json()["id"]
        routes = {
            purpose: {
                "profile_id": profile_id,
                "model_name": "fake-embedding" if purpose == "embedding" else "fake-chat",
            }
            for purpose in ["creative", "analysis", "embedding"]
        }
        route_response = api.put("/api/model-settings/routes", json=routes)
        assert route_response.status_code == 200, route_response.text
        created = api.post(
            "/api/v2/projects",
            json={
                "title": "Migration novel",
                "content_type": "novel",
                "input": {
                    "genre": "fiction",
                    "inspiration": "A person arrives in a city",
                    "style": "gu_long",
                    "total_chapters": 1,
                },
            },
        )
        assert created.status_code == 201, created.text
        novel_id = created.json()["id"]
        assert api.get("/api/v2/projects/" + novel_id).status_code == 200
        assert api.get("/api/v2/projects/" + novel_id + "/projection").status_code == 200

        def execute(operation, args):
            submitted = api.post(
                "/api/v2/jobs",
                json={
                    "project_id": novel_id,
                    "workflow": "novel.write",
                    "workflow_version": 1,
                    "operation": operation,
                    "arguments": args,
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert submitted.status_code == 202, submitted.text
            job_id = submitted.json()["job_id"]

            def accepted():
                clients["dispatch"].receive(lambda body: runtime.accept_task(RuntimeCommand.model_validate_json(body)))
                try:
                    return runtime.get_scoped(job_id, workspace)
                except KeyError:
                    return None

            until(accepted)
            job = runtime.claim_next(
                workspace, "runtime", settings.worker_id, settings.job_lease_seconds, job_id=job_id
            )
            assert job.command.job_id == job_id
            inspect_snapshot(job, settings)
            run_worker(runtime, settings, generate_content, job, control_url)
            outcome = runtime.get_scoped(job_id, workspace)
            assert outcome.status in {"waiting_review", "completed"}, outcome.error
            runtime.relay(workspace, settings.rocketmq_event_topic, clients["relay"].publish, 5)

            def projected():
                response = api.get("/api/v2/jobs/" + job_id)
                if response.status_code != 200:
                    return None
                data = response.json()
                return data if data.get("status") == outcome.status else None

            until(projected)
            return outcome

        initial = execute("run", {})
        assert initial.status == "waiting_review"
        state = until(lambda: api.get(f"/api/v2/projects/{novel_id}/projection").json().get("current_draft"))
        assert state["content"]
        resumed = execute("resume", {"action": "approve", "feedback": "approve"})
        assert resumed.status == "completed"
        final = until(lambda: api.get(f"/api/v2/projects/{novel_id}/projection").json().get("audit_completed"))
        assert final is True
        projection = api.get(f"/api/v2/projects/{novel_id}/projection").json()
        assert len(projection["blocks"]) == 1
        assert api.get(f"/api/v2/projects/{novel_id}/blocks/1/versions").status_code == 200
        assert api.get(f"/api/v2/projects/{novel_id}/audits").status_code == 200
        export = api.get(f"/api/v2/projects/{novel_id}/export", params={"format": "docx"})
        assert export.status_code == 200 and export.content.startswith(b"PK")
        assert api.get("/api/monitoring/summary").status_code == 200
        benchmark = api.post("/api/evaluations/benchmarks", json={"project_id": novel_id, "include_judge": False})
        assert benchmark.status_code == 202, benchmark.text
        assert benchmark.json()["operation_id"]
        assert api.post("/api/auth/logout", json={}).status_code == 200
        assert api.get("/api/v2/projects").status_code == 401
    finally:
        for client in reversed(started):
            client.close()
        runtime.close()
        api.close()
