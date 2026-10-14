"""场景规划 Agent:把章节大纲拆解为结构化场景执行计划。"""

import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from agent_runtime.execution.models import get_analyzer_llm
from agent_runtime.execution.structured import invoke_structured, parse_yaml_block
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.memory.canon import format_canon, narrative_beats_from_chapter
from agent_runtime.workflows.novel.resources.brief import format_creative_brief

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = {"goal", "conflict", "turn", "location", "emotion"}


def _target_words(chapter_plan: dict[str, Any], max_chapter_words: int) -> int:
    maximum = max(int(max_chapter_words or 6000), 1)
    try:
        requested = int(chapter_plan.get("estimated_words") or maximum)
    except TypeError, ValueError:
        requested = maximum
    return min(max(requested, 1), maximum)


def _normalize_word_budgets(scenes: list[dict[str, Any]], target_words: int) -> None:
    if not scenes:
        return
    effective_target = max(target_words, len(scenes))
    weights: list[int] = []
    for scene in scenes:
        try:
            weights.append(max(int(scene.get("estimated_words") or 1), 1))
        except TypeError, ValueError:
            weights.append(1)

    total_weight = sum(weights)
    budgets = [max(round(effective_target * weight / total_weight), 1) for weight in weights]
    difference = effective_target - sum(budgets)
    while difference:
        if difference > 0:
            index = max(range(len(weights)), key=weights.__getitem__)
            budgets[index] += 1
            difference -= 1
            continue
        candidates = [index for index, budget in enumerate(budgets) if budget > 1]
        if not candidates:
            break
        index = max(candidates, key=lambda item: budgets[item])
        budgets[index] -= 1
        difference += 1

    for scene, budget in zip(scenes, budgets, strict=True):
        scene["estimated_words"] = budget


def fallback_scene_plan(chapter_plan: dict[str, Any], target_words: int) -> list[dict[str, Any]]:
    """为旧检查点或缺失计划提供可执行的单场景兼容方案。"""
    characters = chapter_plan.get("characters") or []
    if isinstance(characters, str):
        characters = [characters]
    return [
        {
            "scene_number": 1,
            "goal": str(chapter_plan.get("summary") or "完成本章大纲目标"),
            "conflict": str(chapter_plan.get("conflict") or "推动本章核心冲突"),
            "turn": str(chapter_plan.get("cliffhanger") or "形成通往下一章的新问题"),
            "location": str(chapter_plan.get("location") or "依据正文情境确定"),
            "characters": list(characters),
            "emotion": str(chapter_plan.get("emotion") or "递进"),
            "estimated_words": max(int(target_words), 1),
            "entry_hook": "承接上一章结尾进入当前冲突",
            "exit_hook": str(chapter_plan.get("cliffhanger") or "以悬念结束本章"),
            "narrative_beats": narrative_beats_from_chapter(chapter_plan),
        }
    ]


def _beat_key(beat: dict[str, Any]) -> tuple[str, str]:
    return (
        " ".join(str(beat.get("thread", "")).casefold().split()),
        str(beat.get("action", "")).casefold(),
    )


def validate_scene_plan(
    items: list[dict[str, Any]],
    chapter_plan: dict[str, Any],
) -> None:
    """校验场景顺序、必要字段与叙事 beat 的唯一分配。"""
    if not 1 <= len(items) <= 8:
        raise ValueError("场景数量必须在 1 到 8 之间")
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("每个场景都必须是对象")
    numbers = [int(item.get("scene_number", 0) or 0) for item in items]
    if numbers != list(range(1, len(items) + 1)):
        raise ValueError("scene_number 必须从 1 开始连续编号")
    for item in items:
        missing = [field for field in _REQUIRED_FIELDS if not str(item.get(field, "")).strip()]
        if missing:
            raise ValueError(f"场景 {item.get('scene_number')} 缺少字段:{sorted(missing)}")
        if not isinstance(item.get("characters"), list):
            raise ValueError(f"场景 {item.get('scene_number')} 的 characters 必须是列表")
    expected_beats = [_beat_key(beat) for beat in narrative_beats_from_chapter(chapter_plan)]
    assigned_beats: list[tuple[str, str]] = []
    for item in items:
        raw_beats = item.get("narrative_beats") or []
        if not isinstance(raw_beats, list):
            raise ValueError(f"场景 {item.get('scene_number')} 的 narrative_beats 必须是列表")
        for beat in raw_beats:
            if not isinstance(beat, dict):
                raise ValueError("场景 narrative beat 必须是对象")
            assigned_beats.append(_beat_key(beat))
    if sorted(assigned_beats) != sorted(expected_beats):
        raise ValueError(
            "章节 narrative_beats 必须各自分配到且只分配到一个场景; "
            f"期望分配={expected_beats!r}; 实际分配={assigned_beats!r}"
        )


def normalize_scene_plan(
    items: list[dict[str, Any]],
    chapter_plan: dict[str, Any],
    max_chapter_words: int,
) -> list[dict[str, Any]]:
    """复制、排序并归一化一个已验证的场景计划。"""
    scenes = [dict(item) for item in items]
    scenes.sort(key=lambda item: int(item.get("scene_number", 0) or 0))
    validate_scene_plan(scenes, chapter_plan)
    _normalize_word_budgets(scenes, _target_words(chapter_plan, max_chapter_words))
    return scenes


class ScenePlannerAgent:
    """专业分镜师:为 SceneWriter 生成有因果关系的场景序列。"""

    def __init__(self, llm: BaseChatModel | None = None):
        self.llm = llm or get_analyzer_llm()

    async def plan_chapter(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        chapter_plan: dict[str, Any] = state.get("chapter_plan") or {}
        number = int(state.get("current_chapter", chapter_plan.get("chapter", 1)) or 1)
        target_words = _target_words(
            chapter_plan,
            int(state.get("max_chapter_words") or 6000),
        )
        chapters = state.get("chapters") or []
        previous_ending = "这是第一章,无需衔接。"
        if chapters:
            previous_ending = str(chapters[-1].get("content", ""))[-600:] or "(上一章为空)"

        prompt = fill_template(
            "scene_planner",
            chapter_number=number,
            target_words=target_words,
            chapter_plan=chapter_plan,
            canon_context=format_canon(
                state.get("canon"),
                max_chars=3000,
                current_chapter=number,
            ),
            creative_brief=format_creative_brief(state.get("creative_brief")),
            previous_ending=previous_ending,
        )
        logger.info("ScenePlannerAgent 开始规划第 %s 章场景", number)

        _, scenes = await invoke_structured(
            self.llm,
            prompt,
            parser=parse_yaml_block,
            validator=lambda items: validate_scene_plan(items, chapter_plan),
            agent_name=type(self).__name__,
            format_name="YAML",
        )
        return normalize_scene_plan(scenes, chapter_plan, int(state.get("max_chapter_words") or 6000))
