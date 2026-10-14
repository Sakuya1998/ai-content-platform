"""Execute the full specialized novel graph using Runtime-owned persistence."""

import asyncio
import inspect
import json
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from langgraph.types import Command

from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.execution.context import current_execution
from agent_runtime.execution.errors import WaitingReview
from agent_runtime.execution.snapshots import load_execution_snapshot
from agent_runtime.infrastructure.models.client import RuntimeModels
from agent_runtime.infrastructure.postgres.checkpoints import NovelPostgresSaver
from agent_runtime.infrastructure.postgres.novel import PostgresNovelStore, digest
from agent_runtime.infrastructure.postgres.novel_memory import PostgresNovelMemory
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.workflows.novel.agents.chapter_candidate import ChapterCandidateAgent, chapter_candidate_source_hash
from agent_runtime.workflows.novel.agents.consistency_checker import ConsistencyCheckerAgent
from agent_runtime.workflows.novel.agents.quality_evaluator import QualityEvaluatorAgent
from agent_runtime.workflows.novel.analysis.evaluation import combine_quality_scores, evaluate_chapter_deterministic
from agent_runtime.workflows.novel.analysis.memory_quality import (
    build_memory_eval_cases,
    build_memory_records,
    evaluate_memory_retrieval,
)
from agent_runtime.workflows.novel.context import novel_context
from agent_runtime.workflows.novel.graph.builder import build_graph
from agent_runtime.workflows.novel.graph.state import create_initial_state
from agent_runtime.workflows.novel.memory.canon import apply_canon_operation, ensure_canon
from agent_runtime.workflows.novel.memory.hierarchical import build_hierarchical_memory
from agent_runtime.workflows.novel.publication import novel_envelope, project_envelope
from agent_runtime.workflows.novel.resources.brief import normalize_creative_brief
from agent_runtime.workflows.novel.resources.catalog import QUALITY_DIMENSIONS, normalize_quality_policy_payload


def _resume_payload(value):
    response = dict(value)
    if response.get("restore_version"):
        response.update(action="restore_version", version_number=response["restore_version"])
    elif response.get("version_number"):
        response["action"] = "restore_version"
    if response.get("candidate_id"):
        response["action"] = "restore_candidate"
    if response.get("action") == "approve":
        response["feedback"] = "approve"
    if response.get("action") == "revise" and not str(response.get("feedback", "")).strip():
        raise ValueError("revision feedback is required")
    return response


def _effective_brief(inputs):
    brief = inputs.get("creative_brief") or {}
    template = (inputs.get("creative_template_snapshot") or {}).get("payload") or {}
    if not template or int(inputs.get("creative_brief_version", 1) or 1) > 1:
        return normalize_creative_brief(brief)
    defaults = normalize_creative_brief({})
    overrides = {key: value for key, value in brief.items() if value != defaults.get(key)}
    return normalize_creative_brief({**template, **overrides})


def _effective_resources(state):
    policy = (state.get("quality_policy_snapshot") or {}).get("payload") or {}
    if policy:
        policy = dict(policy)
        if "dimensions" in policy and policy.pop("dimensions") != list(QUALITY_DIMENSIONS):
            raise ValueError("frozen quality policy dimensions conflict")
        state["quality_gate_threshold"] = normalize_quality_policy_payload(policy)["gate_threshold"]
    return state


async def _publish(command, store, state):
    execution = current_execution()
    await asyncio.to_thread(store.save_state, state)
    envelope = project_envelope(await asyncio.to_thread(store.envelope, state))
    data = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > getattr(execution.settings, "artifact_max_bytes", 100_000_000):
        raise ValueError("novel state artifact exceeds configured limit")
    version = str(uuid5(NAMESPACE_URL, f"novel:{command.job_id}:{execution.job.fencing_token}:{digest(envelope)}"))
    reference = await asyncio.to_thread(
        S3ArtifactStore(execution.settings).upload,
        command.workspace_id,
        command.project_id,
        "assets",
        "novel-state",
        "state.json",
        data,
        version_id=version,
        content_type="application/json",
    )
    state_hash = digest(envelope["state"])
    await asyncio.to_thread(store.publish_state, asdict(reference), state_hash)
    return {"artifact": asdict(reference), "state_hash": state_hash, "project_id": command.project_id}


