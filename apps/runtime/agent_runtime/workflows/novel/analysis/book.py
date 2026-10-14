"""全书级确定性审计指标。"""

import hashlib
import json
import math
from collections import Counter
from typing import Any

BOOK_AUDIT_SCHEMA_VERSION = "book-audit-v1"
BOOK_AUDIT_RUBRIC_VERSION = "book-literary-audit-v1"


def manuscript_hash(chapters: list[dict[str, Any]]) -> str:
    payload = [
        {
            "chapter": int(item.get("chapter_number", item.get("chapter", 0)) or 0),
            "title": str(item.get("title", "")),
            "content": str(item.get("content", "")),
        }
        for item in sorted(
            chapters,
            key=lambda value: int(value.get("chapter_number", value.get("chapter", 0)) or 0),
        )
    ]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def evaluate_book_deterministic(
    *,
    chapters: list[dict[str, Any]],
    canon: dict[str, Any] | None,
    total_chapters: int,
) -> dict[str, Any]:
    """评估章节完整性、线程回收、角色覆盖、时间线和全书均衡度。"""
    ordered = sorted(
        chapters,
        key=lambda item: int(item.get("chapter_number", item.get("chapter", 0)) or 0),
    )
    numbers = [int(item.get("chapter_number", item.get("chapter", 0)) or 0) for item in ordered]
    expected = list(range(1, total_chapters + 1))
    completion_score = (
        100.0 if numbers == expected else _clamp(len(set(numbers) & set(expected)) / max(total_chapters, 1) * 100)
    )

    threads = (canon or {}).get("narrative_threads") or []
    open_major = sum(
        item.get("priority") == "major" and item.get("status", "planned") not in {"resolved", "abandoned"}
        for item in threads
    )
    open_minor = sum(
        item.get("priority") != "major" and item.get("status", "planned") not in {"resolved", "abandoned"}
        for item in threads
    )
    thread_score = _clamp(100 - open_major * 35 - open_minor * 10)

    characters = (canon or {}).get("characters") or {}
    covered_characters = sum(bool(item.get("appearances")) for item in characters.values())
    character_score = _clamp(covered_characters / len(characters) * 100) if characters else 100.0

    final_timeline = [item for item in (canon or {}).get("timeline") or [] if item.get("status") == "final"]
    timeline_numbers = [int(item.get("chapter", 0) or 0) for item in final_timeline]
    timeline_score = (
        100.0
        if sorted(timeline_numbers) == expected
        else _clamp(len(set(timeline_numbers) & set(expected)) / max(total_chapters, 1) * 100)
    )

    lengths = [len(str(item.get("content", "")).strip()) for item in ordered]
    if len(lengths) <= 1 or not any(lengths):
        balance_score = 100.0 if lengths and lengths[0] > 0 else 0.0
    else:
        mean = sum(lengths) / len(lengths)
        variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
        coefficient = math.sqrt(variance) / max(mean, 1)
        balance_score = _clamp(100 - min(coefficient * 100, 70))

    summaries = [" ".join(str(item.get("summary", "")).casefold().split()) for item in ordered]
    summaries = [item for item in summaries if item]
    duplicate_summaries = sum(max(count - 1, 0) for count in Counter(summaries).values())
    repetition_score = _clamp(100 - duplicate_summaries * 25)

    dimensions = {
        "chapter_completion": (
            completion_score,
            f"终稿章节 {numbers}，预期 {expected}",
        ),
        "narrative_resolution": (
            thread_score,
            f"未解决主要线程 {open_major}，次要线程 {open_minor}",
        ),
        "character_coverage": (
            character_score,
            f"有出场记录角色 {covered_characters}/{len(characters)}",
        ),
        "timeline_integrity": (
            timeline_score,
            f"终稿时间线章节 {sorted(timeline_numbers)}",
        ),
        "chapter_balance": (
            balance_score,
            f"章节字数 {lengths}",
        ),
        "summary_repetition": (
            repetition_score,
            f"重复章节摘要 {duplicate_summaries}",
        ),
    }
    scores = {name: score for name, (score, _) in dimensions.items()}
    return {
        "schema_version": BOOK_AUDIT_SCHEMA_VERSION,
        "scores": scores,
        "overall_score": round(sum(scores.values()) / len(scores), 1),
        "findings": [
            {
                "dimension": name,
                "score": score,
                "message": message,
                "source": "deterministic",
            }
            for name, (score, message) in dimensions.items()
        ],
    }
