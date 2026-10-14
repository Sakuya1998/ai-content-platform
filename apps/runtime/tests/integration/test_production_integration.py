"""Opt-in AI planning + real FFmpeg/MQ/RustFS production chain."""

import json
import os
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.domain.content_adapters import with_block_ids
from agent_runtime.execution.handlers import generate_content
from agent_runtime.infrastructure.mq.rocketmq import RocketMQRuntimeClient
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.settings import RuntimeSettings
from tests.integration.helpers import content_version, receive_task, run_worker, submit_generation
from tests.integration.test_handler_integration import wait_for


@contextmanager
def planner_provider(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = payload["messages"][-1]["content"]
            if "shots 是镜头数组" in text:
                value = {
                    "shots": [
                        {
                            "shot_id": "shot-1",
                            "description": "红色静帧",
                            "duration_ms": 1000,
                            "source_block_ids": [state["block_ids"][0]],
                        },
                        {
                            "shot_id": "shot-2",
                            "description": "蓝色镜头",
                            "duration_ms": 1000,
                            "source_block_ids": [state["block_ids"][1]],
                        },
                    ]
                }
            elif "唯一字段 timeline" in text:
                value = {"timeline": state["timeline"]}
            else:
                value = {"approved": True, "notes": []}
            result = {
                "id": "planner-test",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "chat-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            }
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_storyboard_timeline_compose_freezes_inputs_and_publishes_real_video():
    url = os.getenv("PRODUCTION_TEST_CONTROL_URL")
    worker = os.getenv("PRODUCTION_TEST_WORKER_CONTAINER")
    control_db = os.getenv("MQ_TEST_CONTROL_DATABASE_URL")
    runtime_image = os.getenv("PRODUCTION_TEST_RUNTIME_IMAGE", "novel-agent-runtime:local")
    if not url or not worker or not control_db:
        pytest.skip("production integration endpoints/container are not configured")
    settings = RuntimeSettings(_env_file=None)
    state = {}
    with (
        tempfile.TemporaryDirectory(prefix="production-fixtures-") as directory,
        planner_provider(state) as endpoint,
        httpx.Client(base_url=url, timeout=30) as api,
    ):
        fixture = Path(directory)
        fixture_commands = [
            ["-f", "lavfi", "-i", "color=c=red:s=160x90", "-frames:v", "1", "-threads", "1", "/fixtures/image.png"],
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:r=25:d=2",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "1",
                "/fixtures/video.mp4",
            ],
            ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "pcm_s16le", "/fixtures/audio.wav"],
        ]
        for argv in fixture_commands:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-v",
                    f"{fixture}:/fixtures",
                    runtime_image,
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    *argv,
                ],
                check=True,
                capture_output=True,
            )
        response = api.post(
            "/api/auth/register",
            json={
                "username": "production_" + uuid4().hex[:10],
                "password": "production-test-password",
                "workspace_name": "Production acceptance",
            },
        )
        assert response.status_code == 201, response.status_code
        identity = response.json()
        workspace = identity["user"]["workspace_id"]
        api.headers["X-CSRF-Token"] = identity["csrf_token"]
        response = api.post("/api/v2/projects", json={"content_type": "storyboard", "title": "Production acceptance"})
        assert response.status_code == 201, response.status_code
        project = response.json()["id"]
        response = api.post(
            "/api/model-settings/profiles",
            json={
                "name": "Mock planner",
                "provider": "openai_compatible",
                "base_url": endpoint,
                "api_key": "planner-test-key",
                "chat_models": ["chat-test"],
                "embedding_models": ["embed-test"],
            },
        )
        assert response.status_code == 201, response.status_code
        profile = response.json()["id"]
        response = api.put(
            "/api/model-settings/routes",
            json={
                purpose: {"profile_id": profile, "model_name": "embed-test" if purpose == "embedding" else "chat-test"}
                for purpose in ["creative", "analysis", "embedding"]
            },
        )
        assert response.status_code == 200, response.status_code
        storage = S3ArtifactStore(settings)
        versions = []
        documents = []
        for kind, filename, mime in [
            ("image", "image.png", "image/png"),
            ("video", "video.mp4", "video/mp4"),
            ("audio", "audio.wav", "audio/wav"),
        ]:
            document_id, version_id = str(uuid4()), str(uuid4())
            ref = storage.upload(
                workspace,
                project,
                "assets",
                document_id,
                filename,
                (fixture / filename).read_bytes(),
                version_id=version_id,
                content_type=mime,
            )
            document = ContentDocument(
                document_id=document_id,
                project_id=project,
                document_type=kind,
                schema_version="1.0",
                blocks=[ContentBlock(type=kind, text=kind + " fixture", metadata={"asset": asdict(ref)})],
            )
            with psycopg.connect(control_db) as db:
                db.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
                db.execute(
                    "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES(%s,%s,%s,%s,%s)",
                    (document_id, workspace, project, kind, "Fixture " + document_id),
                )
                db.execute(
                    "INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,"
                    "content_hash,payload,status) VALUES(%s,%s,%s,1,'1.0',%s,%s,'approved')",
                    (
                        version_id,
                        workspace,
                        document_id,
                        document.content_hash,
                        Jsonb(document.model_dump(mode="json")),
                    ),
                )
                db.execute("UPDATE documents SET current_version_id=%s WHERE id=%s", (version_id, document_id))
            versions.append(version_id)
            documents.append(document)
        state["block_ids"] = [with_block_ids(d).document.blocks[0].metadata["block_id"] for d in documents]
        state["timeline"] = {
            "schema_version": "timeline@1",
            "width": 320,
            "height": 180,
            "fps": 25,
            "duration_ms": 2000,
            "assets": [
                {"asset_id": kind, "document_version_id": vid, "block_position": 0}
                for kind, vid in zip(["image", "video", "audio"], versions, strict=True)
            ],
            "shots": [
                {"shot_id": "s1", "asset_id": "image", "start_ms": 0, "duration_ms": 1000},
                {"shot_id": "s2", "asset_id": "video", "start_ms": 1000, "duration_ms": 1000, "source_start_ms": 1000},
            ],
            "audio_tracks": [
                {"track_id": "voice", "asset_id": "audio", "start_ms": 0, "duration_ms": 2000, "gain": 1.0}
            ],
            "subtitles": [{"cue_id": "c1", "start_ms": 0, "end_ms": 2000, "text": "合成验收字幕"}],
        }
        headers = {"X-Workspace-ID": workspace}
        store = RuntimeMQStore(settings.database_url)
        dispatch, relay = [RocketMQRuntimeClient(settings, role) for role in ["dispatch", "relay"]]
        try:
            dispatch.start()
            relay.start()

            def execute(kind, source=None, operation=None):
                body = {
                    "project_id": project,
                    "document_type": kind,
                    "title": kind + uuid4().hex[:6],
                    "objective": "创作两秒分镜并合成视频",
                    "input_version_ids": versions,
                }
                if source:
                    body["source_version_id"] = source
                if operation:
                    body.update(operation=operation, arguments={"compose": {"preset": "draft"}})
                command = submit_generation(api, body, headers=headers)

                def accepted():
                    return receive_task(dispatch, store, command, workspace, settings)

                wait_for(accepted)
                if operation:
                    process = subprocess.run(
                        [
                            "docker",
                            "exec",
                            worker,
                            "python",
                            "-m",
                            "scripts.platform_task_once",
                            "--workspace-id",
                            workspace,
                            "--job-id",
                            command.job_id,
                        ],
                        capture_output=True,
                        text=True,
                    )
                    assert process.returncode == 0, process.stdout.splitlines()[-1:] or process.returncode
                else:
                    job = store.claim_next(
                        workspace, "runtime", settings.worker_id, settings.job_lease_seconds, job_id=command.job_id
                    )
                    run_worker(store, settings, generate_content, job, url)
                finished = store.get_scoped(command.job_id, workspace)
                assert finished.status == "completed", finished.error or finished.result
                store.relay(workspace, settings.rocketmq_event_topic, relay.publish, 10)
                vid = finished.result["document_version_id"]

                def projected():
                    r = content_version(api, vid, project, headers=headers)
                    return r.json() if r.status_code == 200 else None

                return command, {"document_version_id": vid, "document": wait_for(projected)}

            _, board = execute("storyboard")
            assert len(board["document"]["metadata"]["storyboard"]["shots"]) == 2
            _, timeline = execute("timeline", board["document_version_id"])
            composed, video = execute("video", timeline["document_version_id"], "compose")
            metadata = video["document"]["blocks"][0]["metadata"]
            assert metadata["probe"]["has_audio"] and metadata["probe"]["width"] == 320
            ref = metadata["asset"]
            remote = storage.client.get_object(Bucket=ref["bucket"], Key=ref["object_key"])
            try:
                (fixture / "output.mp4").write_bytes(remote["Body"].read())
            finally:
                remote["Body"].close()
            probe = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-v",
                    f"{fixture}:/fixtures:ro",
                    runtime_image,
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,width,height",
                    "-of",
                    "json",
                    "/fixtures/output.mp4",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            actual = json.loads(probe.stdout)
            assert abs(float(actual["format"]["duration"]) - 2) < 0.1
            assert {s["codec_type"] for s in actual["streams"]} == {"video", "audio"}
            with store.scope(workspace) as cursor:
                cursor.execute("SELECT state FROM composition_runs WHERE job_id=%s", (composed.job_id,))
                assert cursor.fetchone()[0] == "outputs_ready"
                cursor.execute("SELECT count(*) FROM model_calls WHERE job_id=%s", (composed.job_id,))
                assert cursor.fetchone()[0] == 0
            with psycopg.connect(control_db) as db:
                db.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
                assert (
                    db.execute(
                        "SELECT count(*) FROM derivations WHERE target_version_id=%s", (video["document_version_id"],)
                    ).fetchone()[0]
                    == 4
                )
        finally:
            relay.close()
            dispatch.close()
            store.close()