async def _content_commit(command, request, state, store):
    execution = current_execution()
    document = ContentDocument(
        document_id=request["document_id"],
        project_id=command.project_id,
        document_type="novel",
        version=request.get("version", 1),
        schema_version="1.0",
        blocks=[
            ContentBlock(
                type="chapter",
                text=chapter["content"],
                position=index,
                metadata={"chapter_number": chapter["chapter_number"], "title": chapter.get("title", "")},
            )
            for index, chapter in enumerate(state.get("chapters") or [])
        ],
    )
    identity = str(
        uuid5(NAMESPACE_URL, f"novel-content:{command.job_id}:{execution.job.fencing_token}:{document.content_hash}")
    )
    data = json.dumps(document.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode()
    artifact = await asyncio.to_thread(
        S3ArtifactStore(execution.settings).upload,
        command.workspace_id,
        command.project_id,
        "documents",
        document.document_id,
        "content.json",
        data,
        version_id=identity,
        content_type="application/json",
    )
    # Artifact recording and the eventual job completion are both fenced by the Runtime store.
    await asyncio.to_thread(
        execution.store.record_artifact,
        command.job_id,
        command.workspace_id,
        execution.settings.worker_id,
        execution.job.fencing_token,
        artifact,
    )
    return {
        "document_version_id": identity,
        "content_commit": {
            "document_id": document.document_id,
            "document_version_id": identity,
            "source_version": document.version,
            "document_type": "novel",
            "title": request["title"],
            "workflow": "novel.write@1",
            "source_version_id": request.get("source_version_id"),
            "source_ir_hash": None,
            "artifact": asdict(artifact),
            "ir_hash": document.content_hash,
        },
    }


async def _evaluate(store, state, arguments):
    number = int(arguments.get("chapter_number", state.get("current_chapter", 1)))
    version_number = arguments.get("version_number")
    version = store.get_chapter_version(store.project_id, number, int(version_number)) if version_number else None
    if version_number is not None and version is None:
        raise ValueError("chapter version to evaluate does not exist")
    chapter = version or next(
        (item for item in state.get("chapters") or [] if item["chapter_number"] == number),
        state.get("current_draft") or {},
    )
    if not chapter or chapter.get("chapter_number", number) != number:
        raise ValueError("chapter to evaluate does not exist")
    previous = [item for item in state.get("chapters") or [] if item["chapter_number"] < number]
    issues = await ConsistencyCheckerAgent().check(
        chapter=chapter,
        world_bible=state.get("world_bible", ""),
        characters=state.get("characters") or [],
        outline=state.get("outline") or [],
        previous_chapters=previous,
        canon=state.get("canon") or {},
        total_chapters=state.get("total_chapters"),
        creative_brief=state.get("creative_brief"),
        memory_index=state.get("memory_index"),
        max_chapter_words=state.get("max_chapter_words"),
    )
    deterministic = evaluate_chapter_deterministic(chapter, issues=issues)
    judged = (
        await QualityEvaluatorAgent().evaluate(
            novel=state, chapter=chapter, previous_chapters=previous, deterministic_report=deterministic
        )
        if arguments.get("include_judge")
        else {}
    )
    return store.save_chapter_evaluation(
        store.project_id,
        number,
        chapter_version=version_number,
        deterministic_scores=deterministic["scores"],
        judge_scores=judged.get("scores", {}),
        overall_score=combine_quality_scores(deterministic["scores"], judged.get("scores", {})),
        findings=[*deterministic.get("findings", []), *judged.get("findings", [])],
    )


def adapt_novel_request(request):
    value = dict(request)
    arguments = dict(request.get("arguments") or {})
    if "block_index" in arguments:
        arguments["chapter_number"] = arguments.pop("block_index")
    value["arguments"] = arguments
    if value.get("operation") == "revision":
        value["operation"] = "book_revision"
    inputs = novel_envelope(dict(request.get("input") or {}))
    frozen = request.get("frozen_resources") or {}
    for field in ("content_type_snapshot", "style_snapshot", "creative_template_snapshot", "quality_policy_snapshot"):
        if field in frozen:
            inputs[field] = frozen[field]
    inputs.pop("project_id", None)
    inputs["novel_id"] = request["project_id"]
    if "title" in request:
        inputs["title"] = request["title"]
    if "brief_version" in request:
        inputs["creative_brief_version"] = request["brief_version"]
    if request.get("brief") is not None:
        inputs["creative_brief"] = request["brief"]
    value["novel_input"] = inputs
    for field in ("imported_state", "imported_envelope"):
        if request.get(field) is not None:
            value[field] = novel_envelope(request[field])
    return value


async def run_novel(command, bundle=None):
    execution = current_execution()
    bundle = bundle or await load_execution_snapshot(command, execution.settings)
    request = adapt_novel_request(bundle["request"])
    from agent_runtime.execution.policies import policy_provenance

    if bundle.get("job_id", command.job_id) != command.job_id:
        raise ValueError("novel execution job conflict")
    if request["project_id"] != command.project_id or request.get("document_type") != "novel":
        raise ValueError("novel execution scope conflict")
    operation = request.get("operation", "run")
    arguments = request.get("arguments") or {}
    store = PostgresNovelStore(execution)
    models = RuntimeModels(execution.settings)
    memory = PostgresNovelMemory(store, models)
    style_snapshot = {}

    def scoped_memory(identity):
        store.check_scope(identity)
        return memory

    with store.project_lock(), novel_context(store, scoped_memory, models, style_snapshot=style_snapshot):
        graph = build_graph(NovelPostgresSaver(store))
        config = {"configurable": {"thread_id": command.project_id}, "recursion_limit": 1000}
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            if operation != "run" and not request.get("imported_state"):
                raise ValueError("novel workflow has not been initialized")
            inputs = dict(request.get("novel_input") or {})
            inputs.pop("novel_id", None)
            inputs.setdefault("title", request["title"])
            inputs.setdefault("genre", "fiction")
            inputs.setdefault("inspiration", request["objective"])
            inputs.setdefault("total_chapters", 1)
            inputs.setdefault("style", "default")
            inputs["creative_brief"] = _effective_brief(inputs)
            allowed = set(inspect.signature(create_initial_state).parameters) - {"novel_id", "config"}
            unknown = set(inputs) - allowed
            if unknown:
                raise ValueError("unsupported novel input fields: " + ",".join(sorted(unknown)))
            if not 1 <= int(inputs["total_chapters"]) <= 10000:
                raise ValueError("invalid total chapters")
            state = create_initial_state(novel_id=command.project_id, **inputs)
            imported = request.get("imported_state")
            if imported:
                if not isinstance(imported, dict):
                    raise ValueError("imported novel state must be an object")
                state = {**state, **imported, "novel_id": command.project_id}
                incoming = request.get("novel_input") or {}
                for field in (
                    "content_type_snapshot",
                    "style_snapshot",
                    "creative_template_snapshot",
                    "quality_policy_snapshot",
                ):
                    if field in incoming:
                        state[field] = incoming[field]
                incoming_version = incoming.get("creative_brief_version")
                if incoming_version is not None:
                    state["creative_brief_version"] = max(int(incoming_version or 1), 1)
                if "creative_brief" in incoming or "creative_template_snapshot" in incoming:
                    state["creative_brief"] = _effective_brief({**state, **incoming})
            _effective_resources(state)
            state["creation_policy_provenance"] = policy_provenance()
            style_snapshot.update(state.get("style_snapshot") or {})
            await asyncio.to_thread(store.initialize, state)
            await asyncio.to_thread(store.save_state, state)
            if imported and request.get("imported_envelope"):
                await asyncio.to_thread(store.import_envelope, request["imported_envelope"])
            payload = state
            if imported:
                predecessors = {
                    "human_review": "consistency_checker",
                    "blueprint_review": "plot_planner",
                    "scene_review": "scene_planner",
                    "completed": "book_auditor",
                }
                predecessor = predecessors.get(state.get("current_phase"))
                if operation == "resume" and arguments.get("review_type") in predecessors:
                    predecessor = predecessors[arguments["review_type"]]
                if predecessor:
                    await graph.aupdate_state(config, state, as_node=predecessor)
                    snapshot = await graph.aget_state(config)
                    payload = None
                    if operation == "resume":
                        await graph.ainvoke(None, config)
                        snapshot = await graph.aget_state(config)
                        state = dict(snapshot.values)
                        if not snapshot.next:
                            raise ValueError("imported novel workflow is already completed")
                        payload = Command(resume=_resume_payload(execution.job.resume_input or arguments))
        else:
            state = dict(snapshot.values)
            state["creation_policy_provenance"] = policy_provenance()
            incoming = request.get("novel_input") or {}
            stored_version = int(state.get("creative_brief_version", 1) or 1)
            incoming_version = int(incoming.get("creative_brief_version", stored_version) or stored_version)
            if incoming_version < stored_version:
                raise ValueError("novel brief version is stale")
            effective_brief = (
                _effective_brief(incoming)
                if any(key in incoming for key in ("creative_brief", "creative_template_snapshot"))
                else state.get("creative_brief")
            )
            resources = {
                key: incoming[key]
                for key in (
                    "content_type_snapshot",
                    "style_snapshot",
                    "creative_template_snapshot",
                    "quality_policy_snapshot",
                )
                if key in incoming
            }
            changed = (
                incoming_version > stored_version
                or effective_brief != state.get("creative_brief")
                or any(state.get(key) != value for key, value in resources.items())
            )
            if changed:
                required = bool(state.get("creative_brief_review_required")) or snapshot.next == ("human_review",)
                updates = {
                    "creative_brief": effective_brief,
                    "creative_brief_version": incoming_version,
                    "creative_brief_review_required": required,
                    "candidate_source_hash": "",
                    **resources,
                }
                _effective_resources(updates)
                if required:
                    updates.update(issues=[], quality_report={})
                await graph.aupdate_state(config, updates)
                snapshot = await graph.aget_state(config)
                state = dict(snapshot.values)
            style_snapshot.update(state.get("style_snapshot") or {})
            payload = None
            if execution.job.resume_input or operation == "resume":
                if not snapshot.next:
                    raise ValueError("novel workflow is already completed")
                response = _resume_payload(execution.job.resume_input or arguments)
                if state.get("creative_brief_review_required") and snapshot.next == ("human_review",):
                    if response.get("candidate_id") or response.get("version_number"):
                        raise ValueError("updated novel brief requires consistency recheck before restoration")
                    feedback = str(response.get("feedback", "")).strip().casefold()
                    if feedback in {"", "approve", "recheck", "yes", "y", "通过", "重新质检"}:
                        response = {"action": "creative_brief_update"}
                if response.get("review_type") and response["review_type"] not in snapshot.next:
                    raise ValueError("novel review type conflict")
                if response.get("version_number") and snapshot.next in {("blueprint_review",), ("scene_review",)}:
                    artifact_type = "blueprint" if snapshot.next == ("blueprint_review",) else "scene"
                    chapter = 0 if artifact_type == "blueprint" else state["current_chapter"]
                    version = store.get_planning_version(
                        command.project_id, artifact_type, chapter, int(response["version_number"])
                    )
                    if version is None:
                        raise ValueError("planning version does not exist")
                    response = version["payload"]
                payload = Command(resume=response)

        extra = {}
        if operation == "candidates":
            if state.get("creative_brief_review_required"):
                raise ValueError("updated novel brief requires consistency recheck before candidates")
            if snapshot.next != ("human_review",):
                raise ValueError("candidates require chapter human review")
            count = int(arguments.get("count", 2))
            if not 1 <= count <= 4:
                raise ValueError("candidate count must be between 1 and 4")
            source_hash = chapter_candidate_source_hash(state)
            for index in range(1, count + 1):
                candidate = await ChapterCandidateAgent(command.project_id).generate(
                    state, candidate_number=index, total_candidates=count, instruction=arguments.get("instruction", "")
                )
                candidate.pop("chapter_number", None)
                store.save_chapter_candidate(
                    command.project_id,
                    state["current_chapter"],
                    source_hash=source_hash,
                    batch_id=command.job_id,
                    **candidate,
                )
            extra = {"candidate_count": count}
        elif operation in {"memory_rebuild", "memory_evaluate"}:
            source = {key: state.get(key) for key in ("world_bible", "characters", "outline", "chapters", "canon")}
            if operation == "memory_rebuild":
                index = build_hierarchical_memory(state.get("chapters") or [], total_chapters=state["total_chapters"])
                records = build_memory_records(novel_id=command.project_id, **source, memory_index=index)
                report = await asyncio.to_thread(memory.rebuild, records)
                state["memory_index"] = index
                await graph.aupdate_state(
                    config, {"memory_index": index}, as_node="book_auditor" if not snapshot.next else None
                )
                if arguments.get("evaluate", True):
                    cases = build_memory_eval_cases(novel_id=command.project_id, **source)
                    report["quality"] = await asyncio.to_thread(
                        evaluate_memory_retrieval, memory=memory, cases=cases, k=arguments.get("k", 5)
                    )
                else:
                    report["quality"] = None
            else:
                cases = build_memory_eval_cases(novel_id=command.project_id, **source)
                report = await asyncio.to_thread(
                    evaluate_memory_retrieval, memory=memory, cases=cases, k=arguments.get("k", 5)
                )
            store.save_memory_snapshot(command.project_id, schema_version="memory-quality-v1", payload=report)
            store.record("memory_quality", {"operation": operation, "report": report})
            extra = {"memory_report": report}
        elif operation == "evaluation":
            extra = {"evaluation": await _evaluate(store, state, arguments)}
        else:
            if operation == "canon":
                canon_operation = arguments.get("operation", arguments)
                if snapshot.next == ("human_review",):
                    payload = Command(resume={"action": "canon_update", "operation": canon_operation})
                else:
                    state["canon"] = apply_canon_operation(
                        ensure_canon(
                            state.get("canon"),
                            world_bible=state.get("world_bible", ""),
                            characters=state.get("characters") or [],
                            outline=state.get("outline") or [],
                            chapters=state.get("chapters") or [],
                        ),
                        canon_operation,
                    )
                    await graph.aupdate_state(
                        config, {"canon": state["canon"]}, as_node="book_auditor" if not snapshot.next else None
                    )
                    payload = None
            elif operation == "book_revision":
                if snapshot.next or not state.get("book_audit_completed"):
                    raise ValueError("book revision requires completed novel")
                number = int(arguments["chapter_number"])
                chapter = next((item for item in state.get("chapters") or [] if item["chapter_number"] == number), None)
                if chapter is None:
                    raise ValueError("final chapter does not exist")
                plan = next((item for item in state["outline"] if item["chapter"] == number), {})
                await graph.aupdate_state(
                    config,
                    {
                        "current_chapter": number,
                        "current_phase": "writing",
                        "chapter_plan": plan,
                        "scene_plan": chapter.get("scene_plan") or [],
                        "current_draft": chapter,
                        "revision_notes": arguments.get("feedback", ""),
                        "revision_scene_number": 0,
                        "revision_count": 0,
                        "issues": [],
                        "quality_report": {},
                        "persistence_error": "",
                        "book_revision_mode": True,
                        "book_revision_origin_hash": state["book_audit"].get("manuscript_hash", ""),
                        "book_audit_completed": False,
                        "candidate_source_hash": "",
                        "next_agent": "scene_writer",
                    },
                    as_node="orchestrator",
                )
                payload = None
            elif operation not in {"run", "resume"}:
                raise ValueError("unsupported novel operation")
            async for values in graph.astream(payload, config, stream_mode="values"):
                state = {key: value for key, value in values.items() if not key.startswith("__")}
                await asyncio.to_thread(store.save_state, state)
            snapshot = await graph.aget_state(config)
            state = dict(snapshot.values)

        result = {**await _publish(command, store, state), **extra}
        if extra:
            return result
        if snapshot.next:
            interrupts = [item.value for task in snapshot.tasks for item in task.interrupts]
            return WaitingReview(
                **result,
                checkpoint_id=snapshot.config["configurable"]["checkpoint_id"],
                review=interrupts[0] if interrupts else {"type": snapshot.next[0]},
            )
        if state.get("chapters"):
            result.update(await _content_commit(command, request, state, store))
        return result


def validate_novel_snapshot(snapshot):
    if snapshot.workflow != "novel.write@1":
        raise ValueError("unsupported novel workflow")
    execution = current_execution(required=False)
    resources = execution.resource_versions if execution else {}
    from agent_runtime.domain.workflow_contracts import WORKFLOWS

    if execution and not set(WORKFLOWS["novel.write@1"]).issubset(execution.prompt_versions):
        raise ValueError("missing frozen novel prompt bindings")
    for field, builtin, kind in (("skills", "legacy-novel@1", "skill"),):
        references = getattr(snapshot, field)
        if builtin not in references:
            raise ValueError("missing built-in novel capability")
        for reference in set(references) - {builtin}:
            resource = resources.get(reference)
            if not resource or resource.get("kind", resource.get("Kind")) != kind:
                raise ValueError("unsupported novel capability references")
    if snapshot.tools or snapshot.mcp_policies or snapshot.schema_versions.get("content_ir") != "1.0":
        raise ValueError("unsupported novel execution schema")
