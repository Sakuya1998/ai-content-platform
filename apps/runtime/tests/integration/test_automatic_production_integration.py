"""Opt-in parent scheduler creates missing assets and delivers a real narrated video."""

import base64
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
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.domain.protocol import RuntimeCommand
from agent_runtime.domain.storyboard import Storyboard, storyboard_document
from agent_runtime.infrastructure.mq.rocketmq import RocketMQRuntimeClient
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.settings import RuntimeSettings
from tests.integration.helpers import content_version, inspect_snapshot


@contextmanager
def generated_assets(png, audio, mp3, client_host="127.0.0.1"):
    counts = {"image": 0, "audio": 0, "music": 0, "effect": 0, "llm": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            path = urlsplit(self.path).path
            if path.endswith("/images/generations"):
                counts["image"] += 1
                data = json.dumps({"data": [{"b64_json": base64.b64encode(png).decode()}]}).encode()
                content_type = "application/json"
            elif path.endswith("/audio/speech"):
                counts["audio"] += 1
                assert payload["input"] == "欢迎来到车站。"
                data = audio
                content_type = "audio/wav"
            elif path.endswith("/music"):
                counts["music"] += 1
                assert self.headers.get("xi-api-key") == "elevenlabs-test-key"
                assert payload["model_id"] == "music_v2_5" and payload["force_instrumental"] is True
                data = mp3
                content_type = "audio/mpeg"
            elif path.endswith("/sound-generation"):
                counts["effect"] += 1
                assert self.headers.get("xi-api-key") == "elevenlabs-test-key"
                assert payload["model_id"] == "eleven_text_to_sound_v2"
                data = mp3
                content_type = "audio/mpeg"
            else:
                counts["llm"] += 1
                text = payload["messages"][-1]["content"]
                if "唯一字段 prompt" in text:
                    value = {"prompt": "一个暖色车站镜头"}
                elif "唯一字段 text" in text:
                    value = {"text": "雨中脚步轻轻经过站台"}
                else:
                    value = {"approved": True, "notes": []}
                response = {
                    "id": "chat-test",
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
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                }
                data = json.dumps(response).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{client_host}:{server.server_port}/v1", counts
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_production_reuses_asset_generates_missing_media_and_freezes_routes():
    url = os.getenv("AUTOMATIC_TEST_CONTROL_URL")
    worker = os.getenv("AUTOMATIC_TEST_WORKER_CONTAINER")
    control_db = os.getenv("MQ_TEST_CONTROL_DATABASE_URL")
    runtime_image = os.getenv("PRODUCTION_TEST_RUNTIME_IMAGE", "novel-agent-runtime:local")
    if not url or not worker or not control_db:
        pytest.skip("automatic production endpoints/container are not configured")
    settings = RuntimeSettings(_env_file=None)
    gateway_result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{println .Gateway}}{{end}}",
            worker,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    gateway = next((line.strip() for line in gateway_result.stdout.splitlines() if line.strip()), None)
    if gateway is None:
        raise AssertionError("worker container has no Docker network gateway")
    with tempfile.TemporaryDirectory(prefix="automatic-fixtures-") as folder:
        root = Path(folder)
        for argv in [
            ["-f", "lavfi", "-i", "color=c=red:s=160x90", "-frames:v", "1", "-threads", "1", "/fixtures/image.png"],
            ["-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", "-c:a", "pcm_s16le", "/fixtures/audio.wav"],
            ["-f", "lavfi", "-i", "sine=frequency=660:duration=4", "-codec:a", "libmp3lame", "/fixtures/generated.mp3"],
        ]:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-v",
                    f"{root}:/fixtures",
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
        with (
            generated_assets(
                (root / "image.png").read_bytes(),
                (root / "audio.wav").read_bytes(),
                (root / "generated.mp3").read_bytes(),
                client_host=gateway,
            ) as (
                endpoint,
                counts,
            ),
            httpx.Client(base_url=url, timeout=30) as api,
        ):
            response = api.post(
                "/api/auth/register",
                json={
                    "username": "automatic_" + uuid4().hex[:10],
                    "password": "automatic-test-password",
                    "workspace_name": "Automatic acceptance",
                },
            )
            assert response.status_code == 201, response.status_code
            identity = response.json()
            workspace = identity["user"]["workspace_id"]
            api.headers["X-CSRF-Token"] = identity["csrf_token"]
            response = api.post(
                "/api/v2/projects", json={"content_type": "storyboard", "title": "Automatic acceptance"}
            )
            assert response.status_code == 201, response.status_code
            project = response.json()["id"]
            profile = {
                "name": "Original frozen provider",
                "provider": "openai_compatible",
                "base_url": endpoint,
                "api_key": "fake-key",
                "chat_models": ["chat-test"],
                "embedding_models": ["embed-test"],
                "image_models": [
                    {
                        "model_name": "image-test",
                        "capabilities": {
                            "generation": True,
                            "editing": False,
                            "sizes": ["1024x1024"],
                            "formats": ["png"],
                            "max_images": 1,
                        },
                    }
                ],
                "speech_models": [
                    {
                        "model_name": "speech-test",
                        "capabilities": {
                            "voices": ["alloy"],
                            "formats": ["wav"],
                            "languages": ["zh"],
                            "min_speed": 0.25,
                            "max_speed": 4.0,
                            "max_input_chars": 4096,
                            "instructions": False,
                        },
                    }
                ],
            }
            response = api.post("/api/model-settings/profiles", json=profile)
            assert response.status_code == 201, response.status_code
            profile_id = response.json()["id"]
            audio_profile = {
                "name": "ElevenLabs music and effects",
                "provider": "elevenlabs",
                "base_url": endpoint,
                "api_key": "elevenlabs-test-key",
                "music_models": [
                    {
                        "model_name": "music_v2_5",
                        "capabilities": {
                            "output_formats": ["mp3_44100_128"],
                            "min_duration_ms": 3000,
                            "max_duration_ms": 600000,
                            "instrumental": True,
                        },
                    }
                ],
                "sound_effect_models": [
                    {
                        "model_name": "eleven_text_to_sound_v2",
                        "capabilities": {
                            "output_formats": ["mp3_44100_128"],
                            "min_duration_seconds": 0.5,
                            "max_duration_seconds": 30,
                            "prompt_influence": True,
                            "loop": True,
                        },
                    }
                ],
            }
            response = api.post("/api/model-settings/profiles", json=audio_profile)
            assert response.status_code == 201, response.status_code
            audio_profile_id = response.json()["id"]
            purposes = {
                "creative": "chat-test",
                "analysis": "chat-test",
                "embedding": "embed-test",
                "image_generation": "image-test",
                "speech_synthesis": "speech-test",
            }
            routes = {kind: {"profile_id": profile_id, "model_name": name} for kind, name in purposes.items()}
            routes["music_generation"] = {"profile_id": audio_profile_id, "model_name": "music_v2_5"}
            routes["sound_effect_generation"] = {
                "profile_id": audio_profile_id,
                "model_name": "eleven_text_to_sound_v2",
            }
            response = api.put("/api/model-settings/routes", json=routes)
            assert response.status_code == 200, response.status_code
            route_version = response.json()["route_version_id"]
            storage = S3ArtifactStore(settings)
            image_id, image_version, board_version = map(str, [uuid4(), uuid4(), uuid4()])
            ref = storage.upload(
                workspace,
                project,
                "assets",
                image_id,
                "image.png",
                (root / "image.png").read_bytes(),
                version_id=image_version,
                content_type="image/png",
            )
            image = ContentDocument(
                document_id=image_id,
                project_id=project,
                document_type="image",
                schema_version="1.0",
                blocks=[ContentBlock(type="image", text="Reuse original image", metadata={"asset": asdict(ref)})],
            )
            board = storyboard_document(
                Storyboard(
                    shots=[
                        {"shot_id": "s1", "description": "红色静帧", "duration_ms": 1000},
                        {"shot_id": "s2", "description": "暖色车站", "dialogue": "欢迎来到车站。", "duration_ms": 1000},
                    ]
                ),
                document_id=str(uuid4()),
                project_id=project,
            )
            with psycopg.connect(control_db) as db:
                db.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
                for vid, doc in [(image_version, image), (board_version, board)]:
                    db.execute(
                        "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES(%s,%s,%s,%s,%s)",
                        (doc.document_id, workspace, project, doc.document_type, "Fixture " + doc.document_id),
                    )
                    db.execute(
                        "INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,"
                        "content_hash,payload,status) VALUES(%s,%s,%s,1,'1.0',%s,%s,'approved')",
                        (vid, workspace, doc.document_id, doc.content_hash, Jsonb(doc.model_dump(mode="json"))),
                    )
                    db.execute("UPDATE documents SET current_version_id=%s WHERE id=%s", (vid, doc.document_id))
            headers = {"X-Workspace-ID": workspace}
            brief = {
                "project_id": project,
                "storyboard_version_id": board_version,
                "title": "自动车站成片",
                "idempotency_key": uuid4().hex,
                "visual_mode": "image",
                "width": 320,
                "height": 180,
                "fps": 25,
                "subtitles": True,
                "generate_speech": True,
                "generate_music": True,
                "generate_sound_effects": True,
                "image_parameters": {"format": "png"},
                "video_parameters": {},
                "audio_parameters": {"voice": "alloy", "format": "wav"},
                "bindings": [{"shot_id": "s1", "visual_version_id": image_version}],
            }
            public_path = f"/api/v2/projects/{project}/productions"
            response = api.post(public_path, headers=headers, json=brief)
            assert response.status_code == 202, response.status_code
            run_id = response.json()["run_id"]
            public_replay = api.post(public_path, json=brief)
            assert public_replay.status_code == 202 and public_replay.json()["run_id"] == run_id
            assert api.get(public_path + "/" + run_id).status_code == 200
            assert api.get(f"/api/v2/projects/{uuid4()}/productions/{run_id}").status_code == 404
            replay = api.post(public_path, headers=headers, json=brief)
            assert replay.status_code == 202 and replay.json()["run_id"] == run_id
            # Changing the defaults after creation must not alter any child, including future stages.
            response = api.post(
                "/api/model-settings/profiles",
                json={**profile, "name": "Changed defaults", "base_url": "http://127.0.0.1:1/v1"},
            )
            assert response.status_code == 201, response.status_code
            changed_id = response.json()["id"]
            response = api.put(
                "/api/model-settings/routes",
                json={
                    **{kind: {"profile_id": changed_id, "model_name": name} for kind, name in purposes.items()},
                    "music_generation": routes["music_generation"],
                    "sound_effect_generation": routes["sound_effect_generation"],
                },
            )
            assert response.status_code == 200, response.status_code
            jobs = RuntimeMQStore(settings.database_url)
            dispatch, relay = [RocketMQRuntimeClient(settings, role) for role in ["dispatch", "relay"]]
            done = set()
            try:
                dispatch.start()
                relay.start()
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    dispatch.receive(lambda body: jobs.accept_task(RuntimeCommand.model_validate_json(body)))
                    response = api.get(public_path + "/" + run_id, headers=headers)
                    assert response.status_code == 200, response.status_code
                    run = response.json()
                    assert run["status"] not in {"failed", "cancelled", "waiting_review"}, run
                    for child in run["children"]:
                        jid = child["job_id"]
                        if jid in done:
                            continue
                        try:
                            job = jobs.get_scoped(jid, workspace)
                        except KeyError:
                            continue
                        if job.status == "accepted":
                            bundle = inspect_snapshot(job, settings)
                            assert set(bundle["snapshot"]["model_routes"].values()) == {route_version}
                            process = subprocess.run(
                                [
                                    "docker",
                                    "exec",
                                    worker,
                                    "python",
                                    "-m",
                                    "tools.migrate.platform_task_once",
                                    "--workspace-id",
                                    workspace,
                                    "--job-id",
                                    jid,
                                ],
                                capture_output=True,
                                text=True,
                            )
                            assert process.returncode == 0, {
                                "child": child["slot"],
                                "stdout": process.stdout.splitlines()[-1:],
                                "llm_count": counts["llm"],
                                "provider_counts": {key: counts[key] for key in ["image", "audio", "music", "effect"]},
                            }
                            assert jobs.get_scoped(jid, workspace).status == "completed"
                            done.add(jid)
                    jobs.relay(workspace, settings.rocketmq_event_topic, relay.publish, 20)
                    if run["status"] == "completed":
                        break
                    time.sleep(0.1)
                else:
                    raise AssertionError("production run did not finish within deadline")
                assert len(done) == 7 and len(run["children"]) == 7
                assert counts == {"image": 1, "audio": 1, "music": 1, "effect": 2, "llm": 9}
                result = content_version(api, run["final_version_id"], project, headers=headers)
                assert result.status_code == 200, result.status_code
                probe = result.json()["blocks"][0]["metadata"]["probe"]
                assert probe["has_audio"] and probe["has_video"] and abs(probe["duration"] - 2.5) < 0.1
                timeline = content_version(api, run["timeline_version_id"], project, headers=headers).json()[
                    "metadata"
                ]["timeline"]
                assert (
                    timeline["shots"][1]["duration_ms"] == 1500 and timeline["audio_tracks"][0]["duration_ms"] == 1500
                )
                assert {track["role"] for track in timeline["audio_tracks"]} == {"narration", "music", "effect"}
                music = next(track for track in timeline["audio_tracks"] if track["role"] == "music")
                assert music["gain"] == 0.25 and music["duration_ms"] == 2500
                assert sum(track["role"] == "effect" for track in timeline["audio_tracks"]) == 2
                assert timeline["subtitles"][0]["text"] == "欢迎来到车站。"
                with psycopg.connect(control_db) as db:
                    db.execute("UPDATE users SET role='viewer' WHERE id=%s", (identity["user"]["user_id"],))
                assert api.get(public_path + "/" + run_id).status_code == 200
                assert api.post(public_path + "/" + run_id + "/advance", json={}).status_code == 403
            finally:
                relay.close()
                dispatch.close()
                jobs.close()
