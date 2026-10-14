"""Scoped inbox, database dispatch and transactional MQ outboxes."""

import hashlib
import json
import math
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent
from agent_runtime.execution.errors import JobCancelled
from agent_runtime.execution.leases import FencingTokenError
from agent_runtime.infrastructure.postgres.jobs import PostgresRuntimeJobStore, _job_from_row, _json


class RuntimeMQStore(PostgresRuntimeJobStore):
    def accept(self, command):
        self.accept_task(command)
        return self.get_scoped(command.job_id, command.workspace_id)

    @contextmanager
    def scope(self, workspace_id):
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace_id,))
            yield cursor

    def scopes(self):
        with self._pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT workspace_id FROM runtime_worker_scopes ORDER BY workspace_id")
            return [row[0] for row in cursor.fetchall()]

    def _inbox(self, cursor, command):
        payload = command.model_dump(mode="json")
        cursor.execute(
            """INSERT INTO command_inbox(command_id,workspace_id,job_id,payload) VALUES(%s,%s,%s,%s)
               ON CONFLICT(command_id) DO NOTHING RETURNING command_id""",
            (command.command_id, command.workspace_id, command.job_id, _json(payload)),
        )
        if cursor.fetchone():
            cursor.execute(
                "INSERT INTO runtime_worker_scopes VALUES(%s) ON CONFLICT DO NOTHING", (command.workspace_id,)
            )
            return True
        cursor.execute("SELECT payload FROM command_inbox WHERE command_id=%s", (command.command_id,))
        previous = cursor.fetchone()
        if not previous or previous[0] != payload:
            raise ValueError("command_id payload conflict")
        return False

    def accept_task(self, command, queue="runtime"):
        allowed = {"runtime": {"workflow.run", "node.run"}, "memory": {"memory.refresh"}}
        if command.command_type not in allowed[queue]:
            raise ValueError("command type does not match task queue")
        with self.scope(command.workspace_id) as cursor:
            # Serialize task acceptance with an earlier/later control command.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (command.workspace_id + ":" + command.job_id,)
            )
            if not self._inbox(cursor, command):
                return
            cursor.execute(
                "SELECT project_id FROM runtime_cancellations WHERE workspace_id=%s AND job_id=%s",
                (command.workspace_id, command.job_id),
            )
            cancellation = cursor.fetchone()
            if cancellation and cancellation[0] != command.project_id:
                raise ValueError("cancellation project conflict")
            status = "cancelled" if cancellation else "accepted"
            cursor.execute(
                """INSERT INTO runtime_jobs(id,workspace_id,project_id,command_id,payload,status,task_queue)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    command.job_id,
                    command.workspace_id,
                    command.project_id,
                    command.command_id,
                    _json(command.model_dump(mode="json")),
                    status,
                    queue,
                ),
            )
            self._event(cursor, command, f"{'memory' if queue == 'memory' else 'job'}.{status}", {})

    def accept_control(self, command):
        if command.command_type not in {"job.cancel", "job.resume"}:
            raise ValueError("unsupported control command")
        with self.scope(command.workspace_id) as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (command.workspace_id + ":" + command.job_id,)
            )
            if not self._inbox(cursor, command):
                return
            cursor.execute("SELECT * FROM runtime_jobs WHERE id=%s FOR UPDATE", (command.job_id,))
            row = cursor.fetchone()
            job = _job_from_row(cursor, row) if row else None
            if job and job.command.project_id != command.project_id:
                raise ValueError("control command project conflict")
            if command.command_type == "job.cancel":
                cursor.execute(
                    """INSERT INTO runtime_cancellations(workspace_id,job_id,project_id,command_id)
                       VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (command.workspace_id, command.job_id, command.project_id, command.command_id),
                )
                if job and job.status in {"accepted", "running", "waiting_review"}:
                    status = "cancel_requested" if job.status == "running" else "cancelled"
                    cancellation_payload = self._media_cancellation_payload(cursor, job)
                    cursor.execute(
                        "UPDATE runtime_jobs SET status=%s,result=%s,updated_at=now() WHERE id=%s",
                        (status, _json(cancellation_payload), command.job_id),
                    )
                    self._event(cursor, job.command, "job." + status, cancellation_payload, command.command_id)
            elif job and job.status == "waiting_review":
                cursor.execute(
                    """UPDATE runtime_jobs SET status='accepted',next_retry_at=now(),resume_input=%s,
                       updated_at=now() WHERE id=%s""",
                    (_json(command.payload), command.job_id),
                )
                self._event(cursor, job.command, "job.accepted", {}, command.command_id)
            else:
                raise ValueError("resume requires a job waiting for review")

    def claim_next(self, workspace_id, queue, worker_id, lease_seconds, *, job_id=None):
        with self.scope(workspace_id) as cursor:
            # A crashed worker cannot leave cancellation pending indefinitely.
            cursor.execute(
                """SELECT * FROM runtime_jobs WHERE task_queue=%s AND status='cancel_requested'
                   AND lease_expires_at <= clock_timestamp() AND (%s::text IS NULL OR id=%s)
                   FOR UPDATE SKIP LOCKED""",
                (queue, job_id, job_id),
            )
            rows = cursor.fetchall()
            cancelled = [_job_from_row(cursor, row) for row in rows]
            for job in cancelled:
                cancellation_payload = self._media_cancellation_payload(cursor, job)
                cursor.execute(
                    """UPDATE runtime_jobs SET status='cancelled',result=%s,lease_owner=NULL,
                    lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                    (_json(cancellation_payload), job.command.job_id),
                )
                self._event(cursor, job.command, "job.cancelled", cancellation_payload)
            cursor.execute(
                """SELECT * FROM runtime_jobs WHERE task_queue=%s AND next_retry_at <= now()
                   AND (%s::text IS NULL OR id=%s)
                   AND (status='accepted' OR (status='running' AND lease_expires_at <= clock_timestamp()))
                   ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",
                (queue, job_id, job_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            job = _job_from_row(cursor, row)
            cursor.execute(
                """UPDATE runtime_jobs SET status='running',lease_owner=%s,
                   lease_expires_at=clock_timestamp()+(%s*interval '1 second'),heartbeat_at=now(),
                   fencing_token=fencing_token+1,attempt=attempt+1,updated_at=now() WHERE id=%s RETURNING *""",
                (worker_id, lease_seconds, job.command.job_id),
            )
            job = _job_from_row(cursor, cursor.fetchone())
            self._event(cursor, job.command, self._prefix(job) + ".running", {"fencing_token": job.fencing_token})
            return job

    def _owned(self, cursor, job, worker_id):
        cursor.execute(
            """SELECT status FROM runtime_jobs WHERE id=%s AND lease_owner=%s AND fencing_token=%s
               AND status IN ('running','cancel_requested') AND lease_expires_at > clock_timestamp() FOR UPDATE""",
            (job.command.job_id, worker_id, job.fencing_token),
        )
        row = cursor.fetchone()
        if not row:
            raise FencingTokenError("worker lease is no longer valid")
        return row[0]

    def renew(self, job, worker_id, lease_seconds):
        with self.scope(job.command.workspace_id) as cursor:
            if self._owned(cursor, job, worker_id) == "cancel_requested":
                raise JobCancelled()
            cursor.execute(
                """UPDATE runtime_jobs SET lease_expires_at=clock_timestamp()+(%s*interval '1 second'),
                   heartbeat_at=now() WHERE id=%s""",
                (lease_seconds, job.command.job_id),
            )

    def finish(self, job, worker_id, status, result, *, retry_seconds=0):
        if status not in {"completed", "failed", "cancelled", "accepted", "waiting_review"}:
            raise ValueError("invalid worker outcome")
        with self.scope(job.command.workspace_id) as cursor:
            current = self._owned(cursor, job, worker_id)
            if current == "cancel_requested":
                status = "cancelled"
            if status == "cancelled":
                result = self._media_cancellation_payload(cursor, job)
            if status == "completed" and "_knowledge_commit" in result:
                result = self._finish_knowledge_operation(cursor, job, result)
            if job.command.command_type == "memory.refresh" and status == "completed":
                self._commit_memory(cursor, job.command, result)
            cursor.execute(
                """UPDATE runtime_jobs SET status=%s,result=%s,error=%s,lease_owner=NULL,lease_expires_at=NULL,
                   next_retry_at=now()+(%s*interval '1 second'),updated_at=now() WHERE id=%s
                   AND lease_owner=%s AND fencing_token=%s AND lease_expires_at>clock_timestamp()""",
                (
                    status,
                    _json(result if status in {"completed", "waiting_review", "cancelled"} else {}),
                    _json(result if status in {"failed", "accepted"} else {}),
                    retry_seconds,
                    job.command.job_id,
                    worker_id,
                    job.fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise FencingTokenError("worker lease expired during commit")
            event_type = "retry_scheduled" if status == "accepted" else status
            event_payload = result
            if job.command.command_type == "memory.refresh" and status == "completed":
                event_payload = {
                    "document_version_id": job.command.payload["document_version_id"],
                    "node_count": len(result["nodes"]),
                }
            self._event(cursor, job.command, self._prefix(job) + "." + event_type, event_payload)
            if status == "completed" and job.command.command_type != "memory.refresh" and result.get("content_commit"):
                source = result["content_commit"]
                if not isinstance(source, dict) or not source.get("document_version_id"):
                    raise ValueError("content_commit requires document_version_id")
                self._event(
                    cursor,
                    job.command,
                    "content.version.committed",
                    {
                        **source,
                        "capability_snapshot_hash": job.command.capability_snapshot_hash,
                        "source_execution_snapshot": (
                            job.command.execution_snapshot.model_dump(mode="json")
                            if job.command.execution_snapshot is not None
                            else None
                        ),
                    },
                )

    @staticmethod
    def _knowledge_request_id(job):
        return str(uuid5(NAMESPACE_URL, f"knowledge-request:{job.command.workspace_id}:{job.command.job_id}"))

    @staticmethod
    def _knowledge_pack_id(job):
        return str(
            uuid5(
                NAMESPACE_URL,
                f"knowledge-pack:{job.command.workspace_id}:{job.command.project_id}:{job.command.job_id}",
            )
        )

    def stage_knowledge_operation(self, job, worker_id, operation, arguments):
        from agent_runtime.infrastructure.postgres.knowledge import validate_knowledge_operation

        if operation not in {"knowledge.publish", "knowledge.bind", "knowledge.unbind"} or not isinstance(
            arguments, dict
        ):
            raise ValueError("unsupported knowledge operation")
        request = {
            "operation": operation,
            "arguments": arguments,
            "command_id": job.command.command_id,
            "snapshot_hash": job.command.capability_snapshot_hash,
            "snapshot_reference": (
                job.command.execution_snapshot.model_dump(mode="json") if job.command.execution_snapshot else None
            ),
        }
        with self.scope(job.command.workspace_id) as cursor:
            if self._owned(cursor, job, worker_id) == "cancel_requested":
                raise JobCancelled()
            cursor.execute("SELECT payload FROM runtime_jobs WHERE id=%s", (job.command.job_id,))
            stored_command = cursor.fetchone()[0]
            if (
                stored_command.get("command_type") != "workflow.run"
                or stored_command.get("command_id") != request["command_id"]
                or stored_command.get("capability_snapshot_hash") != request["snapshot_hash"]
                or stored_command.get("execution_snapshot") != request["snapshot_reference"]
            ):
                raise ValueError("staged knowledge command identity conflict")
            validate_knowledge_operation(cursor, job.command, operation, arguments)
            cursor.execute(
                """INSERT INTO runtime_maintenance_runs(id,workspace_id,project_id,operation,result)
                VALUES(%s,%s,%s,'knowledge.request',%s) ON CONFLICT(id) DO NOTHING""",
                (self._knowledge_request_id(job), job.command.workspace_id, job.command.project_id, _json(request)),
            )
            cursor.execute(
                "SELECT result FROM runtime_maintenance_runs WHERE id=%s AND operation='knowledge.request'",
                (self._knowledge_request_id(job),),
            )
            previous = cursor.fetchone()
            if not previous or previous[0] != request:
                raise ValueError("staged knowledge request conflict")
        result = (
            {"id": self._knowledge_pack_id(job)}
            if operation == "knowledge.publish"
            else {"pack_id": arguments.get("pack_id"), "shared": operation == "knowledge.bind"}
        )
        return {"operation": operation, "operation_result": result, "_knowledge_commit": request}

    def _finish_knowledge_operation(self, cursor, job, result):
        from agent_runtime.infrastructure.postgres.knowledge import commit_knowledge_operation

        request = result["_knowledge_commit"]
        cursor.execute(
            """SELECT result FROM runtime_maintenance_runs WHERE id=%s AND workspace_id=%s AND project_id=%s
            AND operation='knowledge.request'""",
            (self._knowledge_request_id(job), job.command.workspace_id, job.command.project_id),
        )
        persisted = cursor.fetchone()
        if (
            not persisted
            or not isinstance(request, dict)
            or persisted[0] != request
            or result.get("operation") != request.get("operation")
        ):
            raise ValueError("staged knowledge request identity conflict")
        if job.command.command_type != "workflow.run":
            raise ValueError("knowledge mutation requires a workflow task")
        operation = request["operation"]
        committed = commit_knowledge_operation(
            cursor, job.command, operation, request["arguments"], self._knowledge_pack_id(job)
        )
        return {"operation": operation, "operation_result": committed}

    @staticmethod
    def _media_cancellation_payload(cursor, job):
        """Cancel local work without claiming an unobserved remote cancellation.

        The caller holds the runtime_jobs row lock. This helper records intent
        only; it never issues a provider DELETE or exposes request/result URLs.
        """
        cursor.execute(
            """SELECT state,remote_task_id FROM media_tasks
            WHERE workspace_id=%s AND project_id=%s AND job_id=%s FOR UPDATE""",
            (job.command.workspace_id, job.command.project_id, job.command.job_id),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """SELECT state,vendor_request_id FROM generated_media_tasks
                WHERE workspace_id=%s AND project_id=%s AND job_id=%s FOR UPDATE""",
                (job.command.workspace_id, job.command.project_id, job.command.job_id),
            )
            generated = cursor.fetchone()
            if generated is None:
                return RuntimeMQStore._composition_cancellation_payload(cursor, job)
            state, provider_request_id = generated
            next_state = {"prepared": "cancelled", "submitting": "submission_unknown"}.get(state, state)
            if next_state != state:
                cursor.execute(
                    """UPDATE generated_media_tasks SET state=%s,updated_at=now()
                    WHERE workspace_id=%s AND project_id=%s AND job_id=%s""",
                    (next_state, job.command.workspace_id, job.command.project_id, job.command.job_id),
                )
            return {
                "media_status": next_state,
                "provider_request_id": provider_request_id,
                "remote_cancel_confirmed": False,
                "remote_work_terminal": next_state in {"outputs_ready", "failed", "cancelled"},
            }
        state, remote_task_id = row
        next_state = {"prepared": "cancelled", "submitting": "submission_unknown", "running": "cancel_pending"}.get(
            state, state
        )
        if next_state != state:
            cursor.execute(
                """UPDATE media_tasks SET state=%s,updated_at=now()
                WHERE workspace_id=%s AND project_id=%s AND job_id=%s""",
                (next_state, job.command.workspace_id, job.command.project_id, job.command.job_id),
            )
        return {
            "media_status": next_state,
            "remote_task_id": remote_task_id,
            "remote_cancel_confirmed": next_state == "cancelled" and remote_task_id is not None,
            "remote_work_terminal": next_state in {"succeeded", "failed", "cancelled"},
        }

    @staticmethod
    def _composition_cancellation_payload(cursor, job):
        cursor.execute(
            "SELECT state FROM composition_runs WHERE workspace_id=%s AND project_id=%s AND job_id=%s FOR UPDATE",
            (job.command.workspace_id, job.command.project_id, job.command.job_id),
        )
        row = cursor.fetchone()
        if row is None:
            return {}
        state = row[0]
        updated = {"prepared": "cancelled", "rendering": "cancel_requested"}.get(state, state)
        if updated != state:
            cursor.execute(
                "UPDATE composition_runs SET state=%s,updated_at=now() WHERE job_id=%s", (updated, job.command.job_id)
            )
        return {"media_status": updated, "local_render_terminal": updated in {"cancelled", "outputs_ready"}}

    @staticmethod
    def _prefix(job):
        return "memory" if job.command.command_type == "memory.refresh" else "job"

    def _event(self, cursor, command, kind, payload, causation_id=None):
        cursor.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM runtime_event_outbox WHERE job_id=%s", (command.job_id,)
        )
        event = RuntimeEvent(
            event_id=str(uuid4()),
            event_type=kind,
            workspace_id=command.workspace_id,
            project_id=command.project_id,
            job_id=command.job_id,
            workflow_run_id=command.workflow_run_id,
            sequence=cursor.fetchone()[0],
            operation_id=str(uuid4()),
            trace_id=command.trace_id,
            occurred_at=datetime.now(UTC),
            causation_id=causation_id or command.command_id,
            payload=payload,
        )
        cursor.execute(
            """INSERT INTO runtime_event_outbox(event_id,workspace_id,job_id,sequence,event_type,payload)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (
                event.event_id,
                event.workspace_id,
                event.job_id,
                event.sequence,
                event.event_type,
                _json(event.model_dump(mode="json")),
            ),
        )
        return event

    def schedule_memory(self, event, topic):
        if event.event_type != "content.version.committed":
            raise ValueError("memory scheduler requires content commit event")
        version = event.payload.get("document_version_id")
        if not version:
            raise ValueError("content event requires document_version_id")
        with self.scope(event.workspace_id) as cursor:
            cursor.execute(
                """INSERT INTO memory_event_inbox(event_id,workspace_id,payload) VALUES(%s,%s,%s)
                   ON CONFLICT DO NOTHING RETURNING event_id""",
                (event.event_id, event.workspace_id, _json(event.model_dump(mode="json"))),
            )
            if not cursor.fetchone():
                cursor.execute("SELECT payload FROM memory_event_inbox WHERE event_id=%s", (event.event_id,))
                previous = cursor.fetchone()
                if not previous or previous[0] != event.model_dump(mode="json"):
                    raise ValueError("event_id payload conflict")
                return
            identity = str(uuid5(NAMESPACE_URL, f"memory:{event.workspace_id}:{event.project_id}:{version}:refresh-v1"))
            command = RuntimeCommand(
                command_id=identity,
                command_type="memory.refresh",
                workspace_id=event.workspace_id,
                project_id=event.project_id,
                job_id=identity,
                workflow_run_id=event.workflow_run_id,
                capability_snapshot_hash=event.payload.get("capability_snapshot_hash", "0" * 64),
                trace_id=event.trace_id,
                idempotency_key=identity,
                occurred_at=event.occurred_at,
                causation_id=event.event_id,
                payload={**event.payload, "source_job_id": event.job_id},
            )
            cursor.execute("INSERT INTO runtime_worker_scopes VALUES(%s) ON CONFLICT DO NOTHING", (event.workspace_id,))
            cursor.execute(
                """INSERT INTO runtime_command_outbox(command_id,workspace_id,topic,payload)
                   VALUES(%s,%s,%s,%s) ON CONFLICT(command_id) DO NOTHING""",
                (identity, event.workspace_id, topic, _json(command.model_dump(mode="json"))),
            )

    def _commit_memory(self, cursor, command, result):
        if result.get("shared_canon"):
            from agent_runtime.domain.canon import CanonState
            from agent_runtime.domain.policies import policy_hash

            canon = CanonState.model_validate(result["shared_canon"])
            if canon.project_id != command.project_id or canon.workspace_id != command.workspace_id:
                raise ValueError("memory Canon project scope conflict")
            payload = canon.model_dump(mode="json")
            cursor.execute(
                """INSERT INTO canonical_snapshots(workspace_id,project_id,content_hash,payload,
                policy_version_id,source_job_id,fencing_token) SELECT %s,%s,%s,%s,%s,id,fencing_token
                FROM runtime_jobs WHERE id=%s ON CONFLICT DO NOTHING""",
                (
                    command.workspace_id,
                    command.project_id,
                    policy_hash(payload),
                    _json(payload),
                    result["creation_policy_version_id"],
                    command.job_id,
                ),
            )
        nodes = result.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("memory handler must return non-empty nodes")
        for index, node in enumerate(nodes):
            if not node.get("content") or node.get("memory_type") not in {"summary", "fact", "embedding_source"}:
                raise ValueError("invalid memory node")
            identity = str(uuid5(NAMESPACE_URL, f"{command.command_id}:{index}"))
            cursor.execute(
                """INSERT INTO memory_nodes(id,workspace_id,project_id,document_version_id,memory_type,
                   source_id,source_version,content,content_hash,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                (
                    identity,
                    command.workspace_id,
                    command.project_id,
                    command.payload["document_version_id"],
                    node["memory_type"],
                    str(command.payload["document_version_id"]) + f":{index}",
                    command.payload.get("source_version", 1),
                    node["content"],
                    hashlib.sha256(node["content"].encode()).hexdigest(),
                    _json({**node.get("metadata", {}), "source_command_id": command.command_id}),
                ),
            )
            fact = node.get("fact")
            if fact:
                if not all(isinstance(fact.get(key), str) and fact[key] for key in ["kind", "subject", "value"]):
                    raise ValueError("invalid canonical fact")
                cursor.execute(
                    """INSERT INTO canonical_facts(id,workspace_id,project_id,fact_type,subject,value,status,evidence)
                       VALUES(%s,%s,%s,%s,%s,%s,'confirmed',%s) ON CONFLICT(id) DO NOTHING""",
                    (
                        identity,
                        command.workspace_id,
                        command.project_id,
                        fact["kind"],
                        fact["subject"],
                        _json({"text": fact["value"]}),
                        _json([{"document_version_id": command.payload["document_version_id"]}]),
                    ),
                )
            vector = node.get("embedding")
            if vector is not None:
                if (
                    not isinstance(vector, list)
                    or len(vector) != 1536
                    or any(not isinstance(number, (int, float)) or not math.isfinite(number) for number in vector)
                ):
                    raise ValueError("embedding must have 1536 finite dimensions")
                cursor.execute(
                    """INSERT INTO memory_embeddings(id,project_id,embedding_model,index_version,embedding)
                       VALUES(%s,%s,%s,%s,%s::vector) ON CONFLICT DO NOTHING""",
                    (
                        identity,
                        command.project_id,
                        result["embedding_model"],
                        result["index_version"],
                        json.dumps(vector),
                    ),
                )
        cursor.execute(
            """INSERT INTO memory_version_status(workspace_id,project_id,document_version_id,job_id,status)
               VALUES(%s,%s,%s,%s,'completed') ON CONFLICT DO NOTHING""",
            (command.workspace_id, command.project_id, command.payload["document_version_id"], command.job_id),
        )

    def creation_context(self, job, worker_id, required_versions):
        from agent_runtime.execution.errors import RetryableTaskError

        with self.scope(job.command.workspace_id) as cursor:
            self._owned(cursor, job, worker_id)
            for version in required_versions:
                cursor.execute(
                    "SELECT 1 FROM memory_version_status WHERE project_id=%s AND document_version_id=%s",
                    (job.command.project_id, version),
                )
                if not cursor.fetchone():
                    raise RetryableTaskError("required memory version not ready")
            cursor.execute(
                """SELECT id,subject,value FROM canonical_facts WHERE project_id=%s AND status='confirmed'
                   ORDER BY updated_at DESC,id LIMIT 40""",
                (job.command.project_id,),
            )
            values = [
                {
                    "kind": "canon",
                    "source": str(identity),
                    "content": subject + ": " + json.dumps(value, ensure_ascii=False),
                }
                for identity, subject, value in cursor.fetchall()
            ]
            cursor.execute(
                """SELECT id,content FROM memory_nodes WHERE project_id=%s AND memory_type='summary'
                   ORDER BY source_version DESC,created_at DESC,id LIMIT 20""",
                (job.command.project_id,),
            )
            values.extend(
                {"kind": "retrieval", "source": str(identity), "content": content}
                for identity, content in cursor.fetchall()
            )
            cursor.execute(
                """SELECT p.id,p.nodes FROM knowledge_pack_bindings b JOIN knowledge_packs p ON p.id=b.pack_id
                   WHERE b.target_project_id=%s ORDER BY p.created_at,p.id""",
                (job.command.project_id,),
            )
            for identity, nodes in cursor.fetchall():
                values.extend(
                    {"kind": "retrieval", "source": str(identity) + ":" + str(index), "content": node["content"]}
                    for index, node in enumerate(nodes)
                )
            return values

    def save_execution_plan(self, job, worker_id, snapshot, pack):
        with self.scope(job.command.workspace_id) as cursor:
            self._owned(cursor, job, worker_id)
            cursor.execute(
                """INSERT INTO execution_plans(job_id,capability_snapshot_hash,plan) VALUES(%s,%s,%s)
                   ON CONFLICT(job_id) DO NOTHING""",
                (job.command.job_id, snapshot.snapshot_hash, _json(snapshot.model_dump(mode="json"))),
            )
            cursor.execute(
                "SELECT COALESCE(MAX(version),0) FROM canonical_facts WHERE project_id=%s",
                (job.command.project_id,),
            )
            canon_version = cursor.fetchone()[0]
            sources = [block.source for block in pack.blocks if block.kind == "current"]
            retrieved = [block.source for block in pack.blocks if block.kind == "retrieval"]
            cursor.execute(
                """INSERT INTO context_packs(job_id,task_id,canon_version,source_version_ids,
                   retrieved_memory_ids,token_budget,content_hash,payload)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    job.command.job_id,
                    f"attempt-{job.fencing_token}",
                    canon_version,
                    sources,
                    retrieved,
                    max(pack.token_count, 1),
                    pack.context_hash,
                    _json(pack.model_dump(mode="json")),
                ),
            )

    def record_model_call(self, job, worker_id, purpose, provider, model, prompt_hash, usage, metadata):
        with self.scope(job.command.workspace_id) as cursor:
            self._owned(cursor, job, worker_id)
            cursor.execute(
                """INSERT INTO model_calls(job_id,provider,model,prompt_hash,input_tokens,output_tokens,status,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s,'completed',%s)""",
                (
                    job.command.job_id,
                    provider,
                    model,
                    prompt_hash,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    _json({**metadata, "purpose": purpose, "fencing_token": job.fencing_token}),
                ),
            )
            if metadata.get("prompt_version_id"):
                pack = metadata.get("prompt_pack_version_id") or metadata["prompt_node"]
                cursor.execute(
                    """INSERT INTO prompt_renderings(job_id,prompt_pack_id,prompt_version,context_hash,
                       input_hash,output_schema,token_count) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        job.command.job_id,
                        pack,
                        metadata["prompt_version_id"],
                        metadata["variables_hash"],
                        prompt_hash,
                        metadata["output_schema"],
                        usage.get("input_tokens", 0),
                    ),
                )

            identity = str(uuid4())
            self._event(
                cursor,
                job.command,
                "project.usage.recorded",
                {
                    "id": identity,
                    "purpose": purpose,
                    "provider": provider,
                    "model_name": model,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                },
            )
            safe_metadata = {
                key: metadata[key]
                for key in (
                    "prompt_version_id",
                    "prompt_pack_version_id",
                    "prompt_node",
                    "renderer_version",
                    "variables_hash",
                    "elapsed_ms",
                )
                if key in metadata
            }
            self._event(
                cursor,
                job.command,
                "project.trace.recorded",
                {"id": identity, "purpose": purpose, "provider": provider, "model_name": model, **safe_metadata},
            )

    def model_budget(self, job, worker_id, limit):
        with self.scope(job.command.workspace_id) as cursor:
            self._owned(cursor, job, worker_id)
            if limit:
                cursor.execute(
                    """SELECT COALESCE(sum(m.input_tokens+m.output_tokens),0) FROM model_calls m
                               JOIN runtime_jobs j ON j.id=m.job_id WHERE j.project_id=%s""",
                    (job.command.project_id,),
                )
                if cursor.fetchone()[0] >= limit:
                    from agent_runtime.execution.model_context import ModelBudgetExceededError

                    raise ModelBudgetExceededError("project model token budget exhausted")

    def relay(self, workspace_id, event_topic, publish, retry_seconds):
        count = 0
        for table, id_key, type_key in (
            ("runtime_event_outbox", "event_id", "event_type"),
            ("runtime_command_outbox", "command_id", "command_type"),
        ):
            with self.scope(workspace_id) as cursor:
                # Whitelisted table names above; payloads and scopes stay parameterized.
                cursor.execute(
                    f"""SELECT id,payload FROM {table} WHERE published_at IS NULL AND next_attempt_at<=now()
                        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 16"""
                )
                for identity, payload in cursor.fetchall():
                    topic = event_topic
                    if table == "runtime_command_outbox":
                        cursor.execute("SELECT topic FROM runtime_command_outbox WHERE id=%s", (identity,))
                        topic = cursor.fetchone()[0]
                    try:
                        publish(topic, payload[id_key], payload[type_key], json.dumps(payload).encode())
                    except Exception:
                        cursor.execute(
                            f"""UPDATE {table} SET attempts=attempts+1,
                                next_attempt_at=now()+(%s*interval '1 second') WHERE id=%s""",
                            (retry_seconds, identity),
                        )
                        continue
                    cursor.execute(
                        f"UPDATE {table} SET published_at=now(),attempts=attempts+1 WHERE id=%s", (identity,)
                    )
                    count += 1
        return count
