"""Remote CLI for the Go management API and Python Runtime execution."""

import argparse
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--identifier", default=os.getenv("PLATFORM_CLI_IDENTIFIER", ""))
    parser.add_argument("--password-env", default="PLATFORM_CLI_PASSWORD")
    parser.add_argument("--title")
    parser.add_argument("--content-type", default="novel", choices=["novel", "screenplay", "copy"])
    parser.add_argument("--genre", default="fiction")
    parser.add_argument("--inspiration")
    parser.add_argument("--chapters", type=int, default=3)
    parser.add_argument("--style", default="gu_long")
    parser.add_argument("--resume", metavar="PROJECT_UUID")
    parser.add_argument("--feedback", default="approve")
    parser.add_argument("--scene-number", type=int)
    parser.add_argument("--version-number", type=int)
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--planning-review", action="store_true")
    parser.add_argument("--export")
    parser.add_argument("--format", default="markdown", choices=["markdown", "txt", "docx", "epub", "backup"])
    args = parser.parse_args(argv)
    password = os.getenv(args.password_env, "")
    if not args.identifier or not password:
        parser.error("identifier and password environment variable are required")
    if not args.resume and (not args.title or not args.inspiration):
        parser.error("new project requires title and inspiration")
    if args.export and Path(args.export).exists():
        parser.error("export destination already exists")
    with httpx.Client(base_url=args.platform_url.rstrip("/"), timeout=60, follow_redirects=False) as client:

        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            if response.status_code >= 400:
                raise RuntimeError(f"platform request failed with HTTP {response.status_code}")
            return response

        logged = request("POST", "/api/auth/login", json={"identifier": args.identifier, "password": password}).json()
        client.headers["X-CSRF-Token"] = logged["csrf_token"]
        if args.resume:
            identity = args.resume
        else:
            identity = request(
                "POST",
                "/api/v2/projects",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "title": args.title,
                    "content_type": args.content_type,
                    "input": {
                        "objective": args.inspiration,
                        "genre": args.genre,
                        "style": args.style,
                        "inspiration": args.inspiration,
                        "total_chapters": args.chapters,
                        "planning_review_enabled": args.planning_review,
                    },
                    "brief": {},
                },
            ).json()["id"]
        print("project_id=" + identity)
        if args.export:
            response = request("GET", f"/api/v2/projects/{identity}/export", params={"format": args.format})
            with Path(args.export).open("xb") as destination:
                destination.write(response.content)
            print("export completed")
            return
        operation = "resume" if args.resume else "run"
        payload = {}
        if args.resume:
            payload = {"feedback": args.feedback, "action": "approve" if args.feedback == "approve" else "revise"}
            if args.scene_number:
                payload["scene_number"] = args.scene_number
            if args.version_number:
                payload.update(action="restore_version", version_number=args.version_number)
        while True:
            job = request(
                "POST",
                "/api/v2/jobs",
                json={
                    "project_id": identity,
                    "workflow": "novel.write" if args.content_type == "novel" else "content.generate",
                    "workflow_version": 1,
                    "operation": operation,
                    "arguments": payload,
                },
                headers={"Idempotency-Key": str(uuid4())},
            ).json()["job_id"]
            deadline = time.monotonic() + 3600
            while time.monotonic() < deadline:
                result = request("GET", "/api/v2/jobs/" + job).json()
                if result["status"] in {"completed", "failed", "cancelled", "waiting_review"}:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("job polling deadline exceeded; query the job ID before retrying")
            print("job_id=" + job + " status=" + result["status"])
            if result["status"] in {"failed", "cancelled"}:
                raise RuntimeError("platform execution did not complete")
            if result["status"] != "waiting_review" or not args.auto:
                return
            operation, payload = "resume", {"action": "approve", "feedback": "approve"}
