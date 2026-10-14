"""Opt-in full handler path with real SDKs and a deterministic HTTP model provider."""

import json
import os
import threading
import time
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest

from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent
from agent_runtime.execution.handlers import generate_content, refresh_memory
from agent_runtime.infrastructure.mq.rocketmq import RocketMQRuntimeClient
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from agent_runtime.settings import RuntimeSettings
from tests.integration.helpers import content_version, inspect_snapshot, run_worker, submit_generation


@contextmanager
def model_provider(*, approve=True):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/embeddings"):
                inputs = payload["input"]
                if isinstance(inputs, str):
                    inputs = [inputs]
                response = {
                    "object": "list",
                    "model": "test-embedding",
                    "usage": {"prompt_tokens": 10, "total_tokens": 10},
                    "data": [
                        {"index": index, "object": "embedding", "embedding": [0.1] * 1536}
                        for index, _ in enumerate(inputs)
                    ],
                }
            else:
                text = payload["messages"][-1]["content"]
                if "Summarize this committed" in text:
                    value = {
                        "summary": "A person arrives at the station.",
                        "facts": [{"kind": "event", "subject": "Person", "value": "arrived at station"}],
                    }
                elif text.startswith("Plan the requested"):
                    value = {"plan": "A single scene at the station."}
                elif text.startswith("Review fidelity"):
                    value = {"approved": approve, "notes": [] if approve else ["Human review required"]}
                else:
                    value = {"blocks": [{"type": "scene", "text": "INT. STATION - DAY. A person arrives."}]}
                response = {
                    "id": "chat-test",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "test-chat",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(value)},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                }
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def wait_for(predicate):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("handler integration condition timed out")


def test_real_creation_handler_publishes_ir_and_memory_with_project_isolation():
    url = os.getenv("HANDLER_TEST_CONTROL_URL")
    if not url:
        pytest.skip("HANDLER_TEST_CONTROL_URL is not configured")
    with httpx.Client(base_url=url, timeout=30) as setup:
        response = setup.post(
            "/api/auth/register",
            json={
                "username": "handler_" + uuid4().hex[:12],
                "password": "handler-test-password",
                "workspace_name": "Handler acceptance",
            },
        )
        assert response.status_code == 201, response.text
        identity = response.json()
        workspace = identity["user"]["workspace_id"]
        setup.headers["X-CSRF-Token"] = identity["csrf_token"]
        response = setup.post("/api/v2/projects", json={"content_type": "screenplay", "title": "Handler acceptance"})
        assert response.status_code == 201, response.text
        project = response.json()["id"]

        def configure_models(endpoint):
            response = setup.post(
                "/api/model-settings/profiles",
                json={
                    "name": "Deterministic HTTP model",
                    "provider": "openai_compatible",
                    "base_url": endpoint,
                    "api_key": "test-model-key",
                    "chat_models": ["test-chat"],
                    "embedding_models": ["test-embedding"],
                },
            )
            assert response.status_code == 201, response.text
            profile = response.json()["id"]
            response = setup.put(
                "/api/model-settings/routes",
                json={
                    purpose: {
                        "profile_id": profile,
                        "model_name": "test-embedding" if purpose == "embedding" else "test-chat",
                    }
                    for purpose in ["creative", "analysis", "embedding"]
                },
            )
            assert response.status_code == 200, response.text

        # Retain the authenticated setup session during model profile publication.
        _run_handler_acceptance(url, workspace, project, configure_models, setup)


