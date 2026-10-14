"""Shared frozen-input and publication boundaries for media production workflows."""

import asyncio
import json
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from agent_runtime.domain.content import ContentDocument
from agent_runtime.execution.context import current_execution
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore


def frozen_documents(command, bundle):
    requested = bundle["request"].get("input_version_ids", [])
    inputs = bundle.get("inputs", [])
    if [value["document_version_id"] for value in inputs] != requested:
        raise ValueError("frozen input version sequence conflict")
    sources = {}
    for item in [*inputs, *([bundle["source"]] if bundle.get("source") else [])]:
        document = ContentDocument.model_validate(item["document"])
        if document.project_id != command.project_id or document.schema_version != "1.0":
            raise ValueError("frozen input project or schema conflict")
        if item["document"].get("content_hash") != document.content_hash:
            raise ValueError("frozen input IR checksum conflict")
        identity = item["document_version_id"]
        if identity in sources and sources[identity].content_hash != document.content_hash:
            raise ValueError("conflicting frozen input documents")
        sources[identity] = document
    return sources


def production_snapshot(snapshot, *, workflow, skill, required_nodes):
    execution = current_execution()
    if snapshot.workflow != workflow or snapshot.schema_versions.get("content_ir") != "1.0":
        raise ValueError("unsupported production workflow")
    if snapshot.tools or snapshot.mcp_policies:
        raise ValueError("production workflows do not run dynamic tools or MCP")
    if any(ref != skill and execution.resource_versions.get(ref, {}).get("kind") != "skill" for ref in snapshot.skills):
        raise ValueError("unsupported production skill")
    if not set(required_nodes).issubset(execution.prompt_versions):
        raise ValueError("production requires frozen database prompts")


async def publish_document(command, bundle, snapshot, document):
    execution = current_execution()
    settings, job = execution.settings, execution.job
    reference = await asyncio.to_thread(
        S3ArtifactStore(settings).upload,
        command.workspace_id,
        command.project_id,
        "documents",
        document.document_id,
        "content.json",
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode(),
        version_id=str(uuid5(NAMESPACE_URL, f"content:{command.job_id}:{job.fencing_token}")),
        content_type="application/json",
    )
    version_id = str(uuid5(NAMESPACE_URL, f"content:{command.job_id}:{job.fencing_token}"))
    await asyncio.to_thread(
        execution.store.record_artifact,
        command.job_id,
        command.workspace_id,
        settings.worker_id,
        job.fencing_token,
        reference,
    )
    source = bundle.get("source")
    sources = frozen_documents(command, bundle)
    inputs = bundle["request"].get("input_version_ids", [])
    commit = {
        "document_id": document.document_id,
        "document_version_id": version_id,
        "source_version": document.version,
        "document_type": document.document_type,
        "title": bundle["request"]["title"],
        "workflow": snapshot.workflow,
        "source_version_id": source["document_version_id"] if source else None,
        "source_ir_hash": sources[source["document_version_id"]].content_hash if source else None,
        "input_version_ids": inputs,
        "input_ir_hashes": {identity: sources[identity].content_hash for identity in inputs},
        "artifact": asdict(reference),
        "ir_hash": document.content_hash,
    }
    return {"document_version_id": version_id, "artifact": asdict(reference), "content_commit": commit}
