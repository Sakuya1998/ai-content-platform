"""Content-neutral workflow persistence and tenant isolation."""

from langgraph.checkpoint.base import empty_checkpoint

from tests.infrastructure.test_novel_runtime import novel_store as _novel_store

novel_store = _novel_store


def test_content_workflows_keep_independent_state_records_and_checkpoints(novel_store):
    from agent_runtime.infrastructure.postgres.checkpoints import WorkflowPostgresSaver
    from agent_runtime.infrastructure.postgres.workflow import WorkflowStore

    screenplay = WorkflowStore(novel_store.execution, workflow_id="screenplay.write@2")
    copy = WorkflowStore(novel_store.execution, workflow_id="copy.write@1")
    screenplay.initialize({"project_id": screenplay.project_id, "scene": 1})
    copy.initialize({"project_id": copy.project_id, "campaign": "launch"})
    first = screenplay.record("scene", {"text": "INT. OFFICE"}, category="document", scope_key="scene:1")
    assert screenplay.record("scene", {"text": "INT. OFFICE"}, category="document", scope_key="scene:1") == first
    assert copy.records("scene") == []
    assert copy.state()["campaign"] == "launch"
    screenplay.save_state({"project_id": screenplay.project_id, "scene": 2})
    assert screenplay.state()["scene"] == 2
    assert novel_store.state()["title"] == "Test"
    config = {"configurable": {"thread_id": screenplay.project_id}}
    checkpoint = empty_checkpoint()
    saved = WorkflowPostgresSaver(screenplay).put(config, checkpoint, {}, {})
    assert WorkflowPostgresSaver(screenplay).get_tuple(saved).checkpoint["id"] == checkpoint["id"]
    assert WorkflowPostgresSaver(copy).get_tuple(config) is None
    with screenplay.execution.store.scope("foreign-workspace") as cursor:
        for table in ("workflow_states", "workflow_records", "workflow_checkpoints"):
            cursor.execute(f"SELECT count(*) FROM {table} WHERE project_id=%s", (screenplay.project_id,))
            assert cursor.fetchone()[0] == 0


def test_generic_memory_and_knowledge_operations_share_one_source(novel_store):
    from agent_runtime.infrastructure.postgres.knowledge import KnowledgeOperations
    from agent_runtime.infrastructure.postgres.novel_memory import PostgresWorkflowMemory
    from agent_runtime.infrastructure.postgres.workflow import WorkflowStore

    class Models:
        model = "test"

        async def embed(self, texts):
            return [[0.1] * 1536 for _ in texts]

        def embedding_identity(self):
            return {"embedding_model": self.model, "index_version": "memory@1"}

    store = WorkflowStore(novel_store.execution, workflow_id="copy.write@1")
    models = Models()
    memory = PostgresWorkflowMemory(store, models)
    memory.store_content("campaign fact", content_id="fact")
    memory.store_content("campaign fact", content_id="fact")
    operations = KnowledgeOperations(store.execution.store)
    nodes = operations.list_memory(store.workspace_id, store.project_id)
    assert len(nodes) == 1 and nodes[0]["content"] == "campaign fact"
    assert nodes[0]["metadata"]["_memory_id"] == "fact"
    assert memory.list_records()[0]["id"] == "fact"
    assert operations.search(store.workspace_id, store.project_id, [0.1] * 1536, model="test", index_version="memory@1")
    memory.store_content("confirmed campaign fact", metadata={"type": "canon"}, content_id="fact")
    nodes = operations.list_memory(store.workspace_id, store.project_id)
    assert len(nodes) == 1 and nodes[0]["content"] == "confirmed campaign fact"
    assert nodes[0]["memory_type"] == "canon"
    models.model = "replacement-model"
    memory.store_content("new campaign fact", content_id="fact")
    assert (
        operations.search(store.workspace_id, store.project_id, [0.1] * 1536, model="test", index_version="memory@1")
        == []
    )
    memory.clear()
    assert operations.list_memory(store.workspace_id, store.project_id) == []
