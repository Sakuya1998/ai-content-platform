"""把确定性一致性问题展开为可审阅的 Canon 冲突解释。

该模块只生成建议，不修改检查点或 Canon。建议中的 Canon 操作仍需通过
现有人工审查任务执行。
"""

from typing import Any


def _chapter_number(chapter: dict[str, Any]) -> int:
    return int(chapter.get("chapter_number", chapter.get("chapter", 0)) or 0)


def _record(label: str, value: Any, *, chapter: int | None = None, record_id: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if chapter:
        item["chapter"] = chapter
    if record_id:
        item["id"] = record_id
    return item


def _thread_for_issue(issue: dict[str, Any], canon: dict[str, Any]) -> dict[str, Any] | None:
    thread_id = str(issue.get("thread_id") or "")
    description = str(issue.get("description", ""))
    for thread in canon.get("narrative_threads") or []:
        if thread_id and str(thread.get("id", "")) == thread_id:
            return thread
        title = str(thread.get("title", ""))
        if title and f"「{title}」" in description:
            return thread
    return None


def _timeline_records(
    issue: dict[str, Any], chapter: dict[str, Any], previous: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    number = _chapter_number(chapter)
    current = chapter.get("time_days")
    prior = [item for item in previous if item.get("time_days") is not None and _chapter_number(item) < number]
    previous_item = max(prior, key=_chapter_number, default=None)
    records = []
    if previous_item is not None:
        records.append(
            _record(
                f"第{_chapter_number(previous_item)}章时间标记",
                previous_item.get("time_days"),
                chapter=_chapter_number(previous_item),
            )
        )
    records.append(_record(f"第{number}章时间标记", current, chapter=number))
    if not records and issue.get("description"):
        records.append(_record("诊断描述", issue["description"]))
    return records


def explain_consistency_issues(
    *,
    issues: list[dict[str, Any]],
    chapter: dict[str, Any],
    previous_chapters: list[dict[str, Any]],
    canon: dict[str, Any] | None = None,
    outline: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """将诊断问题转成前端可直接展示和采纳的解释对象。"""
    canon = canon or {}
    outline = outline or []
    number = _chapter_number(chapter)
    explanations: list[dict[str, Any]] = []
    for index, issue in enumerate(issues, start=1):
        issue_type = str(issue.get("type", "consistency"))
        thread = _thread_for_issue(issue, canon)
        evidence: list[dict[str, Any]] = []
        conflicting: list[dict[str, Any]] = []
        impact = "影响当前章节的可审查性，建议在定稿前处理。"
        repair_options: list[dict[str, Any]] = []

        if issue_type == "timeline":
            evidence = _timeline_records(issue, chapter, previous_chapters)
            conflicting = evidence[:]
            impact = "可能改变事件先后、角色可达性和后续章节的时间推断。"
            repair_options = [
                {
                    "id": "rewrite_timeline",
                    "label": "返修正文或时间标记",
                    "kind": "revision_feedback",
                    "feedback": str(issue.get("suggestion", "修正本章时间标记或与前章的事件顺序")),
                },
                {
                    "id": "review_canon_timeline",
                    "label": "人工确认 Canon 时间线",
                    "kind": "canon_operation",
                    "reason": "人工确认章节时间线冲突后更新 Canon",
                },
            ]
        elif issue_type in {"narrative_thread_overdue", "narrative_thread_unresolved"} and thread:
            due = thread.get("due_chapter")
            evidence = [
                _record("叙事线程", thread.get("title", "未命名线程"), record_id=str(thread.get("id", ""))),
                _record("当前状态", thread.get("status", "planned")),
                _record("计划截止章", due or "未定", chapter=int(due or 0) or None),
                _record("当前章节", number, chapter=number),
            ]
            conflicting = [_record("Canon 线程记录", thread, record_id=str(thread.get("id", "")))]
            impact = "线程债务会继续累积，并可能导致终章回收不足或读者无法获得因果闭环。"
            repair_options = [
                {
                    "id": "resolve_thread_in_text",
                    "label": "返修正文并完成回收",
                    "kind": "revision_feedback",
                    "feedback": str(issue.get("suggestion", "在本章安排明确回收")),
                },
                {
                    "id": "adjust_thread_due",
                    "label": "调整线程截止章",
                    "kind": "canon_operation",
                    "operation": {
                        "action": "upsert_thread",
                        "title": thread.get("title"),
                        "description": thread.get("description", ""),
                        "priority": thread.get("priority", "minor"),
                        "status": thread.get("status", "planned"),
                        "introduced_chapter": thread.get("introduced_chapter", 1),
                        "due_chapter": max(number, int(due or number)),
                        "reason": "人工确认叙事线程截止章需要顺延",
                    },
                },
                {
                    "id": "abandon_thread",
                    "label": "标记线程为 abandoned",
                    "kind": "canon_operation",
                    "operation": {
                        "action": "update_thread_status",
                        "target_id": thread.get("id"),
                        "status": "abandoned",
                        "resolved_chapter": number,
                        "reason": "人工确认该叙事线程不再回收",
                    },
                },
            ]
        elif issue_type == "narrative_beat_missing":
            evidence = [_record("本章诊断", issue.get("description", ""), chapter=number)]
            impact = "大纲中的承诺没有落到正文，可能造成剧情推进断档。"
            repair_options = [
                {
                    "id": "restore_beat",
                    "label": "按大纲补回叙事 beat",
                    "kind": "revision_feedback",
                    "feedback": str(issue.get("suggestion", "按大纲补回该叙事 beat")),
                }
            ]
        elif issue_type == "narrative_beat_scene_coverage":
            evidence = [
                _record("章节 beat", chapter.get("narrative_beats") or [], chapter=number),
                _record("场景分配", chapter.get("scene_plan") or [], chapter=number),
            ]
            impact = "章节级叙事承诺与场景执行不一致，可能导致写作阶段遗漏或重复。"
            repair_options = [
                {
                    "id": "align_scene_plan",
                    "label": "返修场景计划分配",
                    "kind": "revision_feedback",
                    "feedback": str(issue.get("suggestion", "确保每个章节叙事 beat 恰好分配到一个场景")),
                }
            ]
        elif issue_type == "creative_brief_boundary":
            evidence = [_record("命中的回避约束", issue.get("description", ""), chapter=number)]
            impact = "正文触碰创作边界，可能违反作品的受众、题材或内容约束。"
            repair_options = [
                {
                    "id": "rewrite_boundary",
                    "label": "删除或改写命中内容",
                    "kind": "revision_feedback",
                    "feedback": str(issue.get("suggestion", "删除或改写相关内容")),
                }
            ]
        elif issue_type == "creative_brief_requirement":
            evidence = [_record("缺失的必备约束", issue.get("description", ""), chapter=number)]
            impact = "全书终稿未兑现创作约束中的必备项。"
            repair_options = [
                {
                    "id": "add_requirement",
                    "label": "返修终章补回必备项",
                    "kind": "revision_feedback",
                    "feedback": str(issue.get("suggestion", "兑现该必备项")),
                }
            ]
        else:
            evidence = [_record("诊断结果", issue.get("description", ""), chapter=number)]
            repair_options = [
                {
                    "id": "follow_suggestion",
                    "label": "按诊断建议返修",
                    "kind": "revision_feedback",
                    "feedback": str(issue.get("suggestion", "请根据诊断结果返修")),
                }
            ]

        explanations.append(
            {
                "conflict_id": f"{issue_type}:{number}:{index}",
                "type": issue_type,
                "title": issue.get("description", issue_type),
                "severity": issue.get("severity", "low"),
                "description": issue.get("description", ""),
                "evidence": evidence,
                "conflicting_records": conflicting,
                "impact": impact,
                "repair_options": repair_options,
                "source": issue.get("source", "deterministic"),
                "chapter": number,
            }
        )
    return explanations
