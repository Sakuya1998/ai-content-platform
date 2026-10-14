"""PostgreSQL project memory, semantic retrieval and explicit frozen sharing."""

import hashlib
import json
import math
from uuid import uuid4

from agent_runtime.infrastructure.postgres.jobs import _json


def knowledge_pack_result(cursor, workspace, project, name, identity):
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ValueError("knowledge pack requires a name")
    nodes = KnowledgeOperations(None)._local(cursor, project)
    if not nodes:
        raise ValueError("cannot publish empty project memory")
    digest = hashlib.sha256(json.dumps(nodes, sort_keys=True).encode()).hexdigest()
    cursor.execute(
        """INSERT INTO knowledge_packs(id,workspace_id,source_project_id,name,nodes,content_hash)
        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING RETURNING id""",
        (identity, workspace, project, name.strip(), _json(nodes), digest),
    )
    if not cursor.fetchone():
        cursor.execute(
            "SELECT source_project_id,name,content_hash,nodes FROM knowledge_packs WHERE id=%s AND workspace_id=%s",
            (identity, workspace),
        )
        existing = cursor.fetchone()
        if not existing or existing[0] != project or existing[1] != name.strip():
            raise ValueError("knowledge pack identity conflict")
        digest, nodes = existing[2], existing[3]
    return {
        "id": identity,
        "source_project_id": project,
        "name": name.strip(),
        "content_hash": digest,
        "node_count": len(nodes),
    }


