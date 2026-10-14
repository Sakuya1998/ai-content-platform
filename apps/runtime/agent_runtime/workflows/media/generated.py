"""Controlled image and speech workflows with durable synchronous-call recovery."""

import asyncio
import hashlib
import json
import re
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from botocore.exceptions import BotoCoreError, ClientError
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.domain.generated_media import MediaArtifact, StoredMediaRequest
from agent_runtime.execution.context import current_execution
from agent_runtime.execution.errors import WaitingReview
from agent_runtime.execution.leases import FencingTokenError
from agent_runtime.infrastructure.media.errors import VideoProviderError
from agent_runtime.infrastructure.postgres.generated_media import PostgresGeneratedMediaStore
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.workflows.media.video import check_review, download_video


async def execute_generated_media(store, job, worker_id, create, recover):
    """Run once; recover an archived receipt before considering another action."""
    task = await asyncio.to_thread(store.get, job, worker_id)
    if task is None:
        raise ValueError("media plan must be persisted")
    if task.state == "outputs_ready":
        return task
    if task.state in {"submitting", "submission_unknown"}:
        try:
            receipt = await recover(task)
        except FencingTokenError:
            raise
        except Exception:
            receipt = None
        if receipt:
            return await confirm_archive(store, job, worker_id, receipt)
        return WaitingReview(
            media_status="submission_unknown",
            review={
                "approved": False,
                "notes": [
                    "Synchronous provider outcome is unknown; verify provider records and artifacts. "
                    "Automatic replay is disabled."
                ],
            },
        )
    if task.state == "prepared" and task.review.get("approved") is not True:
        return WaitingReview(media_status="prepared", review=task.review)
    if task.state != "prepared":
        raise ValueError("media task ended in " + task.state)
    await asyncio.to_thread(store.begin_submit, job, worker_id)
    try:
        receipt = await create(task)
    except asyncio.CancelledError:
        await preserve_unknown(store, job, worker_id)
        raise
    except FencingTokenError:
        raise
    except VideoProviderError:
        await asyncio.to_thread(store.rejected, job, worker_id)
        raise ValueError("media provider rejected request") from None
    except Exception:
        await preserve_unknown(store, job, worker_id)
        return WaitingReview(
            media_status="submission_unknown",
            review={
                "approved": False,
                "notes": [
                    "Provider response or archival could not be confirmed; resume to check the archived receipt."
                ],
            },
        )
    return await confirm_archive(store, job, worker_id, receipt)


async def preserve_unknown(store, job, worker_id):
    try:
        await asyncio.to_thread(store.mark_unknown, job, worker_id)
    except FencingTokenError:
        raise
    except Exception:
        # The committed submitting marker remains authoritative on recovery.
        pass


async def confirm_archive(store, job, worker_id, receipt):
    try:
        return await asyncio.to_thread(
            store.archived,
            job,
            worker_id,
            receipt["outputs"],
            receipt["usage"],
            vendor_request_id=receipt.get("vendor_request_id"),
        )
    except FencingTokenError:
        raise
    except Exception:
        return WaitingReview(
            media_status="archive_pending",
            review={
                "approved": False,
                "notes": ["Assets are archived; resume this job to confirm the receipt and finish publication."],
            },
        )


def validate_asset_scope(value, workspace_id, project_id, bucket, max_bytes):
    asset = MediaArtifact.model_validate(value)
    prefix = f"workspaces/{workspace_id}/projects/{project_id}/"
    suffix = asset.object_key.removeprefix(prefix)
    if (
        asset.bucket != bucket
        or not asset.object_key.startswith(prefix)
        or not suffix.startswith(("assets/", "uploads/"))
        or ".." in asset.object_key.split("/")
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", asset.object_key)
        or asset.size_bytes > max_bytes
    ):
        raise ValueError("media asset scope or size conflict")
    return asset


def read_asset(artifacts, value, workspace_id, project_id, max_bytes):
    asset = validate_asset_scope(value, workspace_id, project_id, artifacts.bucket, max_bytes)
    response = artifacts.client.get_object(Bucket=asset.bucket, Key=asset.object_key)
    try:
        if response.get("ContentLength", 0) > max_bytes:
            raise ValueError("media asset exceeds size limit")
        data = response["Body"].read(max_bytes + 1)
    finally:
        response["Body"].close()
    if len(data) > max_bytes or len(data) != asset.size_bytes or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise ValueError("media asset checksum or size conflict")
    return data


