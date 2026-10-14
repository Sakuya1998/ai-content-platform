"""Resolve frozen project assets, render locally, recover receipts and publish video IR."""

import asyncio
import json
import tempfile
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from botocore.exceptions import BotoCoreError, ClientError
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.domain.images import validate_image_bytes
from agent_runtime.domain.timeline import timeline_from_document
from agent_runtime.execution.context import current_execution
from agent_runtime.execution.errors import JobCancelled, WaitingReview
from agent_runtime.execution.leases import FencingTokenError
from agent_runtime.infrastructure.media.composition import CompositionError, ResolvedMediaAsset, compose_timeline
from agent_runtime.infrastructure.postgres.composition import CompositionStore
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.workflows.media.generated import read_asset, validate_asset_scope
from agent_runtime.workflows.media.planning import validate_timeline_inputs
from agent_runtime.workflows.media.production import frozen_documents, production_snapshot, publish_document

_TYPES = {
    "image": {"image/png", "image/jpeg", "image/webp"},
    "video": {"video/mp4"},
    "audio": {"audio/mpeg", "audio/wav", "audio/ogg", "audio/aac", "audio/flac", "audio/pcm"},
}


async def compose_content(command, bundle, snapshot):
    execution = current_execution()
    production_snapshot(
        snapshot, workflow="media.compose@1", skill="controlled-compose@1", required_nodes=["memory.refresh"]
    )
    if (
        snapshot.schema_versions.get("timeline") != "timeline@1"
        or snapshot.schema_versions.get("renderer") != "ffmpeg@1"
    ):
        raise ValueError("unsupported composition schema or renderer")
    try:
        return await _compose_content(command, bundle, snapshot, execution)
    except FencingTokenError:
        raise
    except BotoCoreError, ClientError, OperationalError, PoolTimeout, ConnectionError, TimeoutError:
        return WaitingReview(
            media_status="infrastructure_unavailable",
            review={
                "approved": False,
                "notes": [
                    "Resume this composition when storage/database are available; "
                    "frozen assets and archived output will be reused."
                ],
            },
        )
    except CompositionError:
        return WaitingReview(
            media_status="render_failed",
            review={
                "approved": False,
                "notes": [
                    "Composition could not render the frozen timeline; "
                    "check stream types, source durations and renderer configuration."
                ],
            },
        )


