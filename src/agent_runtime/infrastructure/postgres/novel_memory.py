"""Scoped pgvector memory with synchronous legacy Agent-compatible methods."""

import asyncio
import hashlib
import math
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from uuid import uuid4

from agent_runtime.infrastructure.models.client import RuntimeModels
from agent_runtime.infrastructure.postgres.jobs import _json


class PostgresWorkflowMemory:
    def __init__(self, store, models):
        self.store, self.models = store, models
        self.project_id = store.project_id

    def _embedding(self, text):
        # Legacy agents call memory synchronously inside an async graph node.
        # A private loop avoids nesting the worker loop and preserves scope.
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            vectors = executor.submit(context.run, lambda: asyncio.run(self.models.embed([text]))).result()
        if len(vectors) != 1 or len(vectors[0]) != 1536:
            raise ValueError("workflow memory requires one 1536-dimensional embedding")
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vectors[0]):
            raise ValueError("workflow memory embedding must contain finite numbers")
        return "[" + ",".join(str(float(value)) for value in vectors[0]) + "]"

    def embedding_identity(self):
        helper = getattr(self.models, "embedding_identity", None)
        if helper:
            return helper()
        return RuntimeModels(self.store.execution.settings).embedding_identity(execution=self.store.execution)

    def store_content(self, content, metadata=None, content_id=None):
        identity = content_id or str(uuid4())
        metadata = {**(metadata or {}), **self.embedding_identity(), "_memory_id": identity}
        metadata.setdefault("source_content_hash", hashlib.sha256(content.encode()).hexdigest())
        metadata.setdefault("source_version_id", metadata.get("content_hash") or metadata["source_content_hash"])
        vector = self._embedding(content)
        with self.store.transaction(write=True) as cursor:
            self._write(cursor, identity, content, metadata, vector)

    def _write(self, cursor, identity, content, metadata, vector):
        cursor.execute(
            """INSERT INTO memory_nodes(workspace_id,project_id,workflow_id,memory_type,source_id,source_version,
               content,metadata,content_hash,source_job_id,source_version_id,fencing_token)
               VALUES(%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(workspace_id,project_id,workflow_id,source_id,source_version) WHERE workflow_id<>''
               DO UPDATE SET memory_type=excluded.memory_type,
               content=excluded.content,metadata=excluded.metadata,content_hash=excluded.content_hash,
               source_job_id=excluded.source_job_id,source_version_id=excluded.source_version_id,
               fencing_token=excluded.fencing_token,updated_at=now() RETURNING id""",
            (
                *self.store.scope,
                metadata.get("type", "summary"),
                identity,
                content,
                _json(metadata),
                hashlib.sha256(content.encode()).hexdigest(),
                self.store.execution.job.command.job_id,
                metadata.get("source_version_id"),
                self.store.execution.job.fencing_token,
            ),
        )
        node_id = cursor.fetchone()[0]
        cursor.execute(
            "DELETE FROM memory_embeddings WHERE id=%s AND (embedding_model<>%s OR index_version<>%s)",
            (node_id, metadata["embedding_model"], metadata["index_version"]),
        )
        cursor.execute(
            """INSERT INTO memory_embeddings(id,project_id,embedding_model,index_version,embedding)
               VALUES(%s,%s,%s,%s,%s::vector) ON CONFLICT(id,embedding_model,index_version)
               DO UPDATE SET embedding=excluded.embedding,created_at=now()""",
            (node_id, self.project_id, metadata["embedding_model"], metadata["index_version"], vector),
        )

    def search_similar(self, query, k=5, where=None):
        embedding_identity = self.embedding_identity()
        vector = self._embedding(query)
        with self.store.transaction() as cursor:
            cursor.execute(
                """SELECT n.source_id,n.content,n.metadata,e.embedding <=> %s::vector AS distance
                   FROM memory_nodes n JOIN memory_embeddings e ON e.id=n.id
                   WHERE n.workspace_id=%s AND n.project_id=%s AND n.workflow_id=%s AND n.metadata @> %s
                   AND e.embedding_model=%s AND e.index_version=%s ORDER BY distance,n.source_id LIMIT %s""",
                (
                    vector,
                    *self.store.scope,
                    _json({**(where or {}), **embedding_identity}),
                    embedding_identity["embedding_model"],
                    embedding_identity["index_version"],
                    max(1, min(int(k), 20)),
                ),
            )
            hits = [
                {"id": identity, "content": content, "metadata": metadata, "distance": float(distance)}
                for identity, content, metadata, distance in cursor.fetchall()
            ]
            cursor.execute(
                """SELECT p.id,p.source_project_id,p.nodes FROM knowledge_packs p
                   JOIN knowledge_pack_bindings b ON b.pack_id=p.id AND b.workspace_id=p.workspace_id
                   WHERE b.workspace_id=%s AND b.target_project_id=%s""",
                (self.store.workspace_id, self.project_id),
            )
            packs = cursor.fetchall()
        query_vector = [float(value) for value in vector.strip("[]").split(",")]
        for pack_id, source_project, nodes in packs:
            for node in nodes:
                metadata = node.get("metadata") or {}
                if any(metadata.get(key) != value for key, value in (where or {}).items()):
                    continue
                content = str(node.get("content", ""))
                shared_vector = node.get("embedding")
                if shared_vector and any(
                    metadata.get(key) != value for key, value in embedding_identity.items() if key != "index_version"
                ):
                    continue
                if not shared_vector:
                    shared_vector = [float(value) for value in self._embedding(content).strip("[]").split(",")]
                if len(shared_vector) != 1536:
                    raise ValueError("knowledge pack embedding dimensions conflict")
                denominator = math.sqrt(sum(value * value for value in query_vector)) * math.sqrt(
                    sum(value * value for value in shared_vector)
                )
                distance = (
                    1 - sum(a * b for a, b in zip(query_vector, shared_vector, strict=True)) / denominator
                    if denominator
                    else 1
                )
                hits.append(
                    {
                        "id": f"pack:{pack_id}:{node.get('id', '')}",
                        "content": content,
                        "metadata": {
                            **metadata,
                            **embedding_identity,
                            "knowledge_pack_id": str(pack_id),
                            "source_project_id": source_project,
                        },
                        "distance": distance,
                    }
                )
        return sorted(hits, key=lambda hit: (hit["distance"], hit["id"]))[: max(1, min(int(k), 20))]

    def list_records(self):
        with self.store.transaction() as cursor:
            cursor.execute(
                "SELECT source_id,content,metadata FROM memory_nodes WHERE workspace_id=%s "
                "AND project_id=%s AND workflow_id=%s ORDER BY source_id",
                self.store.scope,
            )
            return [
                {"id": identity, "content": content, "metadata": metadata}
                for identity, content, metadata in cursor.fetchall()
            ]

    def clear(self):
        with self.store.transaction(write=True) as cursor:
            cursor.execute(
                "DELETE FROM memory_nodes WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s",
                self.store.scope,
            )

    def rebuild(self, records):
        identity = self.embedding_identity()
        prepared = [(record, self._embedding(record["content"])) for record in records]
        with self.store.transaction(write=True) as cursor:
            cursor.execute(
                "DELETE FROM memory_nodes WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s",
                self.store.scope,
            )
            for record, vector in prepared:
                metadata = {
                    **record["metadata"],
                    **identity,
                    "_memory_id": record["id"],
                    "source_content_hash": hashlib.sha256(record["content"].encode()).hexdigest(),
                }
                metadata.setdefault(
                    "source_version_id", metadata.get("content_hash") or metadata["source_content_hash"]
                )
                self._write(cursor, record["id"], record["content"], metadata, vector)
        return {
            "schema_version": "memory-quality-v1",
            "record_count": len(records),
            "ids": [record["id"] for record in records],
        }


class PostgresNovelMemory(PostgresWorkflowMemory):
    """Chapter vocabulary adapter over the shared project memory graph."""

    @property
    def novel_id(self):
        return self.project_id

    def get_chapter_memory(self, chapter_number, k=3):
        return [item["content"] for item in self.search_similar(f"Chapter {chapter_number} previous events", k)]

    def store_hierarchical_memory(self, content, *, content_hash):
        self.store_content(
            content,
            metadata={"type": "hierarchical_memory", "content_hash": content_hash},
            content_id=f"{self.project_id}:hierarchical-memory",
        )
