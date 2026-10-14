from agent_runtime.workflows.novel.agents.chapter_candidate import (
    ChapterCandidateAgent,
    chapter_candidate_matches_state,
    chapter_candidate_source_hash,
)


def _state() -> dict:
    return {
        "current_chapter": 1,
        "style": "gu_long",
        "chapter_plan": {"chapter": 1, "title": "雾起"},
        "scene_plan": [{"scene_number": 1, "estimated_words": 8}],
        "current_draft": {"chapter_number": 1, "content": "当前草稿"},
        "canon": {"version": 3, "facts": []},
    }


def test_candidate_source_hash_tracks_review_context_and_allows_sibling_switches():
    state = _state()
    source_hash = chapter_candidate_source_hash(state)
    candidate = {"source_hash": source_hash}

    assert chapter_candidate_matches_state(candidate, state) is True
    changed = {**state, "current_draft": {"chapter_number": 1, "content": "候选正文"}}
    assert chapter_candidate_matches_state(candidate, changed) is False
    changed["candidate_source_hash"] = source_hash
    assert chapter_candidate_matches_state(candidate, changed) is True

    changed_brief = {**state, "creative_brief": {"ending_tone": "tragic"}}
    assert chapter_candidate_source_hash(changed_brief) != source_hash
    assert chapter_candidate_matches_state(candidate, changed_brief) is False


async def test_candidate_agent_generates_polishes_and_scores_an_independent_draft(monkeypatch):
    captured = {}

    class Writer:
        def __init__(self, novel_id=""):
            captured["novel_id"] = novel_id

        async def write_chapter(self, state):
            captured["notes"] = state["revision_notes"]
            return {
                "chapter_number": 1,
                "title": "雾起",
                "content": "初稿",
                "summary": "入城",
                "scene_plan": [{"scene_number": 1, "estimated_words": 3}],
                "scene_drafts": [{"scene_number": 1, "content": "初稿"}],
            }

    class Editor:
        async def polish(self, chapter, style):
            captured["style"] = style
            return {
                **chapter,
                "content": "甲。\n\n乙。\n\n丙。",
                "scene_drafts": [{"scene_number": 1, "content": "甲乙丙"}],
            }

    monkeypatch.setattr("agent_runtime.workflows.novel.agents.chapter_candidate.SceneWriterAgent", Writer)
    monkeypatch.setattr("agent_runtime.workflows.novel.agents.chapter_candidate.StyleEditorAgent", Editor)

    result = await ChapterCandidateAgent("novel-1").generate(
        _state(),
        candidate_number=2,
        total_candidates=3,
        instruction="加强追逐",
    )

    assert captured["novel_id"] == "novel-1"
    assert "候选稿 2/3" in captured["notes"]
    assert "加强追逐" in captured["notes"]
    assert captured["style"] == "gu_long"
    assert result["candidate_number"] == 2
    assert set(result["scores"]) == {
        "length_adherence",
        "structure",
        "scene_coverage",
        "narrative_coverage",
        "repetition_control",
        "consistency",
    }
