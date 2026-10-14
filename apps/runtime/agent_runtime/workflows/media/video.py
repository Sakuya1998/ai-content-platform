"""Video planning, durable provider execution and immutable asset publication."""

import asyncio
import json
import time
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

import httpx

from agent_runtime.execution.context import current_execution
from agent_runtime.execution.errors import JobCancelled, WaitingReview


async def execute_video_task(store, job, worker_id, provider, settings, *, resume_input=None):
    """Resume an existing task; a committed submitting marker prevents double billing."""
    task = await asyncio.to_thread(store.get, job, worker_id)
    if task is None:
        raise ValueError("video plan must be persisted before execution")
    if task.state == "submission_unknown" and (resume_input or {}).get("action") == "reconcile":
        from agent_runtime.infrastructure.media.errors import VideoProviderError

        remote_id = resume_input.get("remote_task_id")
        if not isinstance(remote_id, str) or not remote_id.strip() or len(remote_id) > 200:
            return WaitingReview(
                media_status="submission_unknown",
                review={"approved": False, "notes": ["Reconciliation requires a confirmed remote task ID."]},
            )
        # The operator confirms ownership; the adapter checks task/model identity.
        try:
            status = await provider.status(remote_id)
        except httpx.HTTPError, TimeoutError, VideoProviderError:
            return WaitingReview(
                media_status="submission_unknown",
                review={
                    "approved": False,
                    "notes": ["Remote task could not be verified; confirm its ID and resume again."],
                },
            )
        await asyncio.to_thread(store.submitted, job, worker_id, remote_id)
        task = await asyncio.to_thread(store.update_status, job, worker_id, status)
    if task.state in {"submitting", "submission_unknown"}:
        return WaitingReview(
            media_status="submission_unknown",
            review={
                "approved": False,
                "notes": [
                    "Provider submission cannot be confirmed; reconcile with the provider before creating a new job."
                ],
            },
        )
    if task.state == "prepared":
        if task.review.get("approved") is not True:
            return WaitingReview(media_status="prepared", review=task.review)
        await asyncio.to_thread(store.begin_submit, job, worker_id)
        try:
            remote_id = await provider.submit(task.request)
        except asyncio.CancelledError:
            # Shutdown or lease loss must not cause a second POST on recovery.
            await asyncio.to_thread(store.mark_unknown, job, worker_id)
            raise
        except Exception as exc:
            from agent_runtime.domain.media import VideoTaskStatus
            from agent_runtime.infrastructure.media.errors import SubmissionUnknown, VideoProviderError

            if isinstance(exc, VideoProviderError) and not isinstance(exc, SubmissionUnknown):
                await asyncio.to_thread(
                    store.update_status,
                    job,
                    worker_id,
                    VideoTaskStatus(state="failed", error={"code": "provider_rejected"}),
                )
                raise ValueError("video provider rejected submission") from None
            await asyncio.to_thread(store.mark_unknown, job, worker_id)
            return WaitingReview(
                media_status="submission_unknown",
                review={
                    "approved": False,
                    "notes": ["Provider submission cannot be confirmed; automatic resubmission is disabled."],
                },
            )
        task = await asyncio.to_thread(store.submitted, job, worker_id, remote_id)
    deadline = time.monotonic() + settings.media_poll_timeout_seconds
    try:
        while task.state in {"running", "cancel_pending"}:
            if task.state == "cancel_pending":
                await cancel_remote_video(store, job, worker_id, provider, task.remote_task_id)
                raise JobCancelled()
            from agent_runtime.infrastructure.media.errors import VideoProviderError

            try:
                status = await provider.status(task.remote_task_id)
            except httpx.TransportError, TimeoutError, VideoProviderError:
                status = None
            if status is not None:
                task = await asyncio.to_thread(store.update_status, job, worker_id, status)
            if task.state == "cancel_pending":
                continue
            if task.state != "running":
                break
            if time.monotonic() >= deadline:
                return WaitingReview(
                    media_status="running",
                    remote_task_id=task.remote_task_id,
                    review={
                        "approved": False,
                        "notes": ["Video is still running; resume this job to continue polling."],
                    },
                )
            await asyncio.sleep(settings.media_poll_seconds)
    except asyncio.CancelledError:
        execution = current_execution(required=False)
        if execution and execution.cancel_requested:
            # Keep cancellation pending when the model cannot cancel or DELETE fails.
            await asyncio.to_thread(store.begin_cancel, job, worker_id)
            await cancel_remote_video(store, job, worker_id, provider, task.remote_task_id)
        raise
    if task.state != "succeeded":
        raise ValueError("video task ended in " + task.state)
    return task


