"""长期记忆检索质量与重建测试。"""

from agent_runtime.workflows.novel.analysis.memory_quality import (
    build_memory_eval_cases,
    build_memory_records,
    evaluate_memory_retrieval,
    rebuild_memory_index,
)


class FakeMemory:
    def __init__(self, records=None):
        self.records = list(records or [])

    def list_records(self):
        return self.records

    def search_similar(self, query, k=5):
        if "角色" in query:
            return [self._hit("n1:character:1", "character")][:k]
        if "第1章" in query:
            return [self._hit("n1:chapter:1", "chapter")][:k]
        if "Canon" in query or "权威" in query:
            return [self._hit("n1:chapter:1", "chapter")][:k]
        return [self._hit("n1:world_bible", "world_bible")][:k]

    @staticmethod
    def _hit(identifier, kind):
        return {"id": identifier, "content": identifier, "metadata": {"_memory_id": identifier, "type": kind}}

    def clear(self):
        self.records = []

    def store_content(self, content, metadata=None, content_id=None):
        self.records.append({"id": content_id, "content": content, "metadata": metadata or {}})


def _sources():
    return {
        "world_bible": "雾都历史与门派规则",
        "characters": [{"name": "林寒", "role": "主角", "personality": "克制"}],
        "outline": [{"chapter": 1, "summary": "林寒进入雾都"}],
        "chapters": [
            {
                "chapter_number": 1,
                "title": "雾起",
                "summary": "林寒进入雾都",
                "content": "正文",
                "status": "final",
            }
        ],
        "canon": {"version": 3, "facts": [{"subject": "林寒", "value": "失忆"}]},
    }


def test_build_records_and_cases_use_stable_ids():
    sources = _sources()
    records = build_memory_records(
        novel_id="n1",
        **sources,
        memory_index={
            "schema_version": "book-memory-v1",
            "chapters": [{"chapter": 1}],
            "book_summary": "摘要",
        },
    )
    ids = {item["id"] for item in records}
    assert "n1:world_bible" in ids
    assert "n1:character:1" in ids
    assert "n1:chapter:1" in ids
    cases = build_memory_eval_cases(novel_id="n1", **sources)
    assert {item["category"] for item in cases} == {"world_bible", "character", "outline", "chapter_summary", "canon"}


def test_quality_report_calculates_recall_mrr_and_canon_conflict():
    sources = _sources()
    cases = build_memory_eval_cases(novel_id="n1", **sources)
    report = evaluate_memory_retrieval(memory=FakeMemory(), cases=cases, k=3)
    assert report["case_count"] == 5
    assert report["recall_at_k"] < 1.0
    assert report["mrr"] > 0
    assert report["canon_vector_conflict_rate"] == 1.0
    assert report["status"] == "attention"


def test_rebuild_clears_old_records_and_writes_all_sources():
    sources = _sources()
    memory = FakeMemory([{"id": "old", "content": "旧", "metadata": {}}])
    records = build_memory_records(novel_id="n1", **sources, memory_index={})
    report = rebuild_memory_index(memory, records)
    assert report["record_count"] == len(records)
    assert {item["id"] for item in memory.records} == {item["id"] for item in records}
    assert "old" not in report["ids"]