def commit_knowledge_operation(cursor, command, operation, arguments, identity):
    if operation == "knowledge.publish":
        return knowledge_pack_result(cursor, command.workspace_id, command.project_id, arguments.get("name"), identity)
    pack_id = arguments.get("pack_id")
    if operation not in {"knowledge.bind", "knowledge.unbind"} or not isinstance(pack_id, str) or not pack_id:
        raise ValueError("invalid staged knowledge operation")
    cursor.execute("SELECT 1 FROM knowledge_packs WHERE id=%s AND workspace_id=%s", (pack_id, command.workspace_id))
    if not cursor.fetchone():
        raise KeyError("knowledge pack not found")
    if operation == "knowledge.bind":
        cursor.execute(
            """INSERT INTO knowledge_pack_bindings(workspace_id,target_project_id,pack_id)
            VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
            (command.workspace_id, command.project_id, pack_id),
        )
    else:
        cursor.execute(
            "DELETE FROM knowledge_pack_bindings WHERE target_project_id=%s AND pack_id=%s",
            (command.project_id, pack_id),
        )
    return {"pack_id": pack_id, "shared": operation == "knowledge.bind"}


def validate_knowledge_operation(cursor, command, operation, arguments):
    if operation == "knowledge.publish":
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise ValueError("knowledge pack requires a name")
        if not KnowledgeOperations(None)._local(cursor, command.project_id):
            raise ValueError("cannot publish empty project memory")
        return
    pack_id = arguments.get("pack_id")
    if not isinstance(pack_id, str) or not pack_id:
        raise ValueError("knowledge pack ID is required")
    cursor.execute("SELECT 1 FROM knowledge_packs WHERE id=%s AND workspace_id=%s", (pack_id, command.workspace_id))
    if not cursor.fetchone():
        raise KeyError("knowledge pack not found")


class KnowledgeOperations:
    def __init__(self, store):
        self.store = store

    def _local(self, cursor, project):
        cursor.execute(
            """SELECT id,project_id,memory_type,content,metadata,document_version_id
               FROM memory_nodes WHERE project_id=%s ORDER BY created_at,id LIMIT 1000""",
            (project,),
        )
        values = [
            {
                "id": str(identity),
                "project_id": origin,
                "memory_type": kind,
                "content": content,
                "metadata": metadata,
                "document_version_id": str(version) if version else None,
            }
            for identity, origin, kind, content, metadata, version in cursor.fetchall()
        ]
        cursor.execute(
            """SELECT e.id,e.embedding::text,e.embedding_model,e.index_version FROM memory_embeddings e
                       JOIN memory_nodes n ON n.id=e.id WHERE n.project_id=%s ORDER BY e.created_at DESC""",
            (project,),
        )
        vectors = {}
        for identity, vector, model, index in cursor.fetchall():
            vectors.setdefault(str(identity), (json.loads(vector), model, index))
        for node in values:
            if node["id"] in vectors:
                vector, model, index = vectors[node["id"]]
                node["embedding"] = vector
                node["metadata"] = {**node["metadata"], "embedding_model": model, "index_version": index}
        return values

    def list_memory(self, workspace, project):
        with self.store.scope(workspace) as cursor:
            values = self._local(cursor, project)
            cursor.execute(
                """SELECT p.id,p.nodes FROM knowledge_pack_bindings b JOIN knowledge_packs p ON p.id=b.pack_id
                   WHERE b.target_project_id=%s ORDER BY p.created_at,p.id""",
                (project,),
            )
            for pack_id, nodes in cursor.fetchall():
                values.extend({**node, "knowledge_pack_id": str(pack_id)} for node in nodes)
            return values

    def publish(self, workspace, project, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise ValueError("knowledge pack requires a name")
        with self.store.scope(workspace) as cursor:
            return knowledge_pack_result(cursor, workspace, project, name, str(uuid4()))

    def list_packs(self, workspace):
        with self.store.scope(workspace) as cursor:
            cursor.execute("SELECT id,source_project_id,name,content_hash FROM knowledge_packs ORDER BY created_at,id")
            return [
                {"id": str(identity), "source_project_id": project, "name": name, "content_hash": digest}
                for identity, project, name, digest in cursor.fetchall()
            ]

    def share(self, workspace, pack, target):
        with self.store.scope(workspace) as cursor:
            cursor.execute("SELECT 1 FROM knowledge_packs WHERE id=%s", (pack,))
            if not cursor.fetchone():
                raise KeyError("knowledge pack not found")
            cursor.execute(
                """INSERT INTO knowledge_pack_bindings(workspace_id,target_project_id,pack_id)
                   VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                (workspace, target, pack),
            )

    def unshare(self, workspace, pack, target):
        with self.store.scope(workspace) as cursor:
            cursor.execute(
                "DELETE FROM knowledge_pack_bindings WHERE target_project_id=%s AND pack_id=%s", (target, pack)
            )

    def search(self, workspace, project, vector, *, model, index_version, limit=5, embedding_identity=None):
        if (
            not isinstance(vector, list)
            or len(vector) != 1536
            or any(not isinstance(value, (float, int)) or not math.isfinite(value) for value in vector)
        ):
            raise ValueError("query embedding requires 1536 finite dimensions")
        limit = max(1, min(int(limit), 20))
        compatible = {key: value for key, value in (embedding_identity or {}).items() if key != "index_version"}
        with self.store.scope(workspace) as cursor:
            cursor.execute(
                """SELECT n.id,n.project_id,n.memory_type,n.content,n.metadata,e.embedding <=> %s::vector distance
                   FROM memory_embeddings e JOIN memory_nodes n ON n.id=e.id
                   WHERE n.project_id=%s AND e.embedding_model=%s AND e.index_version=%s AND n.metadata @> %s
                   ORDER BY distance,n.id LIMIT %s""",
                (json.dumps(vector), project, model, index_version, _json(compatible), limit),
            )
            values = [
                {
                    "id": str(identity),
                    "project_id": origin,
                    "memory_type": kind,
                    "content": content,
                    "metadata": metadata,
                    "distance": float(distance),
                }
                for identity, origin, kind, content, metadata, distance in cursor.fetchall()
            ]
            cursor.execute(
                """SELECT p.id,p.nodes FROM knowledge_pack_bindings b JOIN knowledge_packs p ON p.id=b.pack_id
                           WHERE b.target_project_id=%s""",
                (project,),
            )
            for pack_id, nodes in cursor.fetchall():
                for node in nodes:
                    embedding = node.get("embedding")
                    if compatible and any(
                        node.get("metadata", {}).get(key) != value for key, value in compatible.items()
                    ):
                        continue
                    if not isinstance(embedding, list) or len(embedding) != len(vector):
                        continue
                    denominator = math.sqrt(sum(x * x for x in vector) * sum(x * x for x in embedding))
                    distance = (
                        1 - sum(a * b for a, b in zip(vector, embedding, strict=True)) / denominator
                        if denominator
                        else 1
                    )
                    values.append(
                        {
                            **node,
                            "metadata": {**node.get("metadata", {}), "knowledge_pack_id": str(pack_id)},
                            "distance": distance,
                        }
                    )
            return sorted(values, key=lambda row: (row["distance"], row["id"]))[:limit]

    def record_maintenance(self, workspace, project, operation, result):
        identity = str(uuid4())
        with self.store.scope(workspace) as cursor:
            cursor.execute(
                """INSERT INTO runtime_maintenance_runs(id,workspace_id,project_id,operation,result)
                   VALUES(%s,%s,%s,%s,%s)""",
                (identity, workspace, project, operation, _json(result)),
            )
        return {"id": identity, "operation": operation, "result": result}

    def usage(self, workspace, project):
        with self.store.scope(workspace) as cursor:
            cursor.execute(
                """SELECT count(*),COALESCE(sum(input_tokens),0),COALESCE(sum(output_tokens),0),COALESCE(sum(cost),0)
                           FROM model_calls m JOIN runtime_jobs j ON j.id=m.job_id WHERE j.project_id=%s""",
                (project,),
            )
            count, inputs, outputs, cost = cursor.fetchone()
            return {
                "calls": count,
                "input_tokens": inputs,
                "output_tokens": outputs,
                "total_tokens": inputs + outputs,
                "cost": str(cost),
            }

    def traces(self, workspace, project, limit=100):
        with self.store.scope(workspace) as cursor:
            cursor.execute(
                """SELECT m.id,m.job_id,m.provider,m.model,m.prompt_hash,
                          m.input_tokens,m.output_tokens,m.status,m.metadata
                           FROM model_calls m JOIN runtime_jobs j ON j.id=m.job_id WHERE j.project_id=%s
                           ORDER BY m.created_at DESC,m.id LIMIT %s""",
                (project, max(1, min(limit, 500))),
            )
            return [
                {
                    "id": str(identity),
                    "job_id": job,
                    "provider": provider,
                    "model": model,
                    "prompt_hash": digest,
                    "input_tokens": inputs,
                    "output_tokens": outputs,
                    "status": status,
                    "metadata": metadata,
                }
                for identity, job, provider, model, digest, inputs, outputs, status, metadata in cursor.fetchall()
            ]
