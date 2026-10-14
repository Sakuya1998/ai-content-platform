"""场景正文的分段、格式化与重组工具。"""

import re
from typing import Any

_SCENE_MARKER = re.compile(r"(?m)^[ \t]*<<<SCENE:(\d+)>>>[ \t]*$")


def _scene_numbers(scene_plan: list[dict[str, Any]]) -> list[int]:
    return [int(item.get("scene_number", index)) for index, item in enumerate(scene_plan, 1)]


def _scene_weights(scene_plan: list[dict[str, Any]]) -> list[int]:
    weights: list[int] = []
    for item in scene_plan:
        try:
            weights.append(max(int(item.get("estimated_words") or 1), 1))
        except TypeError, ValueError:
            weights.append(1)
    return weights


def segment_scene_content(
    content: str,
    scene_plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把模型输出切分为与场景计划一一对应的正文段。

    优先读取 ``<<<SCENE:n>>>`` 标记。旧模型输出或旧检查点没有标记时，
    按场景字数权重确定性切分，保证局部重写仍有稳定的替换边界。
    """
    if not scene_plan:
        return []

    text = str(content or "").strip()
    numbers = _scene_numbers(scene_plan)
    matches = list(_SCENE_MARKER.finditer(text))
    if matches and [int(match.group(1)) for match in matches] == numbers:
        return [
            {
                "scene_number": number,
                "content": text[match.end() : matches[index + 1].start()].strip()
                if index + 1 < len(matches)
                else text[match.end() :].strip(),
            }
            for index, (number, match) in enumerate(zip(numbers, matches, strict=True))
        ]

    clean_text = _SCENE_MARKER.sub("", text).strip()
    if len(scene_plan) == 1:
        return [{"scene_number": numbers[0], "content": clean_text}]

    weights = _scene_weights(scene_plan)
    total_weight = sum(weights)
    drafts: list[dict[str, Any]] = []
    start = 0
    cumulative = 0
    for index, (number, weight) in enumerate(zip(numbers, weights, strict=True)):
        cumulative += weight
        end = len(clean_text) if index == len(numbers) - 1 else round(len(clean_text) * cumulative / total_weight)
        drafts.append({"scene_number": number, "content": clean_text[start:end].strip()})
        start = end
    return drafts


def ensure_scene_drafts(chapter: dict[str, Any]) -> list[dict[str, Any]]:
    """读取有效场景稿；缺失或损坏时从章节全文重建。"""
    scene_plan = chapter.get("scene_plan") or []
    numbers = _scene_numbers(scene_plan)
    raw_drafts = chapter.get("scene_drafts") or []
    if isinstance(raw_drafts, list):
        try:
            draft_numbers = [int(item.get("scene_number", 0)) for item in raw_drafts]
        except AttributeError, TypeError, ValueError:
            draft_numbers = []
        if draft_numbers == numbers and all(isinstance(item.get("content"), str) for item in raw_drafts):
            return [
                {"scene_number": number, "content": str(item["content"]).strip()}
                for number, item in zip(numbers, raw_drafts, strict=True)
            ]
    return segment_scene_content(str(chapter.get("content", "")), scene_plan)


def format_scene_drafts(scene_drafts: list[dict[str, Any]]) -> str:
    """生成供模型处理的带边界场景正文。"""
    return "\n\n".join(
        f"<<<SCENE:{int(item.get('scene_number', index))}>>>\n{str(item.get('content', '')).strip()}"
        for index, item in enumerate(scene_drafts, 1)
    )


def join_scene_drafts(scene_drafts: list[dict[str, Any]]) -> str:
    """合并为面向读者的无标记章节正文。"""
    return "\n\n".join(
        str(item.get("content", "")).strip() for item in scene_drafts if str(item.get("content", "")).strip()
    )
