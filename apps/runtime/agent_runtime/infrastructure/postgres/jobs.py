"""PostgreSQL Runtime job store.

The adapter implements the same lease and fencing contract as the in-memory
store. Every state mutation locks the row and checks the current fencing token,
so a worker that lost its lease cannot commit a result or publish a terminal
event.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent
from agent_runtime.execution.leases import FencingTokenError, RuntimeJob


class RuntimeDatabaseUnavailable(RuntimeError):
    """Runtime PostgreSQL is not configured or cannot be reached."""


class PostgresRuntimeJobStore:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeDatabaseUnavailable("RUNTIME_DATABASE_URL is empty")
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeDatabaseUnavailable("psycopg[binary,pool] is not installed") from exc
        self._pool = ConnectionPool(database_url, open=True, kwargs={"autocommit": False})

    def close(self) -> None:
        self._pool.close()

    def ready(self) -> bool:
        try:
            with self._pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM runtime_jobs LIMIT 1")
                cursor.fetchone()
            return True
        except Exception:
            return False

    def accept(self, command: RuntimeCommand) -> RuntimeJob:
        payload = command.model_dump(mode="json")
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.workspace_id', %s, true)", (command.workspace_id,))
            cursor.execute(
                """
                    INSERT INTO command_inbox (command_id, workspace_id, job_id, payload)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (command_id) DO NOTHING
                    RETURNING command_id
                    """,
                (command.command_id, command.workspace_id, command.job_id, _json(payload)),
            )
            inserted = cursor.fetchone() is not None
            if not inserted:
                cursor.execute("SELECT payload FROM command_inbox WHERE command_id = %s", (command.command_id,))
                row = cursor.fetchone()
                if row is None or _json_value(row[0]) != payload:
                    raise ValueError("command_id payload conflict")
            if inserted:
                cursor.execute(
                    """
                        INSERT INTO runtime_jobs (id, workspace_id, project_id, command_id, payload)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                    (command.job_id, command.workspace_id, command.project_id, command.command_id, _json(payload)),
                )
            cursor.execute("SELECT * FROM runtime_jobs WHERE id = %s FOR UPDATE", (command.job_id,))
            row = cursor.fetchone()
            if row is None:
                raise ValueError("job_id is already bound to another command")
            return _job_from_row(cursor, row)

    def get(self, job_id: str) -> RuntimeJob:
        with self._pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM runtime_jobs WHERE id = %s", (job_id,))
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"runtime job not found: {job_id}")
            return _job_from_row(cursor, row)

    def get_scoped(self, job_id: str, workspace_id: str) -> RuntimeJob:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.workspace_id', %s, true)", (workspace_id,))
            cursor.execute("SELECT * FROM runtime_jobs WHERE id = %s AND workspace_id = %s", (job_id, workspace_id))
            row = cursor.fetchone()
            if row is None:
                raise KeyError("runtime job not found")
            return _job_from_row(cursor, row)

    def record_artifact(self, job_id, workspace_id, worker_id, fencing_token, reference) -> RuntimeEvent:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.workspace_id', %s, true)", (workspace_id,))
            cursor.execute(
                """SELECT * FROM runtime_jobs WHERE id=%s AND workspace_id=%s
                   AND status='running' AND lease_owner=%s AND fencing_token=%s
                   AND lease_expires_at > clock_timestamp() FOR UPDATE""",
                (job_id, workspace_id, worker_id, fencing_token),
            )
            row = cursor.fetchone()
            if row is None:
                raise FencingTokenError("worker lease is no longer valid")
            job = _job_from_row(cursor, row)
            cursor.execute(
                """SELECT o.payload FROM artifact_records a JOIN runtime_event_outbox o ON a.event_id=o.event_id
                   WHERE a.job_id=%s AND a.bucket=%s AND a.object_key=%s""",
                (job_id, reference.bucket, reference.object_key),
            )
            existing = cursor.fetchone()
            if existing:
                event = RuntimeEvent.model_validate(_json_value(existing[0]))
                if event.payload.get("sha256") != reference.sha256:
                    raise ValueError("artifact checksum conflict")
                return event
            cursor.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runtime_event_outbox WHERE job_id=%s",
                (job_id,),
            )
            event = RuntimeEvent(
                event_id=f"event_{uuid4().hex}",
                event_type="artifact.created",
                workspace_id=workspace_id,
                project_id=job.command.project_id,
                job_id=job_id,
                workflow_run_id=job.command.workflow_run_id,
                sequence=int(cursor.fetchone()[0]),
                operation_id=f"artifact_{reference.sha256}",
                trace_id=job.command.trace_id,
                payload=asdict(reference),
            )
            cursor.execute(
                """INSERT INTO runtime_event_outbox(event_id,workspace_id,job_id,sequence,event_type,payload)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    event.event_id,
                    workspace_id,
                    job_id,
                    event.sequence,
                    event.event_type,
                    _json(event.model_dump(mode="json")),
                ),
            )
            cursor.execute(
                """INSERT INTO artifact_records
                   (job_id,workspace_id,bucket,object_key,sha256,size_bytes,reference,event_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    job_id,
                    workspace_id,
                    reference.bucket,
                    reference.object_key,
                    reference.sha256,
                    reference.size_bytes,
                    _json(asdict(reference)),
                    event.event_id,
                ),
            )
            return event

    def claim(self, job_id: str, worker_id: str, *, lease_seconds: int) -> RuntimeJob:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM runtime_jobs WHERE id = %s FOR UPDATE", (job_id,))
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"runtime job not found: {job_id}")
            job = _job_from_row(cursor, row)
            now = datetime.now(UTC)
            active_lease = (
                job.status == "running"
                and job.lease_expires_at
                and job.lease_expires_at > now
                and job.worker_id != worker_id
            )
            if active_lease:
                raise FencingTokenError("job is owned by another worker")
            if job.status not in {"accepted", "running"}:
                raise FencingTokenError(f"job cannot be claimed from {job.status}")
            cursor.execute(
                """
                    UPDATE runtime_jobs
                    SET status = 'running', lease_owner = %s,
                        lease_expires_at = now() + (%s * interval '1 second'),
                        heartbeat_at = now(), fencing_token = fencing_token + 1,
                        updated_at = now()
                    WHERE id = %s
                    RETURNING *
                """,
                (worker_id, max(lease_seconds, 1), job_id),
            )
            return _job_from_row(cursor, cursor.fetchone())

    def heartbeat(self, job_id: str, worker_id: str, fencing_token: int, *, lease_seconds: int) -> None:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                    UPDATE runtime_jobs
                    SET lease_expires_at = now() + (%s * interval '1 second'), heartbeat_at = now(), updated_at = now()
                    WHERE id = %s AND status = 'running' AND lease_owner = %s
                      AND fencing_token = %s AND lease_expires_at > now()
                    """,
                (max(lease_seconds, 1), job_id, worker_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise FencingTokenError("worker lease is no longer valid")

    def complete(self, job_id: str, worker_id: str, fencing_token: int, result: dict) -> None:
        self._finish(job_id, worker_id, fencing_token, "completed", result, {})

    def fail(self, job_id: str, worker_id: str, fencing_token: int, error: dict) -> None:
        self._finish(job_id, worker_id, fencing_token, "failed", {}, error)

    def next_event(self, job_id: str, event_type: str, payload: dict) -> RuntimeEvent:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM runtime_jobs WHERE id = %s FOR UPDATE", (job_id,))
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"runtime job not found: {job_id}")
            job = _job_from_row(cursor, row)
            cursor.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runtime_event_outbox WHERE job_id = %s",
                (job_id,),
            )
            sequence = int(cursor.fetchone()[0])
            event = RuntimeEvent(
                event_id=f"event_{uuid4().hex[:16]}",
                event_type=event_type,
                workspace_id=job.command.workspace_id,
                project_id=job.command.project_id,
                job_id=job.command.job_id,
                workflow_run_id=job.command.workflow_run_id,
                sequence=sequence,
                operation_id=f"op_{uuid4().hex[:16]}",
                trace_id=job.command.trace_id,
                payload=payload,
            )
            cursor.execute(
                """
                INSERT INTO runtime_event_outbox
                    (event_id, workspace_id, job_id, sequence, event_type, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
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

    def _finish(self, job_id: str, worker_id: str, fencing_token: int, status: str, result: dict, error: dict) -> None:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                    UPDATE runtime_jobs SET status=%s, result=%s, error=%s,
                        lease_owner=NULL, lease_expires_at=NULL, updated_at=now()
                    WHERE id=%s AND status='running' AND lease_owner=%s
                      AND fencing_token=%s AND lease_expires_at > now()
                    """,
                (status, _json(result), _json(error), job_id, worker_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise FencingTokenError("worker lease is no longer valid")

    @contextmanager
    def _transaction(self):
        with self._pool.connection() as connection, connection.transaction():
            yield connection


def _json(value: Any) -> Any:
    try:
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeDatabaseUnavailable("psycopg is not installed") from exc
    return Jsonb(value)


def _json_value(value: Any) -> Any:
    return value if isinstance(value, dict) else json.loads(value)


def _job_from_row(cursor: Any, row: Any) -> RuntimeJob:
    columns = [item.name for item in cursor.description]
    data = dict(zip(columns, row, strict=True))
    command = RuntimeCommand.model_validate(_json_value(data["payload"]))
    return RuntimeJob(
        command=command,
        status=data["status"],
        worker_id=data.get("lease_owner") or "",
        fencing_token=int(data.get("fencing_token") or 0),
        lease_expires_at=data.get("lease_expires_at"),
        result=_json_value(data.get("result") or {}),
        error=_json_value(data.get("error") or {}),
        attempt=int(data.get("attempt") or 0),
        resume_input=_json_value(data.get("resume_input") or {}),
    )
