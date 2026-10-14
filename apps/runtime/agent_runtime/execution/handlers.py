"""Real AI-backed creation and memory handlers registered by Runtime deployment."""

import asyncio
import hashlib
import json
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from agent_runtime.domain.capabilities import CapabilitySnapshot
from agent_runtime.domain.content import ContentDocument
from agent_runtime.domain.context import ContextAssembler, ContextInput
from agent_runtime.execution.bindings import bind_execution_models
from agent_runtime.execution.context import current_execution
from agent_runtime.execution.errors import WaitingReview
from agent_runtime.execution.snapshots import load_execution_snapshot
from agent_runtime.infrastructure.models.client import RuntimeModels
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.prompts.renderer import render_node
from agent_runtime.workflows.content.graph import create_content_graph, structured, validate_snapshot
from agent_runtime.workflows.novel.workflow import run_novel, validate_novel_snapshot


async def generate_content(command):
    execution = current_execution()
    settings, store, job = execution.settings, execution.store, execution.job
    bundle = await load_execution_snapshot(command, settings)
    snapshot = CapabilitySnapshot.model_validate(bundle["snapshot"])
    if snapshot.snapshot_hash != command.capability_snapshot_hash:
        raise ValueError("capability snapshot checksum conflict")
    request = bundle["request"]
    if request["project_id"] != command.project_id or bundle["job_id"] != command.job_id:
        raise ValueError("execution scope conflict")
    await bind_execution_models(
        snapshot,
        command.workspace_id,
        resolved_resources=bundle["resolved_resources"],
    )
    if request.get("runtime_operation"):
        from agent_runtime.execution.operations import execute_operation

        return await execute_operation(command, bundle)
    if snapshot.workflow == "novel.write@1":
        validate_novel_snapshot(snapshot)
        return await run_novel(command, bundle)
    if snapshot.workflow == "media.video@1":
        from agent_runtime.workflows.media.video import generate_video

        return await generate_video(command, bundle, snapshot)
    if snapshot.workflow in {"media.image@1", "media.speech@1", "media.music@1", "media.sound_effect@1"}:
        from agent_runtime.workflows.media.generated import generate_media_asset

        return await generate_media_asset(command, bundle, snapshot)
    if snapshot.workflow in {"media.storyboard@1", "media.timeline@1"}:
        from agent_runtime.workflows.media.planning import plan_production

        return await plan_production(command, bundle, snapshot)
    if snapshot.workflow == "media.compose@1":
        from agent_runtime.workflows.media.compose import compose_content

        return await compose_content(command, bundle, snapshot)
    if snapshot.workflow == "media.assemble@1":
        from agent_runtime.workflows.media.assemble import assemble_content

        return await assemble_content(command, bundle, snapshot)
    validate_snapshot(snapshot)
    if job.result.get("content_commit") and job.resume_input.get("action", "approve") == "approve":
        return {
            "document_version_id": job.result["content_commit"]["document_version_id"],
            "content_commit": job.result["content_commit"],
            "artifact": job.result["artifact"],
        }
    if job.resume_input.get("feedback"):
        request = {
            **request,
            "objective": request["objective"] + "\nRevision feedback: " + str(job.resume_input["feedback"]),
        }
    existing = bundle.get("committed_version")
    if existing:
        return {"document_version_id": existing["document_version_id"], "content_commit": existing}
    memories = await asyncio.to_thread(
        store.creation_context, job, settings.worker_id, request.get("required_memory_version_ids", [])
    )
    inputs = [ContextInput(kind="constraints", source="job.brief", content=request["objective"])]
    source = bundle.get("source")
    if source:
        from agent_runtime.domain.content_adapters import with_block_ids

        document = ContentDocument.model_validate(source["document"])
        document = with_block_ids(document).document
        inputs.append(
            ContextInput(
                kind="current",
                source=source["document_version_id"],
                content=json.dumps(document.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
            )
        )
    inputs.extend(ContextInput.model_validate(item) for item in memories)
    pack = ContextAssembler(settings.context_token_budget).assemble(inputs)
    required_sources = {"job.brief"} | ({source["document_version_id"]} if source else set())
    if not required_sources.issubset({block.source for block in pack.blocks}):
        raise ValueError("required source exceeds context budget; split content into smaller nodes")
    await asyncio.to_thread(store.save_execution_plan, job, settings.worker_id, snapshot, pack)
    models = RuntimeModels(settings)
    graph = create_content_graph(models.chat("creative"), models.chat("analysis"), snapshot, nodes=request.get("nodes"))
    state = await graph.ainvoke(
        {
            "objective": request["objective"],
            "document_type": request["document_type"],
            "document_id": request["document_id"],
            "project_id": command.project_id,
            "version": request["version"],
            "context": "\n\n".join(block.content for block in pack.blocks),
            "source_document": document.model_dump(mode="json") if source else None,
            "source_version_id": source["document_version_id"] if source else "",
        }
    )
    document = ContentDocument.model_validate(state["document"])
    version_id = str(uuid5(NAMESPACE_URL, f"content:{command.job_id}:{job.fencing_token}"))
    artifacts = S3ArtifactStore(settings)
    data = json.dumps(document.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode()
    reference = await asyncio.to_thread(
        artifacts.upload,
        command.workspace_id,
        command.project_id,
        "documents",
        document.document_id,
        "content.json",
        data,
        version_id=version_id,
        content_type="application/json",
    )
    await asyncio.to_thread(
        store.record_artifact, command.job_id, command.workspace_id, settings.worker_id, job.fencing_token, reference
    )
    committed = {
        "document_id": document.document_id,
        "document_version_id": version_id,
        "source_version": document.version,
        "document_type": document.document_type,
        "title": request["title"],
        "source_version_id": request.get("source_version_id"),
        "workflow": snapshot.workflow,
        "source_ir_hash": source["document"]["content_hash"] if source else None,
        "artifact": asdict(reference),
        "ir_hash": document.content_hash,
    }
    if not state["review"]["approved"]:
        return WaitingReview(artifact=asdict(reference), review=state["review"], content_commit=committed)
    return {"document_version_id": version_id, "artifact": asdict(reference), "content_commit": committed}


async def refresh_memory(command):
    execution = current_execution()
    settings = execution.settings
    source_job = command.payload.get("source_job_id")
    if not source_job or not command.payload.get("source_execution_snapshot"):
        raise ValueError("memory refresh requires frozen source execution snapshot")
    from agent_runtime.domain.protocol import ExecutionSnapshotReference

    reference = ExecutionSnapshotReference.model_validate(command.payload["source_execution_snapshot"])
    source_command = command.model_copy(update={"job_id": source_job, "execution_snapshot": reference})
    bundle = await load_execution_snapshot(source_command, settings)
    snapshot = CapabilitySnapshot.model_validate(bundle["snapshot"])
    await bind_execution_models(snapshot, command.workspace_id, resolved_resources=bundle["resolved_resources"])
    artifact = command.payload["artifact"]
    expected_prefix = f"workspaces/{command.workspace_id}/projects/{command.project_id}/"
    if not artifact["object_key"].startswith(expected_prefix):
        raise ValueError("memory source object scope conflict")
    data = await asyncio.to_thread(
        S3ArtifactStore(settings).download_key,
        artifact["object_key"],
        expected_sha256=artifact["sha256"],
        expected_size_bytes=artifact["size_bytes"],
    )
    document = ContentDocument.model_validate_json(data)
    if document.project_id != command.project_id:
        raise ValueError("memory source project conflict")
    text = "\n".join(block.text for block in document.blocks)
    if len(text) > settings.artifact_max_bytes:
        raise ValueError("memory source is too large; split the content")
    models = RuntimeModels(settings)
    prompt = render_node("memory.refresh", {"source_text": text})

    def check(value):
        if not isinstance(value.get("summary"), str) or not value["summary"].strip():
            raise ValueError("memory summary must be non-empty")
        if not isinstance(value.get("facts"), list) or len(value["facts"]) > 50:
            raise ValueError("invalid facts")
        for fact in value["facts"]:
            if not all(isinstance(fact.get(key), str) and fact[key].strip() for key in ["kind", "subject", "value"]):
                raise ValueError("invalid factual memory")

    digest = await structured(models.chat("analysis"), prompt, check, "memory")
    from agent_runtime.domain.canon import CanonFact, CanonState, Evidence, commit_facts
    from agent_runtime.execution.policies import current_creation_policy, policy_provenance

    shared_canon = None
    if policy := current_creation_policy():
        version = command.payload["document_version_id"]
        facts = [
            CanonFact(
                id=f"{version}:{index}",
                kind=(fact["kind"] if fact["kind"] in policy.canon.fact_types else "other"),
                subject=fact["subject"],
                value=fact["value"],
                evidence=[Evidence(source_id=version, source_version=version)],
            )
            for index, fact in enumerate(digest["facts"])
        ]
        shared_canon = commit_facts(
            CanonState(workspace_id=command.workspace_id, project_id=command.project_id),
            facts,
            policy.canon,
            source_id=version,
            source_version=version,
        ).model_dump(mode="json")
    nodes = [{"memory_type": "summary", "content": digest["summary"]}]
    nodes.extend(
        {"memory_type": "fact", "content": fact["subject"] + ": " + fact["value"], "fact": fact}
        for fact in digest["facts"]
    )
    vectors = await models.embed([node["content"] for node in nodes])
    if len(vectors) != len(nodes):
        raise ValueError("embedding count mismatch")
    for node, vector in zip(nodes, vectors, strict=True):
        node["embedding"] = vector
        node["metadata"] = {**models.embedding_identity(), "index_version": "memory@1"}
    return {
        "nodes": nodes,
        "shared_canon": shared_canon,
        **policy_provenance(),
        "embedding_model": execution.model_routes.get("embedding", {}).get("model_name", settings.embedding_model),
        "index_version": "memory@1",
        "source_hash": hashlib.sha256(text.encode()).hexdigest(),
    }