async def cancel_remote_video(store, job, worker_id, provider, remote_id):
    if provider.route.video_capabilities.cancellation:
        try:
            await provider.cancel(remote_id)
        except Exception:
            return
        await asyncio.to_thread(store.mark_cancelled, job, worker_id)


async def download_video(url, max_bytes, *, client=None):
    """Download provider output without API credentials, enforcing the artifact limit."""
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.username or parsed.password:
        raise ValueError("invalid video download URL")
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=120, follow_redirects=True)
    try:
        data = bytearray()
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            if int(response.headers.get("content-length", 0)) > max_bytes:
                raise ValueError("video exceeds artifact size limit")
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > max_bytes:
                    raise ValueError("video exceeds artifact size limit")
                data.extend(chunk)
        if not data:
            raise ValueError("video output is empty")
        return bytes(data)
    finally:
        if own_client:
            await client.aclose()


def validate_video_snapshot(snapshot, execution):
    if snapshot.workflow != "media.video@1" or snapshot.schema_versions.get("content_ir") != "1.0":
        raise ValueError("unsupported video workflow or IR version")
    if snapshot.tools or snapshot.mcp_policies:
        raise ValueError("video workflow does not execute tools or MCP")
    if any(
        skill != "controlled-video@1" and execution.resource_versions.get(skill, {}).get("kind") != "skill"
        for skill in snapshot.skills
    ):
        raise ValueError("unsupported video skill")
    if not {"media.plan", "media.review", "memory.refresh"}.issubset(execution.prompt_versions):
        raise ValueError("video workflow requires frozen database prompts")
    if "video_generation" not in execution.model_routes:
        raise ValueError("video model route is not configured")


