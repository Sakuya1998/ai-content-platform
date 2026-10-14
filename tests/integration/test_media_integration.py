"""Opt-in Go → MQ → Runtime → Ark mock → RustFS → Go acceptance."""

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


@contextmanager
def media_provider():
    counters = {"submitted": 0, "downloaded": 0, "polled": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, payload, content_type="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/contents/generations/tasks"):
                assert payload["model"] == "seedance-test-endpoint"
                assert payload["duration"] == 5
                assert payload["content"][0]["type"] == "text"
                counters["submitted"] += 1
                self.reply({"id": "video-test-remote"})
                return
            prompt = payload["messages"][-1]["content"]
            value = (
                {"prompt": "一个人物走进车站，镜头缓慢推进。"}
                if "唯一字段 prompt" in prompt
                else {"approved": True, "notes": []}
            )
            self.reply(
                {
                    "id": "chat-test",
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
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                }
            )

        def do_GET(self):
            if self.path == "/video.mp4":
                assert "Authorization" not in self.headers
                counters["downloaded"] += 1
                self.reply(b"\x00\x00\x00\x18ftypmp42mock-video", "video/mp4")
            else:
                counters["polled"] += 1
                self.reply(
                    {
                        "id": "video-test-remote",
                        "model": "seedance-test-endpoint",
                        "status": "succeeded",
                        "content": {"video_url": f"http://127.0.0.1:{self.server.server_port}/video.mp4"},
                        "usage": {"completion_tokens": 123, "total_tokens": 123},
                    }
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", counters
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_video_workflow_freezes_model_archives_asset_and_projects_manifest():
    url = os.getenv("MEDIA_TEST_CONTROL_URL")
    if not url:
        pytest.skip("MEDIA_TEST_CONTROL_URL is not configured")
    settings = RuntimeSettings(_env_file=None)
    with media_provider() as (endpoint, counters), httpx.Client(base_url=url, timeout=30) as api:
        result = api.post(
            "/api/auth/register",
            json={
                "username": "media_" + uuid4().hex[:12],
                "password": "media-test-password",
                "workspace_name": "Media acceptance",
            },
        )
        assert result.status_code == 201, result.status_code
        identity = result.json()
        workspace = identity["user"]["workspace_id"]
        api.headers["X-CSRF-Token"] = identity["csrf_token"]
        result = api.post("/api/v2/projects", json={"content_type": "video", "title": "Video acceptance"})
        assert result.status_code == 201, result.status_code
        project = result.json()["id"]
        result = api.post(
            "/api/model-settings/profiles",
            json={
                "name": "Mock analysis",
                "provider": "openai_compatible",
                "base_url": endpoint + "/v1",
                "api_key": "test-key",
                "chat_models": ["test-chat"],
                "embedding_models": ["test-embedding"],
            },
        )
        assert result.status_code == 201, result.status_code
        text_profile = result.json()["id"]
        result = api.post(
            "/api/model-settings/profiles",
            json={
                "name": "Mock video",
                "provider": "volcengine",
                "base_url": endpoint + "/api/v3",
                "api_key": "test-video-key",
                "video_models": [
                    {
                        "model_name": "seedance-test-endpoint",
                        "capabilities": {
                            "text_to_video": True,
                            "image_to_video": False,
                            "durations": [5],
                            "ratios": ["16:9"],
                            "resolutions": ["720p"],
                            "generate_audio": False,
                            "cancellation": False,
                        },
                    }
                ],
            },
        )
        assert result.status_code == 201, result.status_code
        video_profile = result.json()["id"]
        routes = {
            purpose: {
                "profile_id": text_profile,
                "model_name": "test-embedding" if purpose == "embedding" else "test-chat",
            }
            for purpose in ["creative", "analysis", "embedding"]
        }
        routes["video_generation"] = {"profile_id": video_profile, "model_name": "seedance-test-endpoint"}
        result = api.put("/api/model-settings/routes", json=routes)
        assert result.status_code == 200, result.status_code
        headers = {"X-Workspace-ID": workspace}
        store = RuntimeMQStore(settings.database_url)
        dispatch, relay = [RocketMQRuntimeClient(settings, role) for role in ["dispatch", "relay"]]
        try:
            dispatch.start()
            relay.start()
            command = submit_generation(
                api,
                {
                    "project_id": project,
                    "document_type": "video",
                    "title": "Video acceptance",
                    "objective": "一个人物走进车站",
                    "arguments": {"video": {"duration": 5, "ratio": "16:9", "resolution": "720p"}},
                },
                headers=headers,
            )

            def accepted():
                return receive_task(dispatch, store, command, workspace, settings)

            wait_for(accepted)
            job = store.claim_next(
                workspace, "runtime", settings.worker_id, settings.job_lease_seconds, job_id=command.job_id
            )
            assert job.command.job_id == command.job_id
            run_worker(store, settings, generate_content, job, url)
            finished = store.get_scoped(command.job_id, workspace)
            assert finished.status == "completed", finished.error
            store.relay(workspace, settings.rocketmq_event_topic, relay.publish, 10)
            version_id = finished.result["document_version_id"]

            def committed():
                response = content_version(api, version_id, project, headers=headers)
                return response.json() if response.status_code == 200 else None

            document = wait_for(committed)
            assert document["document_type"] == "video"
            metadata = document["blocks"][0]["metadata"]
            assert metadata["model_name"] == "seedance-test-endpoint"
            assert metadata["usage"] == {"completion_tokens": 123, "total_tokens": 123}
            assert metadata["asset"]["bucket"] == "agent-artifacts"
            assert "video_url" not in metadata
            assert "test-video-key" not in json.dumps(document)
            artifacts = S3ArtifactStore(settings)
            remote = artifacts.client.get_object(
                Bucket=metadata["asset"]["bucket"], Key=metadata["asset"]["object_key"]
            )
            try:
                assert remote["Body"].read().endswith(b"mock-video")
            finally:
                remote["Body"].close()
            assert counters == {"submitted": 1, "downloaded": 1, "polled": 1}
            with store.scope(workspace) as cursor:
                cursor.execute("SELECT state,remote_task_id FROM media_tasks WHERE job_id=%s", (command.job_id,))
                assert cursor.fetchone() == ("succeeded", "video-test-remote")
                cursor.execute("SELECT count(*) FROM model_calls WHERE job_id=%s", (command.job_id,))
                assert cursor.fetchone()[0] == 3
                cursor.execute(
                    "SELECT output_tokens,metadata->>'modality' FROM model_calls "
                    "WHERE job_id=%s AND metadata->>'purpose'='video_generation'",
                    (command.job_id,),
                )
                assert cursor.fetchone() == (123, "video")
        finally:
            relay.close()
            dispatch.close()
            store.close()
