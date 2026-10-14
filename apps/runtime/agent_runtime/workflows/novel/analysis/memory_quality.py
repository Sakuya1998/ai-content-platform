"""长期记忆检索质量评测与可重建索引工具。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from agent_runtime.workflows.novel.memory.canon import format_canon
from agent_runtime.workflows.novel.memory.hierarchical import hierarchical_memory_hash

MEMORY_QUALITY_SCHEMA_VERSION = "memory-quality-v1"


def _chapter_number(item: dict[str, Any]) -> int:
    return int(item.get("chapter_number", item.get("chapter", 0)) or 0)


def _text(value: Any, limit: int = 800) -> str:
    return " ".join(str(value or "").split())[:limit]


def build_memory_records(
    *,
    novel_id: str,
    world_bible: str = "",
    characters: list[dict[str, Any]] | None = None,
    outline: list[dict[str, Any]] | None = None,
    chapters: list[dict[str, Any]] | None = None,
    canon: dict[str, Any] | None = None,
    memory_index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """将结构化来源转换成带稳定 ID 的向量记录描述。"""
    records: list[dict[str, Any]] = []
    if world_bible.strip():
        records.append(
            {
                "id": f"{novel_id}:world_bible",
                "content": world_bible,
                "metadata": {"type": "world_bible", "title": "世界观圣经"},
            }
        )
    for index, character in enumerate(characters or [], start=1):
        name = str(character.get("name", "未知角色"))
        records.append(
            {
                "id": f"{novel_id}:character:{index}",
                "content": f"角色档案:{name}\n{character!r}",
                "metadata": {"type": "character", "name": name},
            }
        )
    for item in sorted(outline or [], key=_chapter_number):
        number = _chapter_number(item)
        if number <= 0:
            continue
        records.append(
            {
                "id": f"{novel_id}:outline:{number}",
                "content": f"第{number}章大纲:{item.get('summary', '')}",
                "metadata": {"type": "outline", "chapter": number},
            }
        )
    for chapter in sorted(chapters or [], key=_chapter_number):
        number = _chapter_number(chapter)
        if number <= 0:
            continue
        records.append(
            {
                "id": f"{novel_id}:chapter:{number}",
                "content": (
                    f"第{number}章 {chapter.get('title', '')}:{chapter.get('summary', '')}\n"
                    f"关键事件:{chapter.get('events') or []}\n"
                    f"人物:{chapter.get('characters') or []}\n"
                    f"地点:{chapter.get('locations') or []}\n"
                    f"{str(chapter.get('content', ''))[:1200]}"
                ),
                "metadata": {"type": "chapter", "chapter": number, "status": chapter.get("status", "final")},
            }
        )
    if canon:
        records.append(
            {
                "id": f"{novel_id}:canon",
                "content": format_canon(canon, max_chars=6000),
                "metadata": {"type": "canon", "version": canon.get("version", 3)},
            }
        )
    if memory_index and memory_index.get("chapters"):
        records.append(
            {
                "id": f"{novel_id}:hierarchical-memory",
                "content": _text(memory_index.get("book_summary"), 12000),
                "metadata": {
                    "type": "hierarchical_memory",
                    "schema_version": memory_index.get("schema_version", ""),
                    "content_hash": hierarchical_memory_hash(memory_index),
                },
            }
        )
    return records


def build_memory_eval_cases(
    *,
    novel_id: str,
    world_bible: str = "",
    characters: list[dict[str, Any]] | None = None,
    outline: list[dict[str, Any]] | None = None,
    chapters: list[dict[str, Any]] | None = None,
    canon: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """从当前来源生成稳定的检索评测样本。"""
    cases: list[dict[str, Any]] = []
    if world_bible.strip():
        cases.append(
            {
                "id": "world_bible",
                "category": "world_bible",
                "query": "世界观设定与历史背景",
                "expected_ids": [f"{novel_id}:world_bible"],
                "expected_types": ["world_bible"],
            }
        )
    for index, character in enumerate(characters or [], start=1):
        name = str(character.get("name", "未知角色"))
        query = _text(f"角色 {name} {character.get('role', '')} {character.get('personality', '')}", 500)
        cases.append(
            {
                "id": f"character:{index}",
                "category": "character",
                "query": query,
                "expected_ids": [f"{novel_id}:character:{index}"],
                "expected_types": ["character"],
            }
        )
    for item in sorted(outline or [], key=_chapter_number):
        number = _chapter_number(item)
        if number <= 0 or not str(item.get("summary", "")).strip():
            continue
        cases.append(
            {
                "id": f"outline:{number}",
                "category": "outline",
                "query": _text(f"第{number}章 {item.get('summary', '')}", 700),
                "expected_ids": [f"{novel_id}:outline:{number}"],
                "expected_types": ["outline"],
            }
        )
    for chapter in sorted(chapters or [], key=_chapter_number):
        number = _chapter_number(chapter)
        summary = str(chapter.get("summary", "")).strip()
        if number <= 0 or not summary:
            continue
        cases.append(
            {
                "id": f"chapter:{number}",
                "category": "chapter_summary",
                "query": _text(f"第{number}章 {chapter.get('title', '')} {summary}", 700),
                "expected_ids": [f"{novel_id}:chapter:{number}"],
                "expected_types": ["chapter"],
            }
        )
    if canon and (canon.get("facts") or canon.get("world_facts") or canon.get("narrative_threads")):
        cases.append(
            {
                "id": "canon",
                "category": "canon",
                "query": "权威设定、角色事实、时间线与叙事线程",
                "expected_ids": [f"{novel_id}:canon"],
                "expected_types": ["canon"],
            }
        )
    return cases


def _record_id(hit: dict[str, Any]) -> str:
    return str(hit.get("id") or (hit.get("metadata") or {}).get("_memory_id") or "")


def evaluate_memory_retrieval(
    *,
    memory: Any,
    cases: list[dict[str, Any]],
    k: int = 5,
) -> dict[str, Any]:
    """执行检索评测并返回可持久化报告。"""
    limit = max(1, min(int(k), 20))
    case_reports: list[dict[str, Any]] = []
    category_stats: dict[str, list[dict[str, float]]] = defaultdict(list)
    errors: list[str] = []
    total_hits = 0
    stale_hits = 0
    canon_cases = 0
    canon_conflicts = 0
    try:
        records = memory.list_records()
    except Exception as exc:
        records = []
        errors.append(f"无法读取向量记录:{type(exc).__name__}")
    try:
        index_hash = hashlib.sha256(
            json.dumps(records, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except Exception:
        index_hash = ""
    for case in cases:
        hits: list[dict[str, Any]] = []
        error = ""
        try:
            hits = list(memory.search_similar(str(case.get("query", "")), k=limit) or [])
        except Exception as exc:
            error = f"检索失败:{type(exc).__name__}"
            errors.append(f"{case.get('id', '')}:{error}")
        expected = {str(item) for item in case.get("expected_ids") or []}
        hit_ids = [_record_id(hit) for hit in hits]
        rank = next((index + 1 for index, item in enumerate(hit_ids) if item in expected), 0)
        hit = bool(rank)
        relevant = sum(item in expected for item in hit_ids)
        stale = sum(
            1
            for item in hits
            if str((item.get("metadata") or {}).get("status", "")).casefold() in {"deprecated", "stale", "draft"}
            or bool((item.get("metadata") or {}).get("deprecated"))
        )
        total_hits += len(hits)
        stale_hits += stale
        if case.get("category") == "canon":
            canon_cases += 1
            if not hit and hits and not any((item.get("metadata") or {}).get("type") == "canon" for item in hits):
                canon_conflicts += 1
        case_report = {
            "id": case.get("id", ""),
            "category": case.get("category", "unknown"),
            "query": case.get("query", ""),
            "expected_ids": sorted(expected),
            "retrieved_ids": hit_ids,
            "hit": hit,
            "rank": rank,
            "precision_at_k": relevant / len(hit_ids) if hit_ids else 0.0,
            "recall_at_k": 1.0 if hit else 0.0,
            "error": error,
        }
        case_reports.append(case_report)
        category_stats[str(case_report["category"])].append(
            {
                "precision": float(case_report["precision_at_k"]),
                "recall": float(case_report["recall_at_k"]),
                "mrr": 1.0 / rank if rank else 0.0,
            }
        )
    count = len(case_reports)
    recall = sum(item["recall_at_k"] for item in case_reports) / count if count else 0.0
    precision = sum(item["precision_at_k"] for item in case_reports) / count if count else 0.0
    mrr = sum((1.0 / item["rank"] if item["rank"] else 0.0) for item in case_reports) / count if count else 0.0
    category_metrics = {
        category: {
            "case_count": len(items),
            "recall_at_k": sum(item["recall"] for item in items) / len(items),
            "precision_at_k": sum(item["precision"] for item in items) / len(items),
            "mrr": sum(item["mrr"] for item in items) / len(items),
        }
        for category, items in category_stats.items()
    }
    return {
        "schema_version": MEMORY_QUALITY_SCHEMA_VERSION,
        "k": limit,
        "case_count": count,
        "passed_cases": sum(bool(item["hit"]) for item in case_reports),
        "index_record_count": len(records),
        "index_hash": index_hash,
        "recall_at_k": recall,
        "precision_at_k": precision,
        "mrr": mrr,
        "stale_fact_hit_rate": stale_hits / total_hits if total_hits else 0.0,
        "canon_vector_conflict_rate": canon_conflicts / canon_cases if canon_cases else 0.0,
        "category_metrics": category_metrics,
        "cases": case_reports,
        "errors": errors,
        "status": "passed" if count and not errors and recall >= 0.8 else "attention",
    }


def rebuild_memory_index(memory: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    """重建向量索引；任一写入失败时恢复重建前的完整记录。"""
    previous = list(memory.list_records() or [])

    def write(items: list[dict[str, Any]]) -> None:
        for record in items:
            memory.store_content(
                str(record.get("content", "")),
                metadata=record.get("metadata") or {},
                content_id=str(record.get("id", "")),
            )

    try:
        memory.clear()
        write(records)
    except Exception:
        # Backends without an atomic rebuild need the previous records for rollback.
        # Recreate the old contents before surfacing the original failure so a
        # transient embedding/provider error cannot leave a half-built index.
        try:
            memory.clear()
            write(previous)
        except Exception as rollback_error:
            raise RuntimeError(f"向量记忆重建失败且回滚失败:{type(rollback_error).__name__}") from rollback_error
        raise
    digest = hashlib.sha256(
        json.dumps(
            [{"id": item.get("id"), "metadata": item.get("metadata")} for item in records],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": MEMORY_QUALITY_SCHEMA_VERSION,
        "record_count": len(records),
        "index_hash": digest,
        "ids": [str(item.get("id", "")) for item in records],
    }