async def _compose_content(command, bundle, snapshot, execution):
    request = bundle["request"]
    if request.get("nodes") not in (None, [], ["validate", "render", "publish"]):
        raise ValueError("unsupported composition nodes")
    if existing := bundle.get("committed_version"):
        return {"document_version_id": existing["document_version_id"], "content_commit": existing}
    sources = frozen_documents(command, bundle)
    if not bundle.get("source"):
        raise ValueError("composition requires a frozen timeline version")
    source_id = bundle["source"]["document_version_id"]
    source = sources[source_id]
    timeline = validate_timeline_inputs(timeline_from_document(source), bundle, sources)
    settings, job = execution.settings, execution.job
    options = request.get("arguments", {}).get("compose", {})
    runs = CompositionStore(execution.store)
    frozen = {
        "timeline": timeline.model_dump(mode="json"),
        "source_version_id": source_id,
        "source_ir_hash": source.content_hash,
        "input_ir_hashes": {
            identity: sources[identity].content_hash for identity in request.get("input_version_ids", [])
        },
        "renderer": "ffmpeg@1",
        "preset": options.get("preset", "standard"),
        "threads": settings.compose_threads,
    }
    run = await asyncio.to_thread(runs.reserve, job, settings.worker_id, frozen)
    run = await asyncio.to_thread(runs.get, job, settings.worker_id)
    if run["state"] in {"cancel_requested", "cancelled"}:
        await asyncio.to_thread(runs.cancelled, job, settings.worker_id)
        raise JobCancelled()
    asset_version = str(uuid5(NAMESPACE_URL, f"composition-assets:{command.job_id}"))
    if run["state"] != "outputs_ready":
        artifacts = S3ArtifactStore(settings)
        receipt = None
        try:
            data = await asyncio.to_thread(
                artifacts.download,
                command.workspace_id,
                command.project_id,
                "assets",
                request["document_id"],
                "composition.json",
                version_id=asset_version,
            )
            if len(data) > 65536:
                raise ValueError("composition receipt exceeds size limit")
            receipt = json.loads(data)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404", "NotFound"}:
                raise
        if receipt is not None:
            if receipt["request_hash"] != run["request_hash"]:
                raise ValueError("composition receipt checksum conflict")
            await asyncio.to_thread(
                read_asset,
                artifacts,
                receipt["artifact"],
                command.workspace_id,
                command.project_id,
                settings.artifact_max_bytes,
            )
            run = await asyncio.to_thread(runs.archived, job, settings.worker_id, receipt["artifact"], receipt["probe"])
        else:
            run = await asyncio.to_thread(runs.begin, job, settings.worker_id)
            try:
                with tempfile.TemporaryDirectory(prefix="agent-render-") as directory:
                    root = Path(directory)
                    assets = {}
                    used = 0
                    for index, ref in enumerate(timeline.assets):
                        block = sources[ref.document_version_id].blocks[ref.block_position]
                        value = block.metadata["asset"]
                        limit = min(settings.artifact_max_bytes, settings.compose_input_max_bytes - used)
                        asset = validate_asset_scope(
                            value, command.workspace_id, command.project_id, artifacts.bucket, limit
                        )
                        if asset.content_type not in _TYPES.get(block.type, set()):
                            raise ValueError("composition media type conflict")
                        data = await asyncio.to_thread(
                            read_asset, artifacts, value, command.workspace_id, command.project_id, limit
                        )
                        used += len(data)
                        if block.type == "image":
                            validate_image_bytes(data, asset.content_type)
                        path = root / f"asset-{index}.bin"
                        path.write_bytes(data)
                        assets[ref.asset_id] = ResolvedMediaAsset(ref.asset_id, path, block.type, asset.content_type)
                    result = await compose_timeline(
                        timeline,
                        assets,
                        root / "output.mp4",
                        ffmpeg_bin=settings.ffmpeg_bin,
                        ffprobe_bin=settings.ffprobe_bin,
                        timeout_seconds=settings.compose_timeout_seconds,
                        threads=settings.compose_threads,
                        preset=options.get("preset", "standard"),
                    )
                    if result.path.stat().st_size > settings.artifact_max_bytes:
                        raise ValueError("composition output exceeds artifact size limit")
                    data = result.path.read_bytes()
                    reference = await asyncio.to_thread(
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
                        reference,
                    )
                    receipt = {
                        "request_hash": run["request_hash"],
                        "artifact": asdict(reference),
                        "probe": result.probe,
                    }
                    await asyncio.to_thread(
                        artifacts.upload,
                        command.workspace_id,
                        command.project_id,
                        "assets",
                        request["document_id"],
                        "composition.json",
                        json.dumps(receipt, sort_keys=True).encode(),
                        version_id=asset_version,
                        content_type="application/json",
                    )
                    run = await asyncio.to_thread(
                        runs.archived, job, settings.worker_id, receipt["artifact"], result.probe
                    )
            except asyncio.CancelledError:
                # Renderer has already reaped its child process group.
                if execution.cancel_requested:
                    with suppress(Exception):
                        await asyncio.to_thread(runs.cancelled, job, settings.worker_id)
                raise
    document = ContentDocument(
        document_id=request["document_id"],
        project_id=command.project_id,
        document_type="video",
        version=request["version"],
        schema_version="1.0",
        blocks=[
            ContentBlock(
                type="video",
                text=request.get("objective", f"Composition of {len(timeline.shots)} shots"),
                metadata={
                    "asset": run["artifact"],
                    "probe": run["probe"],
                    "timeline_version_id": source_id,
                    "renderer": "ffmpeg@1",
                },
            )
        ],
        metadata={
            "workflow": snapshot.workflow,
            "timeline_hash": source.content_hash,
            "input_ir_hashes": frozen["input_ir_hashes"],
        },
    )
    return await publish_document(command, bundle, snapshot, document)
