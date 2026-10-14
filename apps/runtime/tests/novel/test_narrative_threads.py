"""叙事线程确定性诊断测试。"""

from agent_runtime.workflows.novel.analysis.conflicts import explain_consistency_issues
from agent_runtime.workflows.novel.analysis.consistency import build_narrative_thread_diagnostics


def _thread(status="open", due=2, priority="major"):
    return {
        "id": "thread:king-seal",
        "title": "失踪王印",
        "priority": priority,
        "status": status,
        "introduced_chapter": 1,
        "due_chapter": due,
        "beats": [
            {"chapter": 1, "action": "setup", "description": "发现空印盒"},
            {"chapter": 2, "action": "resolve", "description": "揭示王印藏处"},
        ],
    }


def test_overdue_major_thread_is_a_deterministic_high_issue():
    issues, report = build_narrative_thread_diagnostics(
        canon={"narrative_threads": [_thread()]},
        chapter={"chapter_number": 3, "content": "正文", "scene_plan": []},
        total_chapters=5,
    )

    overdue = next(item for item in issues if item["type"] == "narrative_thread_overdue")
    assert overdue["severity"] == "high"
    assert overdue["thread_id"] == "thread:king-seal"
    assert "失踪王印" in report


def test_current_chapter_beat_must_match_scene_assignment():
    chapter = {
        "chapter_number": 2,
        "narrative_beats": [
            {
                "thread": "失踪王印",
                "action": "resolve",
                "description": "揭示王印藏处",
            }
        ],
        "scene_plan": [],
    }
    issues, _ = build_narrative_thread_diagnostics(
        canon={"narrative_threads": [_thread()]},
        chapter=chapter,
        total_chapters=5,
    )

    assert {item["type"] for item in issues} == {"narrative_beat_scene_coverage"}


def test_resolved_or_abandoned_threads_do_not_create_debt():
    issues, _ = build_narrative_thread_diagnostics(
        canon={
            "narrative_threads": [
                _thread(status="resolved", due=1),
                {**_thread(status="abandoned", due=1), "id": "thread:abandoned"},
            ]
        },
        chapter={"chapter_number": 5, "scene_plan": []},
        total_chapters=5,
    )

    assert issues == []


def test_thread_conflict_explanation_keeps_thread_evidence_and_repair_options():
    issues, _ = build_narrative_thread_diagnostics(
        canon={"narrative_threads": [_thread()]},
        chapter={"chapter_number": 3, "content": "正文", "scene_plan": []},
        total_chapters=5,
    )
    explanations = explain_consistency_issues(
        issues=issues,
        chapter={"chapter_number": 3, "content": "正文", "scene_plan": []},
        previous_chapters=[],
        canon={"narrative_threads": [_thread()]},
    )
    overdue = next(item for item in explanations if item["type"] == "narrative_thread_overdue")
    assert overdue["conflicting_records"][0]["id"] == "thread:king-seal"
    assert {option["kind"] for option in overdue["repair_options"]} == {"canon_operation", "revision_feedback"}


def test_boundary_conflict_only_offers_revision_feedback():
    explanations = explain_consistency_issues(
        issues=[
            {
                "type": "creative_brief_boundary",
                "description": "正文命中了回避内容：血腥",
                "severity": "high",
                "suggestion": "删除相关内容",
                "chapter": 2,
            }
        ],
        chapter={"chapter_number": 2, "content": "血腥", "scene_plan": []},
        previous_chapters=[],
    )
    assert [option["kind"] for option in explanations[0]["repair_options"]] == ["revision_feedback"]