def validate_generated_snapshot(snapshot, execution, modality):
    contract = {
        "image": ("media.image@1", "media.image.plan", "controlled-image@1", "image_generation"),
        "audio": ("media.speech@1", "media.speech.plan", "controlled-speech@1", "speech_synthesis"),
        "music": ("media.music@1", "media.music.plan", "controlled-music@1", "music_generation"),
        "effect": (
            "media.sound_effect@1",
            "media.sound_effect.plan",
            "controlled-sound-effect@1",
            "sound_effect_generation",
        ),
    }
    try:
        workflow, plan_node, skill, purpose = contract[modality]
    except KeyError:
        raise ValueError("unsupported generated-media modality") from None
    if snapshot.workflow != workflow or snapshot.schema_versions.get("content_ir") != "1.0":
        raise ValueError("unsupported generated-media workflow")
    if snapshot.tools or snapshot.mcp_policies:
        raise ValueError("generated-media workflows do not execute tools or MCP")
    if any(ref != skill and execution.resource_versions.get(ref, {}).get("kind") != "skill" for ref in snapshot.skills):
        raise ValueError("unsupported generated-media skill")
    if not {plan_node, "media.review", "memory.refresh"}.issubset(execution.prompt_versions):
        raise ValueError("generated-media requires frozen prompts")
    if purpose not in execution.model_routes:
        raise ValueError("generated-media route is not configured")
    return purpose, plan_node


async def generate_media_asset(command, bundle, snapshot):
    from agent_runtime.domain.audio_generation import MusicRoute, SoundEffectRoute
    from agent_runtime.domain.images import ImageRoute
    from agent_runtime.domain.speech import SpeechRoute
    from agent_runtime.infrastructure.media.audio_generation import create_music_provider, create_sound_effect_provider
    from agent_runtime.infrastructure.media.images import create_image_provider
    from agent_runtime.infrastructure.media.speech import create_speech_provider

    execution = current_execution()
    modality = {
        "media.image@1": "image",
        "media.speech@1": "audio",
        "media.music@1": "music",
        "media.sound_effect@1": "effect",
    }.get(snapshot.workflow)
    if modality is None:
        raise ValueError("unsupported generated-media workflow")
    purpose, plan_node = validate_generated_snapshot(snapshot, execution, modality)
    route_type, create_provider = {
        "image": (ImageRoute, create_image_provider),
        "audio": (SpeechRoute, create_speech_provider),
        "music": (MusicRoute, create_music_provider),
        "effect": (SoundEffectRoute, create_sound_effect_provider),
    }[modality]
    route = route_type.model_validate(execution.model_routes[purpose])
    # Construction validates protocol availability and provider-specific configuration.
    provider = create_provider(route)
    try:
        return await _generate_asset(command, bundle, snapshot, modality, plan_node, route, provider)
    except FencingTokenError:
        raise
    except BotoCoreError, ClientError, OperationalError, PoolTimeout, ConnectionError, TimeoutError:
        return WaitingReview(
            media_status="infrastructure_unavailable",
            review={
                "approved": False,
                "notes": [
                    "Resume this job when storage and database are available; "
                    "its persisted media checkpoint will be reused."
                ],
            },
        )
    finally:
        await provider.aclose()


