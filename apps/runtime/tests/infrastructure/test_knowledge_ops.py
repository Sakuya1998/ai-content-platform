import os
from uuid import uuid4

import pytest

from agent_runtime.infrastructure.postgres.knowledge import KnowledgeOperations
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore


@pytest.fixture
def operations():
    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    store = RuntimeMQStore(url)
    yield KnowledgeOperations(store)
    store.close()


def test_published_knowledge_is_frozen_and_only_explicitly_shared(operations):
    workspace, project, target = str(uuid4()), str(uuid4()), str(uuid4())
    with operations.store.scope(workspace) as cursor:
        cursor.execute(
            """INSERT INTO memory_nodes(id,workspace_id,project_id,memory_type,source_id,source_version,
                       content,content_hash) VALUES(%s,%s,%s,'summary','source',1,'Original','original')
                       RETURNING id""",
            (str(uuid4()), workspace, project),
        )
        node_id = str(cursor.fetchone()[0])
    assert operations.list_memory(workspace, target) == []
    pack = operations.publish(workspace, project, "Shared notes")
    operations.share(workspace, pack["id"], target)
    with operations.store.scope(workspace) as cursor:
        cursor.execute("UPDATE memory_nodes SET content='Changed' WHERE id=%s", (node_id,))
    assert operations.list_memory(workspace, target)[0]["content"] == "Original"
    with pytest.raises(KeyError):
        operations.share(str(uuid4()), pack["id"], target)


def test_vector_search_never_crosses_project_or_workspace(operations):
    workspace, project, other = str(uuid4()), str(uuid4()), str(uuid4())
    for target in [project, other]:
        identity = str(uuid4())
        with operations.store.scope(workspace) as cursor:
            cursor.execute(
                """INSERT INTO memory_nodes(id,workspace_id,project_id,memory_type,source_id,source_version,
                           content,content_hash) VALUES(%s,%s,%s,'summary',%s,1,%s,'hash')""",
                (identity, workspace, target, identity, target),
            )
            cursor.execute(
                "INSERT INTO memory_embeddings VALUES(%s,%s,'test','1',%s::vector,now())",
                (identity, target, str([0.1] * 1536)),
            )
    hits = operations.search(workspace, project, [0.1] * 1536, model="test", index_version="1")
    assert len(hits) == 1 and hits[0]["project_id"] == project
    assert operations.search(str(uuid4()), project, [0.1] * 1536, model="test", index_version="1") == []
    with pytest.raises(ValueError):
        operations.search(workspace, project, [float("nan")] * 1536, model="test", index_version="1")
    pack = operations.publish(workspace, project, "Semantic pack")
    target = str(uuid4())
    assert operations.search(workspace, target, [0.1] * 1536, model="test", index_version="1") == []
    operations.share(workspace, pack["id"], target)
    shared = operations.search(workspace, target, [0.1] * 1536, model="test", index_version="1")
    assert len(shared) == 1 and shared[0]["metadata"]["knowledge_pack_id"] == pack["id"]
