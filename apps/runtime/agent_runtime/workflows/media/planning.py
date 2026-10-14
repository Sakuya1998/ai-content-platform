"""Database-prompt storyboard and timeline planning with frozen source provenance."""

import asyncio
import json

from agent_runtime.domain.content_adapters import with_block_ids
from agent_runtime.domain.context import ContextAssembler, ContextInput
from agent_runtime.domain.storyboard import Storyboard, storyboard_document
from agent_runtime.domain.timeline import Timeline, timeline_document
from agent_runtime.execution.context import current_execution
from agent_runtime.execution.errors import WaitingReview
from agent_runtime.infrastructure.models.client import RuntimeModels
from agent_runtime.prompts.renderer import render_node
from agent_runtime.workflows.content.graph import structured
from agent_runtime.workflows.media.production import frozen_documents, production_snapshot, publish_document
from agent_runtime.workflows.media.video import check_review


def validate_timeline_inputs(timeline, bundle, sources):
    allowed = set(bundle["request"].get("input_version_ids", []))
    kinds = {}
    for asset in timeline.assets:
        if asset.document_version_id not in allowed:
            raise ValueError("timeline asset is not in the frozen input catalogue")
        document = sources[asset.document_version_id]
        if asset.block_position >= len(document.blocks):
            raise ValueError("timeline block reference is out of bounds")
        block = document.blocks[asset.block_position]
        if block.type not in {"image", "video", "audio"} or not block.metadata.get("asset"):
            raise ValueError("timeline reference is not a media asset")
        kinds[asset.asset_id] = block.type
    if any(kinds[shot.asset_id] not in {"image", "video"} for shot in timeline.shots):
        raise ValueError("visual track requires image or video assets")
    if any(kinds[track.asset_id] not in {"audio", "video"} for track in timeline.audio_tracks):
        raise ValueError("audio track requires audio or video assets")
    return timeline


async def plan_production(command, bundle, snapshot):
    execution = current_execution()
    request, job, settings = bundle["request"], execution.job, execution.settings
    kind = request["document_type"]
    node = "media.storyboard.plan" if kind == "storyboard" else "media.timeline.plan"
    skill = "controlled-storyboard@1" if kind == "storyboard" else "controlled-timeline@1"
    production_snapshot(
        snapshot, workflow="media." + kind + "@1", skill=skill, required_nodes=[node, "media.review", "memory.refresh"]
    )
    if request.get("nodes") not in (None, [], ["plan", "review"]):
        raise ValueError("unsupported planning nodes")
    if bundle.get("committed_version"):
        existing = bundle["committed_version"]
        return {"document_version_id": existing["document_version_id"], "content_commit": existing}
    if job.result.get("content_commit") and job.resume_input.get("action", "approve") == "approve":
        return {key: job.result[key] for key in ["content_commit", "artifact"]} | {
            "document_version_id": job.result["content_commit"]["document_version_id"]
        }
    sources = frozen_documents(command, bundle)
    normalized = {identity: with_block_ids(document).document for identity, document in sources.items()}
    known_blocks = {b.metadata["block_id"] for doc in normalized.values() for b in doc.blocks}
    objective = request["objective"]
    if job.resume_input.get("feedback"):
        objective += "\nRevision feedback: " + str(job.resume_input["feedback"])
    inputs = [ContextInput(kind="constraints", source="job.brief", content=objective)]
    for identity, document in normalized.items():
        inputs.append(
            ContextInput(
                kind="current",
                source=identity,
                content=json.dumps(
                    {"document_version_id": identity, "document": document.model_dump(mode="json")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
    memories = await asyncio.to_thread(
        execution.store.creation_context, job, settings.worker_id, request.get("required_memory_version_ids", [])
    )
    inputs.extend(ContextInput.model_validate(value) for value in memories)
    pack = ContextAssembler(settings.context_token_budget).assemble(inputs)
    if not ({"job.brief"} | set(sources)).issubset({b.source for b in pack.blocks}):
        raise ValueError("production source exceeds context budget; split into smaller documents")
    await asyncio.to_thread(execution.store.save_execution_plan, job, settings.worker_id, snapshot, pack)
    context = "\n\n".join(b.content for b in pack.blocks)
    from agent_runtime.domain.constraints import format_constraints
    from agent_runtime.execution.policies import current_creation_policy, policy_provenance

    if policy := current_creation_policy():
        context += "\n" + format_constraints({"objective": objective, "document_type": kind}, policy.constraints)
    parameters = request.get("arguments", {}).get(kind, {})

    def validate(value):
        if kind == "timeline":
            validate_timeline_inputs(Timeline.model_validate(value["timeline"]), bundle, sources)
        else:
            board = Storyboard.model_validate({"shots": value["shots"]})
            if sum(shot.duration_ms for shot in board.shots) > 600000:
                raise ValueError("storyboard duration exceeds supported timeline budget")
            if len(board.shots) > parameters.get("max_shots", 128):
                raise ValueError("storyboard exceeds requested shot count")
            if any(set(shot.source_block_ids) - known_blocks for shot in board.shots):
                raise ValueError("storyboard cites unknown source blocks")

    models = RuntimeModels(settings)
    if kind == "timeline" and parameters:
        value = {"timeline": parameters}
        validate(value)
    else:
        value = await structured(
            models.chat("analysis"),
            render_node(node, {"objective": objective, "context": context, "parameters": parameters}),
            validate,
            node,
        )
    metadata = {"workflow": snapshot.workflow, "input_version_ids": list(sources), **policy_provenance()}
    builder, typed = (
        (storyboard_document, Storyboard.model_validate(value))
        if kind == "storyboard"
        else (timeline_document, Timeline.model_validate(value["timeline"]))
    )
    document = builder(
        typed,
        document_id=request["document_id"],
        project_id=command.project_id,
        version=request["version"],
        metadata=metadata,
    )
    review = await structured(
        models.chat("analysis"),
        render_node(
            "media.review",
            {"objective": objective + "\n" + context, "draft": json.dumps(value, ensure_ascii=False, sort_keys=True)},
        ),
        check_review,
        "media.review",
    )
    if kind == "storyboard" and sources and any(not shot.source_block_ids for shot in typed.shots):
        review = {**review, "approved": False, "notes": [*review["notes"], "Some shots lack source-block references."]}
    result = await publish_document(command, bundle, snapshot, document)
    if not review["approved"]:
        return WaitingReview(**result, review=review)
    return result
