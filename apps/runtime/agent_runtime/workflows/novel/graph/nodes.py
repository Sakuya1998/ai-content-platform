"""LangGraph 节点实现(文档 4.2 / 4.3)。

节点即"读取状态 → 调用 Agent → 返回状态增量"的纯函数。
定稿语义:current_draft 在 写作→润色→质检→人工审查 之间流转,
人工批准时才写入 chapters(按章节号追加或替换的唯一写入点),
保证 chapters 中的每一章都是终稿。
"""

import asyncio
import logging
from typing import Any

from langgraph.types import interrupt

from agent_runtime.execution.model_context import model_call_context
from agent_runtime.workflows.novel.agents.book_auditor import BookAuditorAgent
from agent_runtime.workflows.novel.agents.chapter_candidate import chapter_candidate_matches_state
from agent_runtime.workflows.novel.agents.chapter_digest import ChapterDigestAgent, has_current_digest
from agent_runtime.workflows.novel.agents.character_designer import CharacterDesignerAgent, validate_characters
from agent_runtime.workflows.novel.agents.consistency_checker import ConsistencyCheckerAgent
from agent_runtime.workflows.novel.agents.orchestrator import OrchestratorAgent
from agent_runtime.workflows.novel.agents.plot_planner import PlotPlannerAgent, validate_outline
from agent_runtime.workflows.novel.agents.replanner import ReplannerAgent, merge_future_outline
from agent_runtime.workflows.novel.agents.scene_planner import ScenePlannerAgent, normalize_scene_plan
from agent_runtime.workflows.novel.agents.scene_rewriter import SceneRewriterAgent
from agent_runtime.workflows.novel.agents.scene_writer import SceneWriterAgent
from agent_runtime.workflows.novel.agents.style_editor import StyleEditorAgent
from agent_runtime.workflows.novel.agents.world_builder import WorldBuilderAgent
from agent_runtime.workflows.novel.analysis.book import (
    BOOK_AUDIT_RUBRIC_VERSION,
    BOOK_AUDIT_SCHEMA_VERSION,
    evaluate_book_deterministic,
    manuscript_hash,
)
from agent_runtime.workflows.novel.analysis.evaluation import evaluate_chapter_deterministic, quality_gate_result
from agent_runtime.workflows.novel.graph.state import merge_chapters
from agent_runtime.workflows.novel.memory.access import NovelMemory
from agent_runtime.workflows.novel.memory.canon import (
    apply_canon_operation,
    apply_outline_revision,
    build_canon,
    ensure_canon,
    format_canon,
    narrative_beats_for_chapter,
    record_final_chapter,
    replace_final_chapter,
)
from agent_runtime.workflows.novel.memory.hierarchical import (
    HIERARCHICAL_MEMORY_SCHEMA_VERSION,
    build_hierarchical_memory,
    format_hierarchical_memory,
    hierarchical_memory_hash,
)
from agent_runtime.workflows.novel.scenes import ensure_scene_drafts

logger = logging.getLogger(__name__)
orchestrator_agent = OrchestratorAgent()

_DIGEST_FIELDS = (
    "summary",
    "events",
    "characters",
    "locations",
    "emotion",
    "extracted_facts",
    "digest_version",
    "digest_content_hash",
)


def _novel_store():
    from agent_runtime.workflows.novel.context import current_novel

    if dependencies := current_novel():
        return dependencies.store
    raise RuntimeError("Runtime novel store scope is missing")


def _novel_id(state: dict[str, Any]) -> str:
    """从状态中提取小说 ID(用于记忆/持久化);缺省空串表示不持久化。"""
    return str(state.get("novel_id", ""))


async def _digest_final_draft(
    state: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any]:
    """提炼已批准正文；持久化重试时复用匹配当前正文的结果。"""
    if has_current_digest(draft):
        return dict(draft)
    with model_call_context(_novel_id(state), "chapter_digest"):
        digest = await ChapterDigestAgent().digest(
            chapter=draft,
            canon=state.get("canon"),
        )
    return {**draft, **digest}


def _chapter_digest_payload(chapter: dict[str, Any]) -> dict[str, Any]:
    return {field: chapter[field] for field in _DIGEST_FIELDS if field in chapter}


def _memory_index_after_finalization(
    state: dict[str, Any],
    final_draft: dict[str, Any],
) -> dict[str, Any]:
    chapters = merge_chapters(state.get("chapters") or [], [final_draft])
    return build_hierarchical_memory(
        chapters,
        total_chapters=int(state.get("total_chapters", len(chapters)) or len(chapters)),
    )