async def generate_video(command, bundle, snapshot):
    """Create one model-native clip, preserving its source IR and provider provenance."""
    from agent_runtime.domain.content import ContentBlock, ContentDocument
    from agent_runtime.domain.context import ContextAssembler, ContextInput
    from agent_runtime.domain.media import VideoRequest, VideoRoute
    from agent_runtime.execution.policies import current_creation_policy, policy_provenance
    from agent_runtime.infrastructure.media.registry import create_video_provider, video_provider_factory
    from agent_runtime.infrastructure.models.client import RuntimeModels
    from agent_runtime.infrastructure.postgres.media import PostgresMediaTaskStore
    from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
    from agent_runtime.prompts.renderer import render_node
    from agent_runtime.workflows.content.graph import structured

    execution = current_execution()
    validate_video_snapshot(snapshot, execution)
    settings, job = execution.settings, execution.job
    request = bundle["request"]
    if request.get("nodes") not in (None, [], ["plan", "generate", "review"]):
        raise ValueError("unsupported video node composition")
    if job.result.get("content_commit") and job.resume_input.get("action", "approve") == "approve":
        return {
            "document_version_id": job.result["content_commit"]["document_version_id"],
            "content_commit": job.result["content_commit"],
            "artifact": job.result["artifact"],
        }
    if existing := bundle.get("committed_version"):
        return {"document_version_id": existing["document_version_id"], "content_commit": existing}
    route = VideoRoute.model_validate(execution.model_routes["video_generation"])
    video_provider_factory(route.provider)
    store = PostgresMediaTaskStore(execution.store)
    task = await asyncio.to_thread(store.get, job, settings.worker_id)
    source = bundle.get("source")
    source_document = ContentDocument.model_validate(source["document"]) if source else None
    if task is None:
        from agent_runtime.domain.constraints import format_constraints

        memories = await asyncio.to_thread(
            execution.store.creation_context, job, settings.worker_id, request.get("required_memory_version_ids", [])
        )
        inputs = [ContextInput(kind="constraints", source="job.brief", content=request["objective"])]
        if source:
            inputs.append(
                ContextInput(
                    kind="current",
                    source=source["document_version_id"],
                    content=json.dumps(source["document"], ensure_ascii=False, sort_keys=True),
                )
            )
        inputs.extend(ContextInput.model_validate(item) for item in memories)
        pack = ContextAssembler(settings.context_token_budget).assemble(inputs)
        required = {"job.brief"} | ({source["document_version_id"]} if source else set())
        if not required.issubset({block.source for block in pack.blocks}):
            raise ValueError("video source exceeds context budget; split into clips")
        await asyncio.to_thread(execution.store.save_execution_plan, job, settings.worker_id, snapshot, pack)
        context = "\n\n".join(block.content for block in pack.blocks)
        if policy := current_creation_policy():
            context += "\n" + format_constraints(
                {"objective": request["objective"], "document_type": "video"}, policy.constraints
            )
        parameters = request.get("arguments", {}).get("video", {})
        # Validate declared capabilities before spending even a planning model call.
        route.validate_request(VideoRequest(prompt=request["objective"], parameters=parameters))
        model = RuntimeModels(settings).chat("analysis")
        prompt = render_node(
            "media.plan", {"objective": request["objective"], "context": context, "parameters": parameters}
        )

        def check(value):
            if not isinstance(value.get("prompt"), str) or not value["prompt"].strip():
                raise ValueError("video plan requires a non-empty prompt")

        plan = await structured(model, prompt, check, "media.plan")
        video_request = VideoRequest(prompt=plan["prompt"], parameters=parameters)
        route.validate_request(video_request)
        review = await structured(
            model,
            render_node(
                "media.review", {"objective": request["objective"] + "\n" + context, "draft": video_request.prompt}
            ),
            lambda value: check_review(value),
            "media.review",
        )
        task = await asyncio.to_thread(
            store.reserve, job, settings.worker_id, video_request, route.identity(), review=review
        )
        if not review["approved"]:
            return WaitingReview(media_status="prepared", review=review)
    elif task.route_identity != route.identity():
        raise ValueError("persisted video route conflicts with frozen task")
    if task.state == "prepared" and task.review.get("approved") is not True:
        if job.resume_input.get("action") == "approve":
            task = await asyncio.to_thread(store.approve_plan, job, settings.worker_id)
        else:
            return WaitingReview(media_status="prepared", review=task.review)
    provider = create_video_provider(route)
    try:
        task = await execute_video_task(
            store, job, settings.worker_id, provider, settings, resume_input=job.resume_input
        )
    finally:
        await provider.aclose()
    if isinstance(task, WaitingReview):
        return task
    data = await download_video(task.content_url, settings.artifact_max_bytes)
    artifacts = S3ArtifactStore(settings)
    asset_version = str(uuid5(NAMESPACE_URL, f"video-asset:{command.job_id}"))
    asset = await asyncio.to_thread(
        artifacts.upload,
        command.workspace_id,
        command.project_id,
        "assets",
        request["document_id"],
        "video.mp4",
        data,
        version_id=asset_version,
        content_type="video/mp4",
    )
    await asyncio.to_thread(
        execution.store.record_artifact,
        command.job_id,
        command.workspace_id,
        settings.worker_id,
        job.fencing_token,
        asset,
    )
    metadata = {
        "asset": asdict(asset),
        "provider": route.provider,
        "model_name": route.model_name,
        "profile_version_id": route.profile_version_id,
        "remote_task_id": task.remote_task_id,
        "parameters": task.request.parameters,
        "usage": task.usage,
        **policy_provenance(),
    }
    if source_document:
        from agent_runtime.domain.content_adapters import with_block_ids

        metadata.update(
            source_version_id=source["document_version_id"],
            source_block_ids=[block.metadata["block_id"] for block in with_block_ids(source_document).document.blocks],
        )
    document = ContentDocument(
        document_id=request["document_id"],
        project_id=command.project_id,
        document_type="video",
        version=request["version"],
        schema_version="1.0",
        blocks=[ContentBlock(type="video", text=task.request.prompt, metadata=metadata)],
        metadata={"workflow": snapshot.workflow, "review_scope": "prompt_before_submission"},
    )
    version_id = str(uuid5(NAMESPACE_URL, f"content:{command.job_id}:{job.fencing_token}"))
    reference = await asyncio.to_thread(
        artifacts.upload,
        command.workspace_id,
        command.project_id,
        "documents",
        document.document_id,
        "content.json",
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode(),
        version_id=version_id,
        content_type="application/json",
    )
    await asyncio.to_thread(
        execution.store.record_artifact,
        command.job_id,
        command.workspace_id,
        settings.worker_id,
        job.fencing_token,
        reference,
    )
    commit = {
        "document_id": document.document_id,
        "document_version_id": version_id,
        "source_version": document.version,
        "document_type": "video",
        "title": request["title"],
        "source_version_id": request.get("source_version_id"),
        "workflow": snapshot.workflow,
        "source_ir_hash": source_document.content_hash if source_document else None,
        "artifact": asdict(reference),
        "ir_hash": document.content_hash,
    }
    return {
        "document_version_id": version_id,
        "artifact": asdict(reference),
        "content_commit": commit,
        "video_asset": asdict(asset),
    }


def check_review(value):
    if not isinstance(value.get("approved"), bool) or not isinstance(value.get("notes"), list):
        raise ValueError("media review requires approved and notes")
    if any(not isinstance(note, str) for note in value["notes"]):
        raise ValueError("media review notes must be strings")
