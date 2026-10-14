from agent_runtime.workflows.novel.workflow import adapt_novel_request


def test_novel_adapter_maps_generic_revision_and_block_target():
    original = {
        "project_id": "project-1", "operation": "revision",
        "arguments": {"block_index": 3, "feedback": "Improve dialogue"},
        "input": {"genre": "fiction", "total_chapters": 8}, "brief": {"notes": "Keep names stable"},
    }
    adapted = adapt_novel_request(original)
    assert adapted["operation"] == "book_revision"
    assert adapted["arguments"]["chapter_number"] == 3
    assert adapted["novel_input"]["novel_id"] == "project-1"
    assert adapted["novel_input"]["total_chapters"] == 8
    assert adapted["novel_input"]["creative_brief"] == original["brief"]
    assert original["operation"] == "revision"
