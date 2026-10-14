"""Run the local post-migration control-plane/runtime integration smoke test."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with _opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def wait_ready(url: str, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = request_json(url)
            if status == 200:
                return
        except OSError, urllib.error.URLError:
            pass
        time.sleep(1)
    raise RuntimeError(f"service did not become ready: {url}")


def seed_workspace(compose_file: str, workspace_id: str, database_url: str = "") -> None:
    if database_url:
        import psycopg

        with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (workspace_id, "Integration"),
            )
        return
    command = [
        "docker",
        "compose",
        "-f",
        compose_file,
        "exec",
        "-T",
        "postgres-control",
        "psql",
        "-U",
        "control",
        "-d",
        "control",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        f"INSERT INTO workspaces (id, name) VALUES ('{workspace_id}', 'Integration') ON CONFLICT (id) DO NOTHING;",
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def check_tcp(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=5):
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", default="docker-compose.platform.yml")
    parser.add_argument("--control-url", default="http://localhost:8080")
    parser.add_argument("--runtime-url", default="http://localhost:8000")
    parser.add_argument("--control-database-url", default=os.getenv("CONTROL_DATABASE_URL", ""))
    parser.add_argument("--rocketmq-endpoint", default=os.getenv("RUNTIME_ROCKETMQ_ENDPOINT", "localhost:8081"))
    parser.add_argument(
        "--completion-timeout",
        type=int,
        default=0,
        help="Wait for projected task completion; requires configured Runtime model workers",
    )
    args = parser.parse_args()

    wait_ready(f"{args.control_url}/readyz")
    wait_ready(f"{args.runtime_url}/readyz")
    mq_host, mq_port = args.rocketmq_endpoint.rsplit(":", 1)
    check_tcp(mq_host, int(mq_port))

    workspace_id = str(uuid.uuid4())
    status, auth_status = request_json(args.control_url + "/api/auth/status")
    control_headers = {}
    if status == 200 and auth_status.get("enabled"):
        identity = "integration_" + uuid.uuid4().hex[:12]
        registered, user = request_json(
            args.control_url + "/api/auth/register",
            method="POST",
            payload={
                "username": identity,
                "password": uuid.uuid4().hex + uuid.uuid4().hex,
                "workspace_name": "Integration",
            },
        )
        if registered != 201:
            raise RuntimeError("integration user registration failed")
        workspace_id = user["user"]["workspace_id"]
        control_headers = {"X-CSRF-Token": user["csrf_token"]}
    else:
        seed_workspace(args.compose_file, workspace_id, args.control_database_url)
    status, project = request_json(
        f"{args.control_url}/api/v2/projects",
        method="POST",
        headers={**control_headers, "X-Workspace-ID": workspace_id},
        payload={
            "content_type": "screenplay",
            "title": "Integration Project",
            "input": {"objective": "Write one short scene about a train arriving at dawn."},
        },
    )
    if status != 201 or project.get("content_type") != "screenplay":
        raise RuntimeError(f"control project create failed: {status} {project}")

    task = {
        "project_id": project["id"],
        "workflow": "content.generate",
        "workflow_version": 1,
        "operation": "run",
        "arguments": {},
    }
    task_headers = {**control_headers, "X-Workspace-ID": workspace_id, "Idempotency-Key": uuid.uuid4().hex}
    first_status, first = request_json(
        f"{args.control_url}/api/v2/jobs",
        method="POST",
        payload=task,
        headers=task_headers,
    )
    replay_status, replay = request_json(
        f"{args.control_url}/api/v2/jobs",
        method="POST",
        payload=task,
        headers=task_headers,
    )
    conflict = {**task, "arguments": {"objective": "A different brief"}}
    conflict_status, _ = request_json(
        f"{args.control_url}/api/v2/jobs",
        method="POST",
        payload=conflict,
        headers=task_headers,
    )
    if (
        first_status != 202
        or replay_status != 202
        or first.get("job_id") != replay.get("job_id")
        or conflict_status != 409
    ):
        raise RuntimeError(f"task submission contract failed: {first_status}, {replay_status}, {conflict_status}")
    job_id = first["job_id"]
    if args.completion_timeout:
        deadline = time.monotonic() + args.completion_timeout
        while time.monotonic() < deadline:
            status, projected = request_json(
                f"{args.control_url}/api/v2/jobs/{job_id}", headers={**control_headers, "X-Workspace-ID": workspace_id}
            )
            if status != 200:
                raise RuntimeError(f"task projection query failed: {status}")
            if projected.get("status") == "completed":
                break
            if projected.get("status") in {"failed", "cancelled"}:
                raise RuntimeError(f"task did not complete: {projected['status']}")
            time.sleep(1)
        else:
            raise RuntimeError("task completion was not projected before timeout")

    print(json.dumps({"status": "passed", "project_id": project["id"], "job_id": job_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