async def _generate_asset(command, bundle, snapshot, modality, plan_node, route, provider):
    from agent_runtime.domain.constraints import format_constraints
    from agent_runtime.domain.content_adapters import with_block_ids
    from agent_runtime.domain.context import ContextAssembler, ContextInput
    from agent_runtime.domain.images import ImageRequest, validate_image_bytes
    from agent_runtime.domain.speech import SpeechRequest
    from agent_runtime.execution.policies import current_creation_policy, policy_provenance
    from agent_runtime.infrastructure.models.client import RuntimeModels
    from agent_runtime.prompts.renderer import render_node
    from agent_runtime.workflows.content.graph import structured

    execution = current_execution()
    settings, job, request = execution.settings, execution.job, bundle["request"]
    if request.get("nodes") not in (None, [], ["plan", "generate", "review"]):
        raise ValueError("unsupported generated-media node composition")
    if existing := bundle.get("committed_version"):
        return {"document_version_id": existing["document_version_id"], "content_commit": existing}
    store = PostgresGeneratedMediaStore(execution.store)
    artifacts = S3ArtifactStore(settings)
    task = await asyncio.to_thread(store.get, job, settings.worker_id)
    source = bundle.get("source")
    source_document = ContentDocument.model_validate(source["document"]) if source else None
    source_version = source["document_version_id"] if source else ""
    source_hash = source_document.content_hash if source_document else ""
    source_image = None
    if task is None:
        parameters = dict(request.get("arguments", {}).get(modality, {}))
        literal_text = parameters.pop("text", None) if modality == "audio" else None
        if modality == "image":
            cap = route.image_capabilities
            parameters.setdefault("operation", "generate")
            parameters.setdefault("format", cap.formats[0])
            parameters.setdefault("n", 1)
            if cap.sizes:
                parameters.setdefault("size", cap.sizes[0])
            provider.validate_request(ImageRequest(prompt=request["objective"], parameters=parameters))
            if parameters["operation"] == "edit":
                if not source_document:
                    raise ValueError("image edit requires a frozen source document")
                source_image = next(
                    (
                        block.metadata.get("asset")
                        for block in source_document.blocks
                        if block.type == "image" and block.metadata.get("asset")
                    ),
                    None,
                )
                if source_image is None:
                    raise ValueError("image edit source has no image asset")
                if source_image.get("content_type") not in {"image/png", "image/jpeg", "image/webp"}:
                    raise ValueError("image edit source has an unsupported content type")
                validate_asset_scope(
                    source_image,
                    command.workspace_id,
                    command.project_id,
                    artifacts.bucket,
                    settings.artifact_max_bytes,
                )
        elif modality == "audio":
            cap = route.speech_capabilities
            parameters.setdefault("voice", cap.voices[0])
            parameters.setdefault("format", cap.formats[0])
            parameters.setdefault("speed", max(cap.min_speed, min(1.0, cap.max_speed)))
            route.validate_request(SpeechRequest(text=literal_text or "validation", parameters=parameters))
            if parameters.get("scene_id") or parameters.get("character_id"):
                if not source_document:
                    raise ValueError("speech links require a frozen source document")
                normalized = with_block_ids(source_document).document
                scenes = {b.metadata["block_id"] for b in normalized.blocks if b.type == "scene"}
                characters = {b.metadata.get("character_id") for b in normalized.blocks if b.type == "character"}
                if parameters.get("scene_id") and parameters["scene_id"] not in scenes:
                    raise ValueError("speech scene reference conflict")
                if parameters.get("character_id") and parameters["character_id"] not in characters:
                    raise ValueError("speech character reference conflict")
        elif modality == "music":
            from agent_runtime.domain.audio_generation import MusicRequest

            parameters.setdefault("output_format", route.music_capabilities.output_formats[0])
            parameters.setdefault("force_instrumental", route.music_capabilities.instrumental)
            route.validate_request(MusicRequest(prompt="validation", parameters=parameters))
        else:
            from agent_runtime.domain.audio_generation import SoundEffectRequest

            parameters.setdefault("output_format", route.sound_effect_capabilities.output_formats[0])
            parameters.setdefault("prompt_influence", 0.3)
            route.validate_request(SoundEffectRequest(text="validation", parameters=parameters))
        memories = await asyncio.to_thread(
            execution.store.creation_context, job, settings.worker_id, request.get("required_memory_version_ids", [])
        )
        inputs = [ContextInput(kind="constraints", source="job.brief", content=request["objective"])]
        if source:
            inputs.append(
                ContextInput(
                    kind="current",
                    source=source_version,
                    content=json.dumps(source["document"], ensure_ascii=False, sort_keys=True),
                )
            )
        inputs.extend(ContextInput.model_validate(value) for value in memories)
        pack = ContextAssembler(settings.context_token_budget).assemble(inputs)
        required = {"job.brief"} | ({source_version} if source else set())
        if not required.issubset({b.source for b in pack.blocks}):
            raise ValueError("media source exceeds context budget; split into smaller units")
        await asyncio.to_thread(execution.store.save_execution_plan, job, settings.worker_id, snapshot, pack)
        context = "\n\n".join(b.content for b in pack.blocks)
        if policy := current_creation_policy():
            context += "\n" + format_constraints(
                {"objective": request["objective"], "document_type": "image" if modality == "image" else "audio"},
                policy.constraints,
            )
        models = RuntimeModels(settings)
        key = "prompt" if modality in {"image", "music"} else "text"

        def check_plan(value):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise ValueError("media plan must be non-empty")
            if modality == "image":
                provider.validate_request(ImageRequest(prompt=value[key], parameters=parameters))
            elif modality == "audio":
                route.validate_request(SpeechRequest(text=value[key], parameters=parameters))
            elif modality == "music":
                from agent_runtime.domain.audio_generation import MusicRequest

                route.validate_request(MusicRequest(prompt=value[key], parameters=parameters))
            else:
                from agent_runtime.domain.audio_generation import SoundEffectRequest

                route.validate_request(SoundEffectRequest(text=value[key], parameters=parameters))

        if literal_text is not None:
            plan = {key: literal_text}
            check_plan(plan)
        else:
            plan = await structured(
                models.chat("analysis"),
                render_node(
                    plan_node, {"objective": request["objective"], "context": context, "parameters": parameters}
                ),
                check_plan,
                plan_node,
            )
        review = await structured(
            models.chat("analysis"),
            render_node("media.review", {"objective": request["objective"] + "\n" + context, "draft": plan[key]}),
            check_review,
            "media.review",
        )
        stored = StoredMediaRequest(
            prompt=plan[key], parameters=parameters, source_version_id=source_version, source_ir_hash=source_hash
        )
        task = await asyncio.to_thread(
            store.reserve, job, settings.worker_id, modality, stored, route.identity(), review=review
        )
        if not review["approved"]:
            return WaitingReview(media_status="prepared", review=review)
    if (
        task.route_identity != route.identity()
        or task.request.source_version_id != source_version
        or task.request.source_ir_hash != source_hash
    ):
        raise ValueError("persisted media source or route conflicts with frozen task")
    if (
        task.state == "prepared"
        and task.review.get("approved") is not True
        and job.resume_input.get("action") == "approve"
    ):
        task = await asyncio.to_thread(store.approve_plan, job, settings.worker_id)
    asset_version = str(uuid5(NAMESPACE_URL, f"generated-assets:{command.job_id}"))
    source_data, source_content_type = None, "image/png"
    if modality == "image" and task.state == "prepared":
        provider.validate_request(ImageRequest(prompt=task.request.prompt, parameters=task.request.parameters))
        if task.request.parameters.get("operation") == "edit":
            value = next((b.metadata.get("asset") for b in source_document.blocks if b.type == "image"), None)
            source_data = await asyncio.to_thread(
                read_asset, artifacts, value, command.workspace_id, command.project_id, settings.artifact_max_bytes
            )
            source_content_type = value["content_type"]
            validate_image_bytes(source_data, source_content_type)

    async def recover(current):
        try:
            data = await asyncio.to_thread(
                artifacts.download,
                command.workspace_id,
                command.project_id,
                "assets",
                request["document_id"],
                "receipt.json",
                version_id=asset_version,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise
        if len(data) > 65536:
            raise ValueError("media receipt exceeds size limit")
        receipt = json.loads(data)
        if receipt["request_hash"] != current.request_hash or receipt["route_identity"] != route.identity():
            raise ValueError("media receipt identity conflict")
        for asset in receipt["outputs"]:
            await asyncio.to_thread(
                read_asset, artifacts, asset, command.workspace_id, command.project_id, settings.artifact_max_bytes
            )
        return receipt

    async def create(current):
        payload = current.request
        if modality == "image":
            result = await provider.generate(
                ImageRequest(prompt=payload.prompt, parameters=payload.parameters),
                source_image=source_data,
                source_content_type=source_content_type,
                max_bytes=settings.artifact_max_bytes,
            )
            outputs, used = [], 0
            for item in result.images:
                data = (
                    item.data
                    if item.data is not None
                    else await download_video(item.url, settings.artifact_max_bytes - used)
                )
                used += len(data)
                if used > settings.artifact_max_bytes:
                    raise ValueError("image outputs exceed total artifact size limit")
                validate_image_bytes(data, item.content_type)
                outputs.append((data, item.content_type, item.extension))
        elif modality == "audio":
            result = await provider.synthesize(
                SpeechRequest(text=payload.prompt, parameters=payload.parameters), max_bytes=settings.artifact_max_bytes
            )
            outputs = [(result.data, result.content_type, result.extension)]
        elif modality == "music":
            from agent_runtime.domain.audio_generation import MusicRequest

            result = await provider.compose(
                MusicRequest(prompt=payload.prompt, parameters=payload.parameters),
                max_bytes=settings.artifact_max_bytes,
            )
            outputs = [(result.data, result.content_type, result.extension)]
        else:
            from agent_runtime.domain.audio_generation import SoundEffectRequest

            result = await provider.generate(
                SoundEffectRequest(text=payload.prompt, parameters=payload.parameters),
                max_bytes=settings.artifact_max_bytes,
            )
            outputs = [(result.data, result.content_type, result.extension)]
        references = []
        for index, (data, content_type, extension) in enumerate(outputs):
            reference = await asyncio.to_thread(
                artifacts.upload,
                command.workspace_id,
                command.project_id,
                "assets",
                request["document_id"],
                f"{modality}-{index}.{extension}",
                data,
                version_id=asset_version,
                content_type=content_type,
            )
            references.append(asdict(reference))
            await asyncio.to_thread(
                execution.store.record_artifact,
                command.job_id,
                command.workspace_id,
                settings.worker_id,
                job.fencing_token,
                reference,
            )
        receipt = {
            "request_hash": current.request_hash,
            "route_identity": route.identity(),
            "outputs": references,
            "usage": result.usage,
            "vendor_request_id": result.request_id,
        }
        await asyncio.to_thread(
            artifacts.upload,
            command.workspace_id,
            command.project_id,
            "assets",
            request["document_id"],
            "receipt.json",
            json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode(),
            version_id=asset_version,
            content_type="application/json",
        )
        return receipt

    task = await execute_generated_media(store, job, settings.worker_id, create, recover)
    if isinstance(task, WaitingReview):
        return task
    blocks = []
    for index, output in enumerate(task.outputs):
        metadata = {
            "asset": output.model_dump(mode="json"),
            "provider": route.provider,
            "model_name": route.model_name,
            "profile_version_id": route.profile_version_id,
            "parameters": task.request.parameters,
            "usage": task.usage,
            "vendor_request_id": task.vendor_request_id,
            **policy_provenance(),
        }
        if source_document:
            metadata.update(
                source_version_id=source_version,
                source_block_ids=[b.metadata["block_id"] for b in with_block_ids(source_document).document.blocks],
            )
        document_type = "image" if modality == "image" else "audio"
        if document_type == "audio":
            metadata["audio_role"] = {"audio": "narration", "music": "music", "effect": "effect"}[modality]
        blocks.append(ContentBlock(type=document_type, text=task.request.prompt, position=index, metadata=metadata))
    document = ContentDocument(
        document_id=request["document_id"],
        project_id=command.project_id,
        document_type="image" if modality == "image" else "audio",
        version=request["version"],
        schema_version="1.0",
        blocks=blocks,
        metadata={"workflow": snapshot.workflow, "review_scope": "before_provider_submission"},
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
        "document_type": modality,
        "title": request["title"],
        "workflow": snapshot.workflow,
        "source_version_id": source_version or None,
        "source_ir_hash": source_hash or None,
        "artifact": asdict(reference),
        "ir_hash": document.content_hash,
    }
    return {
        "document_version_id": version_id,
        "artifact": asdict(reference),
        "content_commit": commit,
        "media_assets": [output.model_dump(mode="json") for output in task.outputs],
    }
