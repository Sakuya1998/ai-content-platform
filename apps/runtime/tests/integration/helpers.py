"""Public Control submissions and immutable MQ snapshot acceptance helpers."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

from agent_runtime.domain.protocol import RuntimeCommand
from agent_runtime.execution.snapshots import load_execution_snapshot
from agent_runtime.execution.worker import DurableWorker


def submit_generation(api, body, *, headers=None):
    workflow = "content.adapt" if body.get("source_version_id") else "content.generate"
    if body.get("document_type") in {"video", "image", "storyboard", "timeline"}:
        workflow = "media." + body["document_type"]
    elif body.get("document_type") == "audio":
        workflow = "media.speech"
    if body.get("operation") in {"compose", "assemble"}:
        workflow = "media." + body["operation"]
    response = api.post(
        "/api/v2/jobs",
        headers=headers,
        json={"workflow": workflow, "workflow_version": 1, **body},
    )
    assert response.status_code == 202, response.text
    return SimpleNamespace(job_id=response.json()["job_id"])


def content_version(api, version_id, project_id, *, headers=None):
    return api.get("/api/v2/content/versions/" + version_id, headers=headers, params={"project_id": project_id})


def inspect_snapshot(job, settings):
    snapshot = asyncio.run(load_execution_snapshot(job.command, settings))
    assert job.command.payload == {}
    assert snapshot["job_id"] == job.command.job_id
    assert snapshot["workspace_id"] == job.command.workspace_id
    assert snapshot["project_id"] == job.command.project_id
    frozen_workflow = snapshot["snapshot"]["workflow_definition"]
    assert frozen_workflow is not None
    assert frozen_workflow["definition"]["template"] == snapshot["snapshot"]["workflow"]
    assert frozen_workflow["version_id"] and frozen_workflow["content_hash"]
    return snapshot


@contextmanager
def forbid_control_business_http(control_url):
    origin = urlsplit(control_url)
    original_sync, original_async = httpx.Client.send, httpx.AsyncClient.send

    def check(request):
        target = urlsplit(str(request.url))
        if (target.hostname, target.port) == (origin.hostname, origin.port):
            raise AssertionError("Runtime attempted an HTTP request to Control: " + target.path)

    def send(client, request, *args, **kwargs):
        check(request)
        return original_sync(client, request, *args, **kwargs)

    async def asend(client, request, *args, **kwargs):
        check(request)
        return await original_async(client, request, *args, **kwargs)

    with patch.object(httpx.Client, "send", send), patch.object(httpx.AsyncClient, "send", asend):
        yield


def run_worker(store, settings, handler, job, control_url):
    with forbid_control_business_http(control_url):
        asyncio.run(DurableWorker(store, settings, handler).execute(job))


def receive_task(client, store, submission, workspace, settings):
    client.receive(lambda body: store.accept_task(RuntimeCommand.model_validate_json(body)))
    try:
        job = store.get_scoped(submission.job_id, workspace)
    except KeyError:
        return None
    inspect_snapshot(job, settings)
    return job
