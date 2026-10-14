"""Build a deterministic timeline from a frozen storyboard and actual media streams."""

import asyncio
import math
import tempfile
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from agent_runtime.domain.assembly import AssemblyRequest, AssemblyReviewRequired, ProbedAsset, assemble_timeline
from agent_runtime.domain.images import validate_image_bytes
from agent_runtime.domain.storyboard import storyboard_from_document
from agent_runtime.domain.timeline import timeline_document
from agent_runtime.execution.context import current_execution
from agent_runtime.execution.errors import WaitingReview
from agent_runtime.execution.leases import FencingTokenError
from agent_runtime.execution.policies import policy_provenance
from agent_runtime.infrastructure.media.composition import CompositionError, probe_media
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.workflows.media.compose import _TYPES
from agent_runtime.workflows.media.generated import read_asset, validate_asset_scope
from agent_runtime.workflows.media.production import frozen_documents, production_snapshot, publish_document


async def assemble_content(command, bundle, snapshot):
    production_snapshot(
        snapshot, workflow="media.assemble@1", skill="controlled-assemble@1", required_nodes=["memory.refresh"]
    )
    if (
        snapshot.schema_versions.get("storyboard") != "storyboard@1"
        or snapshot.schema_versions.get("timeline") != "timeline@1"
    ):
        raise ValueError("unsupported assembly schema")
    try:
        return await _assemble_content(command, bundle, snapshot)
    except FencingTokenError:
        raise
    except AssemblyReviewRequired, CompositionError:
        return WaitingReview(
            media_status="assembly_review",
            review={
                "approved": False,
                "notes": [
                    "Assembly could not preserve the full storyboard and dialogue; "
                    "check actual media durations and bindings."
                ],
            },
        )
    except BotoCoreError, ClientError, OperationalError, PoolTimeout, ConnectionError, TimeoutError:
        return WaitingReview(
            media_status="infrastructure_unavailable",
            review={
                "approved": False,
                "notes": [
                    "Resume assembly when storage, probe executables and database are available; "
                    "existing assets will be reused."
                ],
            },
        )


async def _assemble_content(command, bundle, snapshot):
    execution = current_execution()
    request, settings = bundle["request"], execution.settings
    if request.get("operation") != "assemble" or request.get("nodes") not in (
        None,
        [],
        ["validate", "assemble", "publish"],
    ):
        raise ValueError("unsupported automatic assembly nodes")
    if existing := bundle.get("committed_version"):
        return {"document_version_id": existing["document_version_id"], "content_commit": existing}
    sources = frozen_documents(command, bundle)
    source = bundle.get("source")
    if not source:
        raise ValueError("assembly requires a frozen storyboard")
    board = storyboard_from_document(sources[source["document_version_id"]])
    options = AssemblyRequest.model_validate(request.get("arguments", {}).get("assemble", {}))
    allowed = set(request.get("input_version_ids", []))
    references = []
    audio_roles = {}

    def bind_audio_role(reference, role):
        existing = audio_roles.get(reference)
        if existing is not None and existing != role:
            raise ValueError("one audio asset cannot be assigned to multiple track roles")
        audio_roles[reference] = role

    for shot in options.shots:
        references.append((shot.visual_version_id, shot.visual_block_position))
        if shot.audio_version_id:
            narration_reference = (shot.audio_version_id, shot.audio_block_position)
            references.append(narration_reference)
            bind_audio_role(narration_reference, "narration")
    if options.music_version_id:
        music_reference = (options.music_version_id, options.music_block_position)
        references.append(music_reference)
        bind_audio_role(music_reference, "music")
    for effect in options.effects:
        effect_reference = (effect.version_id, effect.block_position)
        references.append(effect_reference)
        bind_audio_role(effect_reference, "effect")
    references = list(dict.fromkeys(references))
    if any(identity not in allowed for identity, _ in references):
        raise ValueError("assembly reference is not a frozen input")
    storage = S3ArtifactStore(settings)
    media = {}
    used = 0
    with tempfile.TemporaryDirectory(prefix="assemble-") as directory:
        for index, (identity, position) in enumerate(references):
            document = sources[identity]
            if position >= len(document.blocks):
                raise ValueError("assembly block position exceeds document")
            block = document.blocks[position]
            asset = block.metadata.get("asset")
            if block.type not in _TYPES or not asset or asset.get("content_type") not in _TYPES[block.type]:
                raise ValueError("assembly reference is not a supported media block")
            expected_role = audio_roles.get((identity, position))
            if expected_role:
                actual_role = block.metadata.get("audio_role")
                if actual_role is None and expected_role == "narration":
                    pass
                elif actual_role != expected_role:
                    raise ValueError("assembly audio role does not match the requested track")
            limit = min(settings.artifact_max_bytes, settings.compose_input_max_bytes - used)
            validate_asset_scope(asset, command.workspace_id, command.project_id, storage.bucket, limit)
            data = await asyncio.to_thread(read_asset, storage, asset, command.workspace_id, command.project_id, limit)
            used += len(data)
            if block.type == "image":
                validate_image_bytes(data, asset["content_type"])
            path = Path(directory) / f"asset-{index}.bin"
            path.write_bytes(data)
            metadata = await probe_media(
                path,
                ffprobe_bin=settings.ffprobe_bin,
                timeout_seconds=min(30, settings.compose_timeout_seconds),
                content_type=asset["content_type"],
            )
            if block.type in {"image", "video"} and not metadata["has_video"]:
                raise CompositionError("visual asset has no video/image stream")
            if block.type == "audio" and not metadata["has_audio"]:
                raise CompositionError("audio asset has no audio stream")
            durations = {
                key: max(0, math.ceil(metadata.get(key, 0) * 1000))
                for key in ["duration", "video_duration", "audio_duration"]
            }
            media[identity, position] = ProbedAsset(
                identity,
                position,
                block.type,
                durations["duration"],
                visual_video_duration_ms=durations["video_duration"] if metadata["has_video"] else None,
                audio_duration_ms=durations["audio_duration"] if metadata["has_audio"] else None,
            )
    timeline = assemble_timeline(board, options, media)
    document = timeline_document(
        timeline,
        document_id=request["document_id"],
        project_id=command.project_id,
        version=request["version"],
        metadata={
            "workflow": snapshot.workflow,
            "storyboard_version_id": source["document_version_id"],
            "assembly": "assembly@1",
            "input_version_ids": request.get("input_version_ids", []),
            **policy_provenance(),
        },
    )
    return await publish_document(command, bundle, snapshot, document)