def _run_handler_acceptance(url, workspace, project, configure_models, api):
    with model_provider() as endpoint:
        configure_models(endpoint)
        settings = RuntimeSettings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_model="test-chat",
            llm_api_key="test-model-key",
            llm_base_url=endpoint,
            embedding_model="test-embedding",
            embedding_api_key="test-model-key",
            embedding_base_url=endpoint,
        )
        store = RuntimeMQStore(settings.database_url)
        clients = {
            role: RocketMQRuntimeClient(settings, role)
            for role in ["dispatch", "control", "relay", "memory", "memory-scheduler"]
        }
        started = []
        try:
            artifacts = __import__(
                "agent_runtime.infrastructure.storage.artifacts", fromlist=["S3ArtifactStore"]
            ).S3ArtifactStore(settings)
            if not artifacts.ready():
                artifacts.client.create_bucket(Bucket=artifacts.bucket)
            for client in clients.values():
                client.start()
                started.append(client)
            with nullcontext(api) as api:
                command = submit_generation(
                    api,
                    {
                        "project_id": project,
                        "title": "SDK check " + uuid4().hex,
                        "document_type": "screenplay",
                        "objective": "Create a scene at the station.",
                    },
                )

                def accepted():
                    clients["dispatch"].receive(
                        lambda body: store.accept_task(RuntimeCommand.model_validate_json(body))
                    )
                    try:
                        return store.get_scoped(command.job_id, workspace)
                    except KeyError:
                        return None

                wait_for(accepted)
                job = store.claim_next(
                    workspace, "runtime", settings.worker_id, settings.job_lease_seconds, job_id=command.job_id
                )
                assert job.command.job_id == command.job_id
                inspect_snapshot(job, settings)
                run_worker(store, settings, generate_content, job, url)
                assert store.get_scoped(command.job_id, workspace).status == "completed"
                store.relay(workspace, settings.rocketmq_event_topic, clients["relay"].publish, 5)

                def committed():
                    result = api.get("/api/v2/jobs/" + command.job_id).json()
                    return result if result["status"] == "completed" else None

                projection = wait_for(committed)
                version_id = projection["result"]["document_version_id"]

                def projected():
                    source = content_version(api, version_id, project)
                    return source.json() if source.status_code == 200 else None

                version = wait_for(projected)
                assert version["blocks"][0]["text"].startswith("INT. STATION")

                def scheduled():
                    clients["memory-scheduler"].receive(
                        lambda body: store.schedule_memory(
                            RuntimeEvent.model_validate_json(body), settings.rocketmq_memory_topic
                        )
                    )
                    store.relay(workspace, settings.rocketmq_event_topic, clients["relay"].publish, 5)
                    with store.scope(workspace) as cursor:
                        cursor.execute(
                            """SELECT 1 FROM runtime_command_outbox WHERE published_at IS NOT NULL
                                       AND payload->'payload'->>'document_version_id'=%s""",
                            (version_id,),
                        )
                        return cursor.fetchone()

                wait_for(scheduled)

                def memory_ready():
                    clients["memory"].receive(
                        lambda body: store.accept_task(RuntimeCommand.model_validate_json(body), "memory")
                    )
                    candidate = store.claim_next(workspace, "memory", settings.worker_id, settings.job_lease_seconds)
                    if candidate and candidate.command.payload["document_version_id"] != version_id:
                        run_worker(store, settings, refresh_memory, candidate, url)
                        return None
                    return candidate

                memory = wait_for(memory_ready)
                assert memory.command.payload["source_execution_snapshot"]
                assert memory.command.payload["artifact"]["sha256"]
                run_worker(store, settings, refresh_memory, memory, url)
                assert store.get_scoped(memory.command.job_id, workspace).status == "completed"
                with store.scope(workspace) as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM memory_nodes WHERE project_id=%s AND document_version_id=%s",
                        (project, version_id),
                    )
                    assert cursor.fetchone()[0] == 2
                    cursor.execute("SELECT count(*) FROM memory_embeddings WHERE project_id=%s", (project,))
                    assert cursor.fetchone()[0] >= 2
                    cursor.execute("SELECT count(*) FROM model_calls WHERE job_id=%s", (command.job_id,))
                    assert cursor.fetchone()[0] == 3
                    cursor.execute(
                        "SELECT count(DISTINCT prompt_version),count(*) FROM prompt_renderings WHERE job_id=%s",
                        (command.job_id,),
                    )
                    assert cursor.fetchone() == (3, 3)
                assert content_version(api, version_id, str(uuid4())).status_code == 404
                with model_provider(approve=False) as review_endpoint:
                    configure_models(review_endpoint)
                    review_settings = settings.model_copy(update={"llm_base_url": review_endpoint})
                    adaptation = submit_generation(
                        api,
                        {
                            "project_id": project,
                            "title": "Adaptation " + uuid4().hex,
                            "document_type": "copy",
                            "objective": "Adapt the screenplay to short copy.",
                            "source_version_id": version_id,
                            "required_memory_version_ids": [version_id],
                        },
                    )

                    def adaptation_accepted():
                        clients["dispatch"].receive(
                            lambda body: store.accept_task(RuntimeCommand.model_validate_json(body))
                        )
                        try:
                            return store.get_scoped(adaptation.job_id, workspace)
                        except KeyError:
                            return None

                    wait_for(adaptation_accepted)
                    next_job = store.claim_next(
                        workspace, "runtime", settings.worker_id, settings.job_lease_seconds, job_id=adaptation.job_id
                    )
                    inspect_snapshot(next_job, settings)
                    run_worker(store, review_settings, generate_content, next_job, url)
                    pending = store.get_scoped(adaptation.job_id, workspace)
                    assert pending.status == "waiting_review", pending.error
                    store.relay(workspace, settings.rocketmq_event_topic, clients["relay"].publish, 5)
                    wait_for(lambda: api.get("/api/v2/jobs/" + adaptation.job_id).json()["status"] == "waiting_review")
                    assert (
                        api.post(
                            "/api/v2/jobs/" + adaptation.job_id + "/resume",
                            json={"action": "approve"},
                            headers={"Idempotency-Key": str(uuid4())},
                        ).status_code
                        == 202
                    )

                    def resumed():
                        clients["control"].receive(
                            lambda body: store.accept_control(RuntimeCommand.model_validate_json(body))
                        )
                        return store.claim_next(
                            workspace,
                            "runtime",
                            settings.worker_id,
                            settings.job_lease_seconds,
                            job_id=adaptation.job_id,
                        )

                    claimed = wait_for(resumed)
                    run_worker(store, review_settings, generate_content, claimed, url)
                    finished = store.get_scoped(adaptation.job_id, workspace)
                    assert finished.status == "completed"
                    store.relay(workspace, settings.rocketmq_event_topic, clients["relay"].publish, 5)
                    adapted_version = finished.result["document_version_id"]

                    def adapted():
                        response = content_version(api, adapted_version, project)
                        return response.json() if response.status_code == 200 else None

                    assert wait_for(adapted)["document_type"] == "copy"
        finally:
            for client in reversed(started):
                client.close()
            store.close()
