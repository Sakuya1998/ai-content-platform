"""Verify public task submission, actual MQ delivery, S3 artifacts and cancellation."""

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict
from uuid import uuid4

import httpx

from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent
from agent_runtime.execution.context import current_execution
from agent_runtime.execution.snapshots import load_execution_snapshot
from agent_runtime.execution.worker import DurableWorker
from agent_runtime.infrastructure.mq.rocketmq import RocketMQRuntimeClient
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.settings import RuntimeSettings


def wait_for(predicate, description):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.1)
    raise AssertionError(description)


async def diagnostic_handler(command):
    execution = current_execution()
    snapshot = await load_execution_snapshot(command, execution.settings)
    request = snapshot["request"]
    document = ContentDocument(
        document_id=request["document_id"],
        project_id=command.project_id,
        document_type=request["document_type"],
        schema_version="1.0",
        version=request["version"],
        blocks=[ContentBlock(type="text", text="MQ diagnostic content", position=0)],
    )
    version_id = str(uuid4())
    artifact = S3ArtifactStore(execution.settings).upload(
        command.workspace_id,
        command.project_id,
        "documents",
        document.document_id,
        "content.json",
        document.model_dump_json().encode(),
        version_id=version_id,
        content_type="application/json",
    )
    execution.store.record_artifact(
        command.job_id, command.workspace_id, execution.settings.worker_id, execution.job.fencing_token, artifact
    )
    return {
        "mq_check": True,
        "artifact": asdict(artifact),
        "content_commit": {
            "document_id": document.document_id,
            "document_version_id": version_id,
            "source_version": document.version,
            "document_type": document.document_type,
            "title": request["title"],
            "artifact": asdict(artifact),
            "ir_hash": document.content_hash,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.runtime")
    parser.add_argument("--control-url", default="http://localhost:8080")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--username", default=os.getenv("PLATFORM_VERIFY_USERNAME", ""))
    args = parser.parse_args()
    settings = RuntimeSettings(_env_file=args.env_file)
    password = os.getenv("PLATFORM_VERIFY_PASSWORD", "")
    if not args.username or not password or not settings.database_url or not settings.rocketmq_endpoint:
        parser.error("Username, PLATFORM_VERIFY_PASSWORD, Runtime database and Proxy endpoint are required")
    store = RuntimeMQStore(settings.database_url)
    clients = {
        role: RocketMQRuntimeClient(settings, role)
        for role in ["dispatch", "control", "relay", "memory-scheduler", "memory"]
    }
    started = []
    try:
        with httpx.Client(base_url=args.control_url, timeout=30) as api:
            response = api.post("/api/auth/login", json={"username": args.username, "password": password})
            if response.status_code != 200:
                raise ValueError("public Control session login failed")
            api.headers.update({"X-CSRF-Token": response.json()["csrf_token"], "X-Workspace-ID": args.workspace_id})
            for client in clients.values():
                client.start()
                started.append(client)

            def submit(key):
                response = api.post(
                    "/api/v2/jobs",
                    headers={"Idempotency-Key": key},
                    json={
                        "project_id": args.project_id,
                        "workflow": "content.generate",
                        "workflow_version": 1,
                        "operation": "run",
                        "arguments": {},
                    },
                )
                assert response.status_code == 202, "public task submission failed"
                return response.json()["job_id"]

            key = uuid4().hex
            job_id = submit(key)
            assert submit(key) == job_id

            def accept_task():
                clients["dispatch"].receive(lambda body: store.accept_task(RuntimeCommand.model_validate_json(body)))
                try:
                    return store.get_scoped(job_id, args.workspace_id)
                except KeyError:
                    return None

            accepted = wait_for(accept_task, "Control Outbox did not reach Runtime Inbox")
            assert accepted.command.payload == {} and accepted.command.execution_snapshot
            job = store.claim_next(
                args.workspace_id, "runtime", settings.worker_id, settings.job_lease_seconds, job_id=job_id
            )
            asyncio.run(DurableWorker(store, settings, diagnostic_handler).execute(job))
            store.relay(args.workspace_id, settings.rocketmq_event_topic, clients["relay"].publish, 10)
            wait_for(
                lambda: api.get("/api/v2/jobs/" + job_id).json().get("status") == "completed",
                "Runtime completion did not reach Control projection",
            )

            def schedule_memory():
                clients["memory-scheduler"].receive(
                    lambda body: store.schedule_memory(
                        RuntimeEvent.model_validate_json(body), settings.rocketmq_memory_topic
                    )
                )
                store.relay(args.workspace_id, settings.rocketmq_event_topic, clients["relay"].publish, 10)
                clients["memory"].receive(
                    lambda body: store.accept_task(RuntimeCommand.model_validate_json(body), "memory")
                )
                return store.claim_next(args.workspace_id, "memory", settings.worker_id, settings.job_lease_seconds)

            memory = wait_for(schedule_memory, "content commit did not schedule memory")
            assert memory.command.payload["source_execution_snapshot"] and memory.command.payload["artifact"]

            async def diagnostic_memory(command):
                return {"nodes": [{"memory_type": "summary", "content": "MQ diagnostic summary"}]}

            asyncio.run(DurableWorker(store, settings, diagnostic_memory).execute(memory))
            with store.scope(args.workspace_id) as cursor:
                cursor.execute(
                    "SELECT count(*) FROM memory_nodes WHERE metadata->>'source_command_id'=%s",
                    (memory.command.command_id,),
                )
                assert cursor.fetchone()[0] == 1

            cancelled = submit(uuid4().hex)
            response = api.post(
                "/api/v2/jobs/" + cancelled + "/cancel", json={}, headers={"Idempotency-Key": uuid4().hex}
            )
            assert response.status_code == 202

            def cancelled_before_task():
                clients["control"].receive(lambda body: store.accept_control(RuntimeCommand.model_validate_json(body)))
                with store.scope(args.workspace_id) as cursor:
                    cursor.execute("SELECT 1 FROM runtime_cancellations WHERE job_id=%s", (cancelled,))
                    return cursor.fetchone()

            wait_for(cancelled_before_task, "cancellation command did not reach Runtime")
            clients["dispatch"].receive(lambda body: store.accept_task(RuntimeCommand.model_validate_json(body)))
            assert store.get_scoped(cancelled, args.workspace_id).status == "cancelled"
            store.relay(args.workspace_id, settings.rocketmq_event_topic, clients["relay"].publish, 10)
            wait_for(
                lambda: api.get("/api/v2/jobs/" + cancelled).json().get("status") == "cancelled",
                "cancellation event did not reach Control",
            )
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "job_id": job_id,
                        "checks": ["outbox/inbox", "snapshot", "S3 artifact", "projection", "memory", "cancel"],
                    }
                )
            )
    finally:
        for client in reversed(started):
            client.close()
        store.close()


if __name__ == "__main__":
    main()