async def _replan_after_finalization(
    state: dict[str, Any],
    final_draft: dict[str, Any],
    canon: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """分析成稿对未来大纲的影响；失败时返回原大纲且不阻塞定稿。"""
    current = int(state.get("current_chapter", 1) or 1)
    total = int(state.get("total_chapters", 0) or 0)
    outline = [dict(item) for item in state.get("outline") or []]
    future = [item for item in outline if int(item.get("chapter", 0) or 0) > current]
    if not future:
        return (
            outline,
            {
                "status": "stable",
                "impact": "low",
                "rationale": "已是最终章，没有需要重规划的后续章节。",
                "outline_updates": [],
                "replan_version": "replan-v1",
            },
            canon,
        )

    try:
        with model_call_context(_novel_id(state), "replanner"):
            proposal = await ReplannerAgent().analyze(
                current_chapter=current,
                total_chapters=total,
                chapter_plan=state.get("chapter_plan") or {},
                chapter_digest=_chapter_digest_payload(final_draft),
                future_outline=future,
                creative_brief=state.get("creative_brief"),
            )
        updated_outline = merge_future_outline(
            outline,
            proposal.get("outline_updates") or [],
            current_chapter=current,
            total_chapters=total,
        )
        if proposal.get("status") == "replanned":
            revised_canon = apply_outline_revision(canon, updated_outline)
            revised_canon = record_final_chapter(revised_canon, final_draft)
            proposal = {**proposal, "outline": updated_outline}
            return updated_outline, proposal, revised_canon
        proposal = {**proposal, "outline": outline}
        return outline, proposal, canon
    except Exception as exc:
        logger.warning("未来大纲重规划失败(%s),保留原大纲", type(exc).__name__)
        return (
            outline,
            {
                "status": "error",
                "impact": "high",
                "rationale": f"重规划未应用，原因:{type(exc).__name__}",
                "outline_updates": [],
                "replan_version": "replan-v1",
                "outline": outline,
            },
            canon,
        )


def _save_planning_snapshot(
    state: dict[str, Any],
    artifact_type: str,
    chapter_number: int,
    source: str,
    payload: dict[str, Any],
) -> None:
    """保存辅助规划快照；失败不得阻断 LangGraph 检查点。"""
    novel_id = _novel_id(state)
    if not novel_id:
        return
    try:
        store = _novel_store()
        if hasattr(store, "save_planning_version"):
            store.save_planning_version(
                novel_id,
                artifact_type,
                chapter_number,
                source=source,
                payload=payload,
            )
    except Exception as exc:
        logger.warning(
            "规划版本快照写入失败(%s, %s, %s)",
            artifact_type,
            source,
            type(exc).__name__,
        )


def _assign_narrative_beats(
    scene_plan: list[dict[str, Any]],
    beats: list[dict[str, Any]],
    preferred_scene: int = 0,
) -> list[dict[str, Any]]:
    """保留已有分配，将新增 Canon beat 确定性挂到指定或首个场景。"""
    if not scene_plan:
        return []
    updated = [dict(scene) for scene in scene_plan]
    previous_assignment: dict[str, int] = {}
    for scene in updated:
        number = int(scene.get("scene_number", 0) or 0)
        for beat in scene.get("narrative_beats") or []:
            key = str(beat.get("beat_id", "")) or (f"{beat.get('thread', '')}:{beat.get('action', '')}")
            previous_assignment[key] = number
        scene["narrative_beats"] = []
    valid_numbers = {int(scene.get("scene_number", 0) or 0) for scene in updated}
    fallback = min(valid_numbers) if valid_numbers else 0
    for beat in beats:
        key = str(beat.get("beat_id", "")) or f"{beat.get('thread', '')}:{beat.get('action', '')}"
        target = int(beat.get("scene_number", 0) or 0)
        if target not in valid_numbers:
            target = previous_assignment.get(key, 0)
        if target not in valid_numbers:
            target = preferred_scene if preferred_scene in valid_numbers else fallback
        assigned = {**beat, "scene_number": target}
        next(scene for scene in updated if int(scene.get("scene_number", 0) or 0) == target)["narrative_beats"].append(
            assigned
        )
    return updated


async def orchestrator_node(state: dict[str, Any]) -> dict[str, Any]:
    """主控调度:决定下一个 Agent,并准备当前章节大纲。"""
    next_agent = await orchestrator_agent.decide_next(state)
    updates: dict[str, Any] = {"next_agent": next_agent}

    # 进入新章节的写作循环时,取对应大纲作为 chapter_plan
    if next_agent in {"scene_planner", "scene_writer"}:
        outline = state.get("outline") or []
        current = int(state.get("current_chapter", 1))
        plan = next(
            (c for c in outline if int(c.get("chapter", 0)) == current),
            {"chapter": current, "title": f"第{current}章", "summary": ""},
        )
        updated_canon = ensure_canon(
            state.get("canon"),
            world_bible=str(state.get("world_bible", "")),
            characters=state.get("characters") or [],
            outline=outline,
            chapters=state.get("chapters") or [],
        )
        beats = narrative_beats_for_chapter(updated_canon, current)
        updates["chapter_plan"] = {**plan, "narrative_beats": beats}
        updates["current_phase"] = "writing"
        updates["canon"] = updated_canon
        updates["memory_index"] = state.get("memory_index") or build_hierarchical_memory(
            state.get("chapters") or [],
            total_chapters=int(state.get("total_chapters", 0) or 0),
        )
    return updates


async def book_auditor_node(state: dict[str, Any]) -> dict[str, Any]:
    """终章完成后运行一次跨章审计；模型或辅助存储失败不阻断完结。"""
    chapters = [dict(item) for item in state.get("chapters") or []]
    memory_index = state.get("memory_index") or build_hierarchical_memory(
        chapters,
        total_chapters=int(state.get("total_chapters", len(chapters)) or len(chapters)),
    )
    canon = ensure_canon(
        state.get("canon"),
        world_bible=str(state.get("world_bible", "")),
        characters=state.get("characters") or [],
        outline=state.get("outline") or [],
        chapters=chapters,
    )
    deterministic = evaluate_book_deterministic(
        chapters=chapters,
        canon=canon,
        total_chapters=int(state.get("total_chapters", len(chapters)) or len(chapters)),
    )
    content_hash = manuscript_hash(chapters)
    judge_scores: dict[str, float] = {}
    judge_findings: list[str] = []
    revision_priorities: list[str] = []
    judge_error = ""
    try:
        with model_call_context(_novel_id(state), "book_auditor"):
            judged = await BookAuditorAgent().evaluate(
                novel={
                    "title": state.get("title", ""),
                    "genre": state.get("genre", ""),
                    "style": state.get("style", ""),
                    "inspiration": state.get("inspiration", ""),
                    "creative_brief": state.get("creative_brief") or {},
                },
                chapters=chapters,
                canon=canon,
                deterministic_report=deterministic,
                memory_index=memory_index,
            )
        judge_scores = judged.get("scores") or {}
        judge_findings = judged.get("findings") or []
        revision_priorities = judged.get("revision_priorities") or []
    except Exception as exc:
        judge_error = f"模型终审失败:{type(exc).__name__}"
        logger.warning("全书模型终审失败(%s),保留确定性审计", type(exc).__name__)

    combined_scores = {
        **(deterministic.get("scores") or {}),
        **judge_scores,
    }
    report: dict[str, Any] = {
        "schema_version": BOOK_AUDIT_SCHEMA_VERSION,
        "rubric_version": BOOK_AUDIT_RUBRIC_VERSION,
        "manuscript_hash": content_hash,
        "deterministic_scores": deterministic.get("scores") or {},
        "judge_scores": judge_scores,
        "overall_score": round(
            sum(float(value) for value in combined_scores.values()) / max(len(combined_scores), 1),
            1,
        ),
        "findings": [
            *(deterministic.get("findings") or []),
            *[{"dimension": "literary_judgment", "message": item, "source": "model"} for item in judge_findings],
        ],
        "revision_priorities": revision_priorities,
        "judge_error": judge_error,
        "storage_error": "",
        "memory_schema_version": memory_index.get("schema_version", ""),
        "memory_index_hash": hierarchical_memory_hash(memory_index),
    }

    nid = _novel_id(state)
    if nid:
        try:
            store = _novel_store()
            store.save_book_audit(
                nid,
                manuscript_hash=content_hash,
                schema_version=BOOK_AUDIT_SCHEMA_VERSION,
                rubric_version=BOOK_AUDIT_RUBRIC_VERSION,
                report=report,
            )
            store.save_progress(
                novel_id=nid,
                current_chapter=int(state.get("current_chapter", len(chapters) + 1)),
                current_phase="completed",
                state={
                    "book_audit": report,
                    "book_audit_completed": True,
                    "memory_index": memory_index,
                },
            )
        except Exception as exc:
            report["storage_error"] = f"审计持久化失败:{type(exc).__name__}"
            logger.warning("全书审计辅助持久化失败(%s)", type(exc).__name__)

    return {
        "canon": canon,
        "memory_index": memory_index,
        "book_audit": report,
        "book_audit_completed": True,
        "current_phase": "completed",
    }


async def world_builder_node(state: dict[str, Any]) -> dict[str, Any]:
    """构建世界观圣经,进入角色设计阶段。"""
    agent = WorldBuilderAgent(novel_id=_novel_id(state))
    with model_call_context(_novel_id(state), "world_builder"):
        result = await agent.generate(
            genre=state.get("genre", ""),
            inspiration=state.get("inspiration", ""),
            title=state.get("title", ""),
            creative_brief=state.get("creative_brief"),
        )
    return {"world_bible": result["world_bible"]}


async def character_designer_node(state: dict[str, Any]) -> dict[str, Any]:
    """设计五维角色档案。"""
    agent = CharacterDesignerAgent(novel_id=_novel_id(state))
    with model_call_context(_novel_id(state), "character_designer"):
        characters = await agent.generate(
            world_bible=state.get("world_bible", ""),
            inspiration=state.get("inspiration", ""),
            creative_brief=state.get("creative_brief"),
        )
    return {"characters": characters}


async def plot_planner_node(state: dict[str, Any]) -> dict[str, Any]:
    """规划全书章节大纲。"""
    agent = PlotPlannerAgent(novel_id=_novel_id(state))
    with model_call_context(_novel_id(state), "plot_planner"):
        outline = await agent.generate(
            world_bible=state.get("world_bible", ""),
            characters=state.get("characters") or [],
            total_chapters=int(state.get("total_chapters", 10)),
            inspiration=state.get("inspiration", ""),
            creative_brief=state.get("creative_brief"),
        )
    return {
        "outline": outline,
        "canon": build_canon(
            world_bible=str(state.get("world_bible", "")),
            characters=state.get("characters") or [],
            outline=outline,
            chapters=state.get("chapters") or [],
        ),
    }


async def blueprint_review_node(state: dict[str, Any]) -> dict[str, Any]:
    """暂停并允许用户批准或结构化修订世界观、角色与全书大纲。"""
    generated_payload = {
        "world_bible": str(state.get("world_bible", "")),
        "characters": state.get("characters") or [],
        "outline": state.get("outline") or [],
    }
    _save_planning_snapshot(state, "blueprint", 0, "generated", generated_payload)
    response = interrupt(
        {
            "type": "blueprint_review",
            **generated_payload,
            "instruction": "确认或编辑创作蓝图后继续，正文尚未生成。",
        }
    )
    if not isinstance(response, dict):
        raise ValueError("蓝图审阅必须提交结构化对象")
    world_bible = str(response.get("world_bible", state.get("world_bible", ""))).strip()
    characters = response.get("characters", state.get("characters") or [])
    outline = response.get("outline", state.get("outline") or [])
    if not world_bible:
        raise ValueError("世界观圣经不能为空")
    if not isinstance(characters, list) or not isinstance(outline, list):
        raise ValueError("characters 和 outline 必须是列表")
    validate_characters(characters)
    validate_outline(outline, int(state.get("total_chapters", 0) or 0))
    sorted_outline = sorted(
        (dict(item) for item in outline),
        key=lambda item: int(item.get("chapter", 0) or 0),
    )
    approved_payload = {
        "world_bible": world_bible,
        "characters": [dict(item) for item in characters],
        "outline": sorted_outline,
    }
    _save_planning_snapshot(state, "blueprint", 0, "approved", approved_payload)
    return {
        **approved_payload,
        "canon": build_canon(
            world_bible=world_bible,
            characters=characters,
            outline=sorted_outline,
            chapters=state.get("chapters") or [],
        ),
        "current_phase": "writing",
    }


async def scene_planner_node(state: dict[str, Any]) -> dict[str, Any]:
    """将当前章节大纲拆分为可直接执行的场景序列。"""
    agent = ScenePlannerAgent()
    with model_call_context(_novel_id(state), "scene_planner"):
        scene_plan = await agent.plan_chapter(state)
    return {
        "scene_plan": scene_plan,
        "current_phase": "writing",
    }


async def scene_review_node(state: dict[str, Any]) -> dict[str, Any]:
    """暂停并允许用户在生成正文前修订当前章场景计划。"""
    chapter_number = int(state.get("current_chapter", 1) or 1)
    generated_payload = {"scene_plan": state.get("scene_plan") or []}
    _save_planning_snapshot(
        state,
        "scene",
        chapter_number,
        "generated",
        generated_payload,
    )
    response = interrupt(
        {
            "type": "scene_review",
            "chapter_number": chapter_number,
            "chapter_plan": state.get("chapter_plan") or {},
            "scene_plan": state.get("scene_plan") or [],
            "instruction": "确认或编辑场景计划后继续生成正文。",
        }
    )
    if not isinstance(response, dict):
        raise ValueError("场景审阅必须提交结构化对象")
    scene_plan = response.get("scene_plan", state.get("scene_plan") or [])
    if not isinstance(scene_plan, list):
        raise ValueError("scene_plan 必须是列表")
    normalized = normalize_scene_plan(
        scene_plan,
        state.get("chapter_plan") or {},
        int(state.get("max_chapter_words") or 6000),
    )
    _save_planning_snapshot(
        state,
        "scene",
        chapter_number,
        "approved",
        {"scene_plan": normalized},
    )
    return {"scene_plan": normalized, "current_phase": "writing"}


async def scene_writer_node(state: dict[str, Any]) -> dict[str, Any]:
    """撰写当前章草稿,交由风格编辑。"""
    agent = SceneWriterAgent(novel_id=_novel_id(state))
    with model_call_context(_novel_id(state), "scene_writer"):
        chapter = await agent.write_chapter(state)
    return {
        "current_draft": chapter,
        "current_phase": "style_editing",
        "revision_count": int(state.get("revision_count", 0)),
        "candidate_source_hash": "",
    }


async def scene_rewriter_node(state: dict[str, Any]) -> dict[str, Any]:
    """只重写人工指定的场景,保留其余场景正文并重新进入一致性检查。"""
    agent = SceneRewriterAgent()
    with model_call_context(_novel_id(state), "scene_rewriter"):
        chapter = await agent.rewrite_scene(state)
    return {
        "current_draft": chapter,
        "current_phase": "consistency_check",
        "revision_notes": "",
        "revision_scene_number": 0,
        "candidate_source_hash": "",
    }


async def style_editor_node(state: dict[str, Any]) -> dict[str, Any]:
    """风格润色当前草稿,交由一致性检查。"""
    agent = StyleEditorAgent()
    with model_call_context(_novel_id(state), "style_editor"):
        polished = await agent.polish(
            state.get("current_draft") or {},
            state.get("style", ""),
            state.get("creative_brief"),
        )
    return {
        "current_draft": polished,
        "current_phase": "consistency_check",
    }


async def consistency_checker_node(state: dict[str, Any]) -> dict[str, Any]:
    """一致性检查:结果写入 issues;存在 high 问题且未超重写上限 → 回写重写。

    通过(无问题/仅轻微/已超上限)→ human_review 人工终审。
    """
    agent = ConsistencyCheckerAgent()
    draft = state.get("current_draft") or {}
    number = int(draft.get("chapter_number", draft.get("chapter", 0)) or 0)
    finalized = state.get("chapters") or []
    previous_chapters = [
        item for item in finalized if int(item.get("chapter_number", item.get("chapter", 0)) or 0) < number
    ]
    future_chapters = (
        [item for item in finalized if int(item.get("chapter_number", item.get("chapter", 0)) or 0) > number]
        if state.get("book_revision_mode")
        else []
    )
    with model_call_context(_novel_id(state), "consistency_checker"):
        issues = await agent.check(
            chapter=draft,
            world_bible=state.get("world_bible", ""),
            characters=state.get("characters") or [],
            outline=state.get("outline") or [],
            previous_chapters=previous_chapters,
            future_chapters=future_chapters,
            memory_index=state.get("memory_index"),
            max_chapter_words=int(state.get("max_chapter_words", 0) or 0) or None,
            canon=state.get("canon"),
            total_chapters=int(state.get("total_chapters", 0) or 0) or None,
            creative_brief=state.get("creative_brief"),
        )
    quality_report = quality_gate_result(
        evaluate_chapter_deterministic(draft, issues=issues),
        threshold=(
            float(state.get("quality_gate_threshold", 70.0))
            if (state.get("quality_policy_snapshot") or {}).get("payload")
            else None
        ),
    )
    updates: dict[str, Any] = {
        "issues": issues,
        "quality_report": quality_report,
        "creative_brief_review_required": False,
    }

    serious = [i for i in issues if str(i.get("severity", "low")).lower() == "high"]
    over_limit = int(state.get("revision_count", 0)) >= int(state.get("max_revision_attempts", 3))
    if serious and not over_limit:
        notes = "\n".join(
            f"- [{i.get('severity')}] {i.get('description')} 修正建议:{i.get('suggestion')}" for i in serious
        )
        updates.update(
            {
                "revision_notes": notes,
                "revision_count": int(state.get("revision_count", 0)) + 1,
                "current_phase": "writing",  # 条件边将路由回 scene_writer 重写
            }
        )
        quality_report["status"] = "blocked_by_consistency"
    elif not quality_report["passed"] and not over_limit:
        updates.update(
            {
                "revision_notes": quality_report["revision_notes"],
                "revision_count": int(state.get("revision_count", 0)) + 1,
                "current_phase": "writing",
            }
        )
        quality_report["status"] = "rewrite"
    elif not quality_report["passed"]:
        quality_report["status"] = "escalated"
    else:
        quality_report["status"] = "passed"
    # else: 保持 phase=consistency_check,条件边放行至 human_review
    return updates


async def human_review_node(state: dict[str, Any]) -> dict[str, Any]:
    """人工审查(文档 9.1):interrupt 暂停图,等待 resume。

    resume 值约定:
        - "approve" / "通过" / 空白 → 定稿并进入下一章
        - 其他文本 → 视为修改意见,回 scene_writer 重写
        - {"feedback": "...", "scene_number": n} → 仅重写第 n 场
        - {"action": "canon_update", "operation": {...}} → 更新 Canon 后重新质检

    注意:interrupt 后节点将在 resume 时重新执行,本节点必须保持幂等。
    """
    draft = state.get("current_draft") or {}
    number = draft.get("chapter_number", state.get("current_chapter", 0))
    book_revision_mode = bool(state.get("book_revision_mode", False))
    persistence_error = str(state.get("persistence_error") or "")
    nid = _novel_id(state)
    retry_instruction = (
        "终稿事实提炼失败，请再次输入 approve 重试;或输入修改意见。"
        if persistence_error.startswith("终稿事实提炼失败")
        else "定稿持久化失败,请稍后再次输入 approve 重试;或输入修改意见。"
    )
    approval_instruction = (
        "输入 approve 替换该章终稿并重新运行全书终审;或继续输入修改意见。"
        if book_revision_mode
        else "输入 approve 定稿进入下一章;或直接输入修改意见。"
    )

    if nid and draft.get("content"):
        source = "book_revision" if book_revision_mode else "initial"
        if not book_revision_mode and str(draft.get("status")) == "revised":
            source = "scene_revision"
        elif not book_revision_mode and str(draft.get("status")) == "restored":
            source = "restored"
        elif not book_revision_mode and int(state.get("revision_count", 0)) > 0:
            source = "revision"
        try:
            version_store = _novel_store()
            if hasattr(version_store, "save_chapter_version"):
                version_store.save_chapter_version(
                    novel_id=nid,
                    chapter_number=int(number),
                    source=source,
                    content=str(draft.get("content", "")),
                    summary=str(draft.get("summary", "")),
                    scene_plan=draft.get("scene_plan") or [],
                    scene_drafts=draft.get("scene_drafts") or [],
                )
        except Exception as exc:
            logger.warning("章节版本快照写入失败(%s)", type(exc).__name__)

    feedback = interrupt(
        {
            "type": "human_review",
            "chapter_number": number,
            "title": draft.get("title", ""),
            "summary": draft.get("summary", ""),
            "word_count": draft.get("word_count", 0),
            "content": str(draft.get("content", "")),
            "scene_plan": draft.get("scene_plan") or state.get("scene_plan") or [],
            "issues": state.get("issues") or [],
            "persistence_error": persistence_error,
            "instruction": retry_instruction if persistence_error else approval_instruction,
        }
    )

    scene_number = 0
    restore_version = 0
    restore_candidate = ""
    if isinstance(feedback, dict):
        raw_feedback = str(feedback.get("feedback", "")).strip()
        try:
            scene_number = int(feedback.get("scene_number") or 0)
        except TypeError, ValueError:
            scene_number = 0
        if str(feedback.get("action", "")) == "restore_version":
            try:
                restore_version = int(feedback.get("version_number") or 0)
            except TypeError, ValueError:
                restore_version = 0
        if str(feedback.get("action", "")) == "restore_candidate":
            restore_candidate = str(feedback.get("candidate_id", "")).strip()
    else:
        raw_feedback = str(feedback).strip()

    if isinstance(feedback, dict) and str(feedback.get("action", "")) == "canon_update":
        operation = feedback.get("operation")
        if not isinstance(operation, dict):
            raise ValueError("Canon operation 必须是对象")
        updated_canon = apply_canon_operation(
            ensure_canon(
                state.get("canon"),
                world_bible=str(state.get("world_bible", "")),
                characters=state.get("characters") or [],
                outline=state.get("outline") or [],
                chapters=state.get("chapters") or [],
            ),
            operation,
        )
        if nid:
            try:
                await asyncio.to_thread(
                    NovelMemory(nid).store_content,
                    format_canon(updated_canon, max_chars=6000),
                    metadata={"type": "canon", "version": updated_canon.get("version", 3)},
                    content_id=f"{nid}:canon",
                )
            except Exception as exc:
                logger.warning("人工 Canon 更新写入向量记忆失败(%s)", type(exc).__name__)
        updates: dict[str, Any] = {
            "canon": updated_canon,
            "current_phase": "consistency_check",
            "revision_notes": "",
            "revision_scene_number": 0,
            "persistence_error": "",
            "candidate_source_hash": "",
            "creative_brief_review_required": False,
        }
        if str(operation.get("action", "")).startswith(("upsert_thread", "update_thread")):
            current = int(state.get("current_chapter", number) or 0)
            beats = narrative_beats_for_chapter(updated_canon, current)
            chapter_plan = {**(state.get("chapter_plan") or {}), "narrative_beats": beats}
            try:
                preferred_scene = int(operation.get("scene_number") or 0)
            except TypeError, ValueError:
                preferred_scene = 0
            scene_plan = _assign_narrative_beats(
                state.get("scene_plan") or draft.get("scene_plan") or [],
                beats,
                preferred_scene,
            )
            updates["chapter_plan"] = chapter_plan
            updates["scene_plan"] = scene_plan
            updates["current_draft"] = {**draft, "scene_plan": scene_plan}
        return updates

    if isinstance(feedback, dict) and str(feedback.get("action", "")) == "creative_brief_update":
        return {
            "current_phase": "consistency_check",
            "revision_notes": "",
            "revision_scene_number": 0,
            "issues": [],
            "quality_report": {},
            "persistence_error": "",
            "candidate_source_hash": "",
            "creative_brief_review_required": False,
        }

    if restore_version > 0:
        store = _novel_store()
        version = store.get_chapter_version(nid, int(number), restore_version)
        if version is None:
            raise ValueError(f"章节版本 v{restore_version} 不存在")
        restored = {
            **draft,
            "content": str(version.get("content", "")),
            "summary": str(version.get("summary", draft.get("summary", ""))),
            "word_count": int(version.get("word_count", 0)),
            "status": "restored",
            "scene_plan": version.get("scene_plan") or draft.get("scene_plan") or [],
            "scene_drafts": version.get("scene_drafts") or [],
        }
        if not restored["scene_drafts"]:
            restored["scene_drafts"] = ensure_scene_drafts(restored)
        return {
            "current_draft": restored,
            "current_phase": "consistency_check",
            "revision_notes": "",
            "revision_scene_number": 0,
            "revision_count": int(state.get("revision_count", 0)) + 1,
            "persistence_error": "",
            "candidate_source_hash": "",
        }

    if restore_candidate:
        store = _novel_store()
        candidate = store.get_chapter_candidate(nid, restore_candidate)
        if candidate is None:
            raise ValueError("章节候选稿不存在")
        if int(candidate.get("chapter_number", 0) or 0) != int(number):
            raise ValueError("章节候选稿不属于当前章节")
        if not chapter_candidate_matches_state(candidate, state):
            raise ValueError("当前审查上下文已变化，请重新生成候选稿")
        restored = {
            **draft,
            "candidate_id": restore_candidate,
            "content": str(candidate.get("content", "")),
            "summary": str(candidate.get("summary", draft.get("summary", ""))),
            "word_count": len(str(candidate.get("content", ""))),
            "status": "candidate",
            "scene_plan": candidate.get("scene_plan") or draft.get("scene_plan") or [],
            "scene_drafts": candidate.get("scene_drafts") or [],
        }
        if not restored["scene_drafts"]:
            restored["scene_drafts"] = ensure_scene_drafts(restored)
        try:
            store.mark_chapter_candidate_selected(nid, int(number), restore_candidate)
        except Exception as exc:
            logger.warning("候选稿选择状态写入失败(%s)", type(exc).__name__)
        return {
            "current_draft": restored,
            "scene_plan": restored["scene_plan"],
            "current_phase": "consistency_check",
            "revision_notes": "",
            "revision_scene_number": 0,
            "revision_count": int(state.get("revision_count", 0)) + 1,
            "persistence_error": "",
            "candidate_source_hash": str(candidate.get("source_hash", "")),
        }

    approved = scene_number == 0 and raw_feedback.lower() in {
        "",
        "approve",
        "通过",
        "y",
        "yes",
    }
    if approved:
        # 定稿:chapters reducer 会为新章追加、为全书返修按章节号替换。
        try:
            digested_draft = await _digest_final_draft(state, draft)
        except Exception as exc:
            # 提炼失败时回到一个全新的 human_review 节点，确保 API 能看到可恢复的 next。
            return {
                "current_draft": draft,
                "current_phase": "human_review",
                "persistence_error": f"终稿事实提炼失败:{type(exc).__name__}",
            }
        final_draft = {**digested_draft, "status": "final"}
        memory_index = _memory_index_after_finalization(state, final_draft)
        base_canon = ensure_canon(
            state.get("canon"),
            world_bible=str(state.get("world_bible", "")),
            characters=state.get("characters") or [],
            outline=state.get("outline") or [],
            chapters=state.get("chapters") or [],
        )
        if book_revision_mode:
            updated_canon = replace_final_chapter(base_canon, final_draft)
            updated_outline = [dict(item) for item in state.get("outline") or []]
            replan_proposal = {
                "status": "stable",
                "impact": "medium",
                "rationale": "全书返修已替换指定终稿，保留其他已完成章节与现有大纲。",
                "outline_updates": [],
                "replan_version": "replan-v1",
                "outline": updated_outline,
            }
        else:
            updated_canon = record_final_chapter(base_canon, final_draft)
            updated_outline, replan_proposal, updated_canon = await _replan_after_finalization(
                state,
                final_draft,
                updated_canon,
            )
        next_chapter = (
            int(state.get("total_chapters", 0) or 0) + 1
            if book_revision_mode
            else int(state.get("current_chapter", 1)) + 1
        )
        updates: dict[str, Any] = {
            "chapters": [final_draft],
            "current_draft": final_draft,
            "canon": updated_canon,
            "outline": updated_outline,
            "replan_proposal": replan_proposal,
            "current_chapter": next_chapter,
            "current_phase": "writing",
            "scene_plan": [],
            "revision_count": 0,
            "revision_notes": "",
            "revision_scene_number": 0,
            "persistence_error": "",
            "book_revision_mode": False,
            "book_revision_origin_hash": "",
            "candidate_source_hash": "",
            "book_audit_completed": False if book_revision_mode else state.get("book_audit_completed", False),
            "memory_index": memory_index,
        }
        if nid:
            store = _novel_store()
            try:
                store.save_chapter(
                    novel_id=nid,
                    chapter_number=int(number),
                    title=str(final_draft.get("title", "")),
                    content=str(final_draft.get("content", "")),
                    summary=str(final_draft.get("summary", "")),
                    status="final",
                    scene_plan=final_draft.get("scene_plan") or [],
                    digest=_chapter_digest_payload(final_draft),
                )
                store.save_progress(
                    novel_id=nid,
                    current_chapter=next_chapter,
                    current_phase="writing",
                    state={
                        "canon": updated_canon,
                        "outline": updated_outline,
                        "replan_proposal": replan_proposal,
                        "memory_index": memory_index,
                    },
                )
            except Exception as exc:
                logger.exception("定稿持久化失败,保留人工审查检查点")
                return {
                    "current_draft": digested_draft,
                    "current_phase": "human_review",
                    "revision_notes": "",
                    "persistence_error": f"章节定稿持久化失败:{type(exc).__name__}",
                }
            try:
                if hasattr(store, "save_chapter_version"):
                    store.save_chapter_version(
                        novel_id=nid,
                        chapter_number=int(number),
                        source="book_revision_final" if book_revision_mode else "final",
                        content=str(final_draft.get("content", "")),
                        summary=str(final_draft.get("summary", "")),
                        scene_plan=final_draft.get("scene_plan") or [],
                        scene_drafts=final_draft.get("scene_drafts") or [],
                    )
            except Exception as exc:
                logger.warning("终稿版本快照写入失败(%s)", type(exc).__name__)
            try:
                if hasattr(store, "save_memory_snapshot"):
                    store.save_memory_snapshot(
                        nid,
                        schema_version=HIERARCHICAL_MEMORY_SCHEMA_VERSION,
                        content_hash=hierarchical_memory_hash(memory_index),
                        payload=memory_index,
                    )
            except Exception as exc:
                logger.warning("分层记忆快照写入失败(%s)", type(exc).__name__)
            if replan_proposal.get("status") == "replanned":
                try:
                    if hasattr(store, "save_planning_version"):
                        store.save_planning_version(
                            novel_id=nid,
                            artifact_type="blueprint",
                            chapter_number=0,
                            source="replanned",
                            payload={
                                "world_bible": str(state.get("world_bible", "")),
                                "characters": state.get("characters") or [],
                                "outline": updated_outline,
                                "replan": replan_proposal,
                            },
                        )
                except Exception as exc:
                    logger.warning("重规划版本快照写入失败(%s)", type(exc).__name__)
            try:
                memory = NovelMemory(nid)
                await asyncio.to_thread(
                    memory.store_content,
                    f"第{number}章 {final_draft.get('title', '')}:"
                    f"{final_draft.get('summary', '')}\n"
                    f"关键事件:{final_draft.get('events') or []}\n"
                    f"人物:{final_draft.get('characters') or []}\n"
                    f"地点:{final_draft.get('locations') or []}\n"
                    f"{str(final_draft.get('content', ''))[:1200]}",
                    metadata={"type": "chapter", "chapter": int(number), "status": "final"},
                    content_id=f"{nid}:chapter:{number}",
                )
                await asyncio.to_thread(
                    memory.store_content,
                    format_canon(updated_canon, max_chars=6000),
                    metadata={"type": "canon", "version": updated_canon.get("version", 3)},
                    content_id=f"{nid}:canon",
                )
                if hasattr(memory, "store_hierarchical_memory"):
                    await asyncio.to_thread(
                        memory.store_hierarchical_memory,
                        format_hierarchical_memory(
                            memory_index,
                            current_chapter=0,
                            max_chars=12000,
                        ),
                        content_hash=hierarchical_memory_hash(memory_index),
                    )
            except Exception as exc:
                logger.warning("终稿章节写入向量记忆失败(%s)", type(exc).__name__)
        return updates

    return {
        "revision_notes": raw_feedback,
        "revision_scene_number": scene_number,
        "current_phase": "writing",
        "revision_count": int(state.get("revision_count", 0)) + 1,
        "persistence_error": "",
    }
