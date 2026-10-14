"""Generate independent alternatives for a chapter awaiting human review."""

import hashlib
import inspect
import json
from typing import Any

from agent_runtime.execution.model_context import model_call_context
from agent_runtime.workflows.novel.agents.scene_writer import SceneWriterAgent
from agent_runtime.workflows.novel.agents.style_editor import StyleEditorAgent
from agent_runtime.workflows.novel.analysis.evaluation import evaluate_chapter_deterministic
from agent_runtime.workflows.novel.resources.brief import normalize_creative_brief

_VARIATION_DIRECTIONS = (
    "强化因果推进和场景之间的转折，让每个行动都产生清晰后果。",
    "强化人物选择、潜台词和关系张力，让冲突更多来自角色本身。",
    "强化氛围、感官细节和情绪递进，同时保持叙事紧凑。",
    "强化节奏变化、章节钩子和关键揭示，避免重复当前稿的表达路径。",
)


def chapter_candidate_source_hash(state: dict[str, Any]) -> str:
    """Hash the review context so candidates cannot be restored after it changes."""
    draft = state.get("current_draft") or {}
    payload = {
        "chapter": int(draft.get("chapter_number", state.get("current_chapter", 0)) or 0),
        "content": str(draft.get("content", "")),
        "chapter_plan": state.get("chapter_plan") or {},
        "scene_plan": draft.get("scene_plan") or state.get("scene_plan") or [],
        "canon": state.get("canon") or {},
        "creative_brief": normalize_creative_brief(state.get("creative_brief")),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def chapter_candidate_matches_state(
    candidate: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    """Allow switching among siblings while rejecting candidates from older edits."""
    source_hash = str(candidate.get("source_hash", ""))
    return bool(source_hash) and source_hash in {
        chapter_candidate_source_hash(state),
        str(state.get("candidate_source_hash", "")),
    }


class ChapterCandidateAgent:
    """Create one polished and deterministically scored chapter alternative."""

    def __init__(self, novel_id: str = ""):
        self.novel_id = novel_id

    async def generate(
        self,
        state: dict[str, Any],
        *,
        candidate_number: int,
        total_candidates: int,
        instruction: str = "",
    ) -> dict[str, Any]:
        direction = _VARIATION_DIRECTIONS[(max(candidate_number, 1) - 1) % len(_VARIATION_DIRECTIONS)]
        notes = [
            f"请生成候选稿 {candidate_number}/{total_candidates}。",
            direction,
            "保留既定场景计划、Canon 事实和本章必须完成的叙事 beat。",
            "不要只做同义改写，应提供明显不同但完整可用的正文方案。",
        ]
        if instruction.strip():
            notes.append(f"用户额外要求：{instruction.strip()}")
        candidate_state = {
            **state,
            "revision_notes": "\n".join(notes),
            "revision_scene_number": 0,
        }

        with model_call_context(self.novel_id, "chapter_candidate_writer"):
            draft = await SceneWriterAgent(novel_id=self.novel_id).write_chapter(candidate_state)
        with model_call_context(self.novel_id, "chapter_candidate_style_editor"):
            editor = StyleEditorAgent()
            polish = editor.polish
            style = str(state.get("style", ""))
            brief = state.get("creative_brief")
            try:
                inspect.signature(polish).bind(draft, style, brief)
            except TypeError:
                # Keep compatibility with older integrations that monkeypatch the two-argument hook.
                polished = await polish(draft, style)
            else:
                polished = await polish(draft, style, brief)
        evaluation = evaluate_chapter_deterministic(polished)
        return {
            **polished,
            "candidate_number": candidate_number,
            "scores": evaluation.get("scores") or {},
            "overall_score": float(evaluation.get("overall_score", 0.0)),
            "evaluation_schema_version": evaluation.get("schema_version", ""),
        }
