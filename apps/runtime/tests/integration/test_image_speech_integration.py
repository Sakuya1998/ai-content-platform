"""Opt-in media providers mocked over HTTP with real MQ/RustFS and Go projection."""

import base64
import json
import os
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest

from agent_runtime.execution.handlers import generate_content
from agent_runtime.infrastructure.mq.rocketmq import RocketMQRuntimeClient
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.settings import RuntimeSettings
from tests.integration.helpers import content_version, receive_task, run_worker, submit_generation
from tests.integration.test_handler_integration import wait_for

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII=")
AUDIO = b"ID3\x04\x00\x00mock-audio"


@contextmanager
def asset_provider():
    calls = {"image": 0, "edit": 0, "seedream": 0, "seedream_edit": 0, "speech": 0, "doubao": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data, content_type="application/json"):
            data = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Request-ID", "mock-image-request")
            self.send_header("X-Tt-Logid", "mock-speech-request")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.endswith("/images/edits"):
                assert PNG in body and b"multipart/form-data" in self.headers["Content-Type"].encode()
                calls["edit"] += 1
                self.reply({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
                return
            payload = json.loads(body)
            if self.path.endswith("/images/generations"):
                key = "seedream" if self.path.startswith("/api/v3/") else "image"
                if payload.get("image"):
                    assert base64.b64decode(payload["image"].split(",", 1)[1]) == PNG
                    calls["seedream_edit"] += 1
                else:
                    calls[key] += 1
                self.reply(
                    {
                        "data": [{"b64_json": base64.b64encode(PNG).decode()}],
                        "usage": {"input_tokens": 3, "output_tokens": 4},
                    }
                )
            elif self.path.endswith("/audio/speech"):
                assert payload["input"]
                calls["speech"] += 1
                self.reply(AUDIO, "audio/mpeg")
            elif self.path.endswith("/tts/unidirectional"):
                assert self.headers["X-Api-Key"] == "test-tts-key"
                assert self.headers["X-Api-Resource-Id"] == "speech-resource"
                assert payload["req_params"]["text"]
                calls["doubao"] += 1
                events = [
                    {"code": 0, "data": base64.b64encode(AUDIO).decode()},
                    {"code": 20000000, "data": None, "usage": {"text_words": 9}},
                ]
                self.reply(("\n".join(json.dumps(e) for e in events) + "\n").encode(), "application/json")
            else:
                prompt = payload["messages"][-1]["content"]
                if "唯一字段 prompt" in prompt:
                    value = {"prompt": "A station in warm light"}
                elif "唯一字段 text" in prompt:
                    value = {"text": "欢迎来到车站。"}
                else:
                    value = {"approved": True, "notes": []}
                self.reply(
                    {
                        "id": "mock-chat",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "test-chat",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)},
                            }
                        ],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                    }
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_image_edit_and_both_speech_protocols_publish_assets_and_ir():
    url = os.getenv("ASSET_TEST_CONTROL_URL")
    if not url:
        pytest.skip("ASSET_TEST_CONTROL_URL is not configured")
    settings = RuntimeSettings(_env_file=None)
    with asset_provider() as (endpoint, calls), httpx.Client(base_url=url, timeout=30) as api:
        response = api.post(
            "/api/auth/register",
            json={
                "username": "assets_" + uuid4().hex[:12],
                "password": "asset-test-password",
                "workspace_name": "Image/speech acceptance",
            },
        )
        assert response.status_code == 201, response.status_code
        identity = response.json()
        workspace = identity["user"]["workspace_id"]
        api.headers["X-CSRF-Token"] = identity["csrf_token"]
        response = api.post("/api/v2/projects", json={"content_type": "image", "title": "Assets acceptance"})
        assert response.status_code == 201, response.status_code
        project = response.json()["id"]

        def profile(provider, base, **extra):
            response = api.post(
                "/api/model-settings/profiles",
                json={
                    "name": provider,
                    "provider": provider,
                    "base_url": endpoint + base,
                    "api_key": "test-tts-key",
                    **extra,
                },
            )
            assert response.status_code == 201, response.status_code
            return response.json()["id"]

        text_profile = profile("openai_compatible", "/v1", chat_models=["test-chat"], embedding_models=["embed-test"])
        image_models = [
            {
                "model_name": "image-test",
                "capabilities": {
                    "generation": True,
                    "editing": True,
                    "sizes": ["1024x1024"],
                    "formats": ["png"],
                    "max_images": 1,
                },
            }
        ]
        speech_models = [
            {
                "model_name": "speech-resource",
                "capabilities": {
                    "voices": ["test-voice"],
                    "formats": ["mp3"],
                    "languages": ["zh"],
                    "min_speed": 0.5,
                    "max_speed": 2.0,
                    "max_input_chars": 4096,
                    "instructions": False,
                },
            }
        ]
        images = {
            p: profile(p, "/api/v3" if p == "volcengine" else "/v1", image_models=image_models)
            for p in ["openai_compatible", "volcengine"]
        }
        speech = {
            p: profile(p, "" if p == "volcengine_tts" else "/v1", speech_models=speech_models)
            for p in ["openai_compatible", "volcengine_tts"]
        }
        headers = {"X-Workspace-ID": workspace}
        jobs = RuntimeMQStore(settings.database_url)
        dispatch, relay = [RocketMQRuntimeClient(settings, role) for role in ["dispatch", "relay"]]
        artifacts = S3ArtifactStore(settings)
        try:
            dispatch.start()
            relay.start()

            def generate(kind, provider_name, parameters, source_version=None):
                routes = {
                    purpose: {
                        "profile_id": text_profile,
                        "model_name": "embed-test" if purpose == "embedding" else "test-chat",
                    }
                    for purpose in ["creative", "analysis", "embedding"]
                }
                purpose = "image_generation" if kind == "image" else "speech_synthesis"
                routes[purpose] = {
                    "profile_id": (images if kind == "image" else speech)[provider_name],
                    "model_name": "image-test" if kind == "image" else "speech-resource",
                }
                response = api.put("/api/model-settings/routes", json=routes)
                assert response.status_code == 200, response.status_code
                body = {
                    "project_id": project,
                    "document_type": kind,
                    "title": kind + uuid4().hex[:6],
                    "objective": "车站片段",
                    "arguments": {kind: parameters},
                }
                if source_version:
                    body["source_version_id"] = source_version
                command = submit_generation(api, body, headers=headers)

                def accepted():
                    return receive_task(dispatch, jobs, command, workspace, settings)

                wait_for(accepted)
                job = jobs.claim_next(
                    workspace, "runtime", settings.worker_id, settings.job_lease_seconds, job_id=command.job_id
                )
                run_worker(jobs, settings, generate_content, job, url)
                finished = jobs.get_scoped(command.job_id, workspace)
                assert finished.status == "completed", finished.error or finished.result
                jobs.relay(workspace, settings.rocketmq_event_topic, relay.publish, 10)
                version_id = finished.result["document_version_id"]

                def projected():
                    response = content_version(api, version_id, project, headers=headers)
                    return response.json() if response.status_code == 200 else None

                document = wait_for(projected)
                assert document["document_type"] == kind
                value = document["blocks"][0]["metadata"]["asset"]
                stored = artifacts.client.get_object(Bucket=value["bucket"], Key=value["object_key"])
                try:
                    assert stored["Body"].read() == (PNG if kind == "image" else AUDIO)
                finally:
                    stored["Body"].close()
                assert "test-tts-key" not in json.dumps(document)
                with jobs.scope(workspace) as cursor:
                    cursor.execute("SELECT state FROM generated_media_tasks WHERE job_id=%s", (command.job_id,))
                    assert cursor.fetchone()[0] == "outputs_ready"
                    cursor.execute(
                        "SELECT count(*) FROM model_calls WHERE job_id=%s AND metadata->>'purpose'=%s",
                        (command.job_id, purpose),
                    )
                    assert cursor.fetchone()[0] == 1
                return version_id

            first = generate("image", "openai_compatible", {"operation": "generate"})
            generate("image", "openai_compatible", {"operation": "edit"}, first)
            generate("image", "volcengine", {"operation": "generate"})
            generate("image", "volcengine", {"operation": "edit"}, first)
            generate("audio", "openai_compatible", {"voice": "test-voice", "text": "欢迎来到车站。"})
            generate("audio", "volcengine_tts", {"voice": "test-voice", "language": "zh"})
            assert calls == {"image": 1, "edit": 1, "seedream": 1, "seedream_edit": 1, "speech": 1, "doubao": 1}
        finally:
            relay.close()
            dispatch.close()
            jobs.close()
