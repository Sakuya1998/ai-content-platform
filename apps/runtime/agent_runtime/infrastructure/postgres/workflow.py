"""Fenced state and typed records shared by every content workflow."""

import hashlib
import json
from contextlib import contextmanager
from uuid import uuid4

from agent_runtime.execution.errors import JobCancelled, RetryableTaskError
from agent_runtime.infrastructure.postgres.jobs import _json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class WorkflowStore:
    def __init__(self, execution, *, workflow_id, thread_id=None):
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise ValueError("workflow identity is required")
        self.execution = execution
        self.workspace_id = execution.job.command.workspace_id
        self.project_id = execution.job.command.project_id
        self.workflow_id = workflow_id
        self.thread_id = thread_id or self.project_id

    @property
    def scope(self):
        return self.workspace_id, self.project_id, self.workflow_id

    def check_scope(self, project_id):
        if project_id != self.project_id:
            raise ValueError("workflow project scope conflict")

    @contextmanager
    def transaction(self, *, write=False):
        with self.execution.store.scope(self.workspace_id) as cursor:
            if write and (
                self.execution.store._owned(cursor, self.execution.job, self.execution.settings.worker_id)
                == "cancel_requested"
            ):
                raise JobCancelled("workflow execution cancelled")
            yield cursor

    @contextmanager
    def project_lock(self):
        with self.execution.store._pool.connection() as connection, connection.cursor() as cursor:
            key = self.workspace_id + ":" + self.project_id
            cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,))
            if not cursor.fetchone()[0]:
                raise RetryableTaskError("another worker is executing this project")
            try:
                yield
            finally:
                cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))

    def initialize(self, state):
        self.check_scope(state.get("project_id", self.project_id))
        with self.transaction(write=True) as cursor:
            cursor.execute(
                """INSERT INTO workflow_states(workspace_id,project_id,workflow_id,state,source_job_id,fencing_token)
                   VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,project_id,workflow_id) DO NOTHING""",
                (*self.scope, _json(state), self.execution.job.command.job_id, self.execution.job.fencing_token),
            )

    def state(self):
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT state FROM workflow_states WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s",
                self.scope,
            )
            row = cursor.fetchone()
            return dict(row[0]) if row else None

    def _save_state(self, cursor, state, *, merge=False):
        expression = "state || %s" if merge else "%s"
        cursor.execute(
            f"""UPDATE workflow_states SET state={expression},source_job_id=%s,fencing_token=%s,updated_at=now()
                WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s""",
            (_json(state), self.execution.job.command.job_id, self.execution.job.fencing_token, *self.scope),
        )

    def save_state(self, state):
        self.check_scope(state.get("project_id", self.project_id))
        with self.transaction(write=True) as cursor:
            self._save_state(cursor, state)

    def save_progress(self, progress):
        with self.transaction(write=True) as cursor:
            self._save_state(cursor, progress, merge=True)

    def record(self, kind, payload, *, category, scope_key="", record_id=None, deduplicate=True, content_hash=None):
        content_hash = content_hash or digest(payload)
        with self.transaction(write=True) as cursor:
            cursor.execute(
                "SELECT project_id FROM workflow_states WHERE workspace_id=%s AND project_id=%s "
                "AND workflow_id=%s FOR UPDATE",
                self.scope,
            )
            if cursor.fetchone() is None:
                raise ValueError("workflow state must be initialized before recording results")
            if deduplicate:
                cursor.execute(
                    """SELECT payload FROM workflow_records WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s
                       AND record_kind=%s AND scope_key=%s AND content_hash=%s AND category=%s""",
                    (*self.scope, kind, scope_key, content_hash, category),
                )
                if row := cursor.fetchone():
                    return row[0]
            cursor.execute(
                """SELECT COALESCE(MAX(version_number),0)+1 FROM workflow_records
                   WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s AND record_kind=%s AND scope_key=%s""",
                (*self.scope, kind, scope_key),
            )
            version = cursor.fetchone()[0]
            identity = record_id or str(uuid4())
            result = {**payload, "id": identity, "version_number": version}
            cursor.execute(
                """INSERT INTO workflow_records(workspace_id,project_id,workflow_id,category,record_kind,record_id,
                   scope_key,version_number,content_hash,payload,source_job_id,fencing_token)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(workspace_id,project_id,workflow_id,record_kind,record_id) DO UPDATE SET
                   payload=excluded.payload,content_hash=excluded.content_hash,source_job_id=excluded.source_job_id,
                   fencing_token=excluded.fencing_token""",
                (
                    *self.scope,
                    category,
                    kind,
                    identity,
                    scope_key,
                    version,
                    content_hash,
                    _json(result),
                    self.execution.job.command.job_id,
                    self.execution.job.fencing_token,
                ),
            )
            return result

    def records(self, kind, scope_key=None):
        with self.transaction() as cursor:
            cursor.execute(
                """SELECT payload FROM workflow_records WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s
                   AND record_kind=%s AND (%s::text IS NULL OR scope_key=%s) ORDER BY scope_key,version_number""",
                (*self.scope, kind, scope_key, scope_key),
            )
            return [row[0] for row in cursor.fetchall()]
