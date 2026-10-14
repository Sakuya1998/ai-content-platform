"""Durable video task creation and recovery, fenced by the Runtime job lease."""

import hashlib
import json
from contextlib import contextmanager
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from agent_runtime.domain.media import MediaTask, VideoRequest, VideoTaskStatus
from agent_runtime.execution.leases import FencingTokenError
from agent_runtime.infrastructure.postgres.jobs import RuntimeDatabaseUnavailable, _json


class PostgresMediaTaskStore:
    def __init__(self, source=None, *, database_url: str | None = None, pool=None):
        """Borrow a Runtime store/pool or own a pool for a URL/RuntimeSettings."""
        self._owns_pool = False
        if pool is not None:
            self._pool = pool
        elif hasattr(source, "_pool"):
            self._pool = source._pool
        elif hasattr(source, "connection"):
            self._pool = source
        else:
            from psycopg_pool import ConnectionPool

            url = database_url or (source if isinstance(source, str) else getattr(source, "database_url", ""))
            if not url:
                raise RuntimeDatabaseUnavailable("RUNTIME_DATABASE_URL is empty")
            self._pool = ConnectionPool(url, open=True, kwargs={"autocommit": False})
            self._owns_pool = True

    def close(self) -> None:
        if self._owns_pool:
            self._pool.close()

    @contextmanager
    def _scope(self, job, worker_id):
        with self._pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.workspace_id',%s,true)", (job.command.workspace_id,))
            cursor.execute("SELECT set_config('app.project_id',%s,true)", (job.command.project_id,))
            cursor.execute(
                """SELECT id FROM runtime_jobs WHERE id=%s AND workspace_id=%s AND project_id=%s
                AND lease_owner=%s AND fencing_token=%s AND status IN ('running','cancel_requested')
                AND lease_expires_at>clock_timestamp() FOR UPDATE""",
                self._lease(job, worker_id),
            )
            if not cursor.fetchone():
                raise FencingTokenError("worker lease is no longer valid")
            yield cursor

    @staticmethod
    def _lease(job, worker_id) -> tuple:
        return (job.command.job_id, job.command.workspace_id, job.command.project_id, worker_id, job.fencing_token)

    @staticmethod
    def _load(cursor, job) -> MediaTask | None:
        cursor.execute(
            "SELECT * FROM media_tasks WHERE workspace_id=%s AND project_id=%s AND job_id=%s FOR UPDATE",
            (job.command.workspace_id, job.command.project_id, job.command.job_id),
        )
        row = cursor.fetchone()
        return (
            MediaTask.model_validate(dict(zip((item.name for item in cursor.description), row, strict=True)))
            if row
            else None
        )

    def get(self, job, worker_id: str) -> MediaTask | None:
        with self._scope(job, worker_id) as cursor:
            task = self._load(cursor, job)
            if task and task.state == "submitting" and task.fencing_token != job.fencing_token:
                task = self._write(cursor, job, worker_id, {"state": "submission_unknown"})
            return task

    def reserve(
        self, job, worker_id: str, request: VideoRequest, route_identity: dict[str, Any], *, review: dict | None = None
    ) -> MediaTask:
        """Reserve once; callers recover via get() before planning a new request."""
        if "api_key" in route_identity or "secret" in route_identity:
            raise ValueError("route identity cannot persist credentials")
        payload = request.model_dump(mode="json")
        encoded = json.dumps(
            {"request": payload, "route": route_identity},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._scope(job, worker_id) as cursor:
            existing = self._load(cursor, job)
            if existing:
                if existing.request_hash != digest:
                    raise ValueError("media task request or route conflict")
                if existing.state == "submitting" and existing.fencing_token != job.fencing_token:
                    existing = self._write(cursor, job, worker_id, {"state": "submission_unknown"})
                return existing
            cursor.execute(
                """INSERT INTO media_tasks(workspace_id,project_id,job_id,request,request_hash,route_identity,
                review,state,fencing_token) SELECT workspace_id,project_id,id,%s,%s,%s,%s,'prepared',fencing_token
                FROM runtime_jobs WHERE id=%s AND workspace_id=%s AND project_id=%s AND lease_owner=%s
                AND fencing_token=%s AND lease_expires_at>clock_timestamp() RETURNING job_id""",
                (_json(payload), digest, _json(route_identity), _json(review or {}), *self._lease(job, worker_id)),
            )
            if not cursor.fetchone():
                raise FencingTokenError("worker lease expired during media commit")
            return self._load(cursor, job)

    def begin_submit(self, job, worker_id: str) -> MediaTask:
        """Return only after the submitting transaction commits, before HTTP POST."""
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            if task.state != "prepared":
                raise ValueError("only a prepared media task can submit")
            if task.review.get("approved") is not True:
                raise ValueError("media plan must be approved before submission")
            return self._write(cursor, job, worker_id, {"state": "submitting"})

    def approve_plan(self, job, worker_id: str) -> MediaTask:
        """Persist an explicit approval without changing the frozen request."""
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            if task.state != "prepared":
                raise ValueError("only a prepared media plan can be approved")
            return self._write(cursor, job, worker_id, {"review": {**task.review, "approved": True}})

    def submitted(self, job, worker_id: str, remote_task_id: str) -> MediaTask:
        if not isinstance(remote_task_id, str) or not remote_task_id.strip():
            raise ValueError("remote media task ID is required")
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            if task.remote_task_id:
                if task.remote_task_id != remote_task_id:
                    raise ValueError("remote media task ID conflict")
                return task
            if task.state not in {"submitting", "submission_unknown"}:
                raise ValueError("invalid media task transition to running")
            return self._write(cursor, job, worker_id, {"state": "running", "remote_task_id": remote_task_id})

    def mark_unknown(self, job, worker_id: str) -> MediaTask:
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            if task.state not in {"submitting", "submission_unknown"}:
                raise ValueError("invalid media task transition to submission_unknown")
            return self._write(cursor, job, worker_id, {"state": "submission_unknown"})

    def update_status(self, job, worker_id: str, status: VideoTaskStatus) -> MediaTask:
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            allowed = task.state in {"running", "cancel_pending"} or task.state == status.state
            # A definite HTTP rejection can fail before a remote ID exists.
            allowed = allowed or (task.state == "submitting" and status.state == "failed")
            if not allowed:
                raise ValueError("invalid media task status transition")
            payload = status.model_dump(mode="json")
            if task.state == "cancel_pending" and status.state == "running":
                payload["state"] = "cancel_pending"
            updated = self._write(cursor, job, worker_id, payload)
            self._record_video_usage(cursor, updated)
            return updated

    @staticmethod
    def _record_video_usage(cursor, task: MediaTask) -> None:
        """Record an external task once, atomically with its durable terminal state."""
        if not task.remote_task_id or task.state not in {"succeeded", "failed", "cancelled"}:
            return

        def tokens(*names):
            return next(
                (task.usage[name] for name in names if type(task.usage.get(name)) is int and task.usage[name] >= 0), 0
            )

        identity = uuid5(
            NAMESPACE_URL,
            "media:" + json.dumps([task.workspace_id, task.job_id, task.remote_task_id], separators=(",", ":")),
        )
        metric_names = {"input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "total_tokens"}
        raw_usage = {
            name: value
            for name, value in task.usage.items()
            if name in metric_names and type(value) is int and value >= 0
        }
        metadata = {
            "purpose": "video_generation",
            "modality": "video",
            "profile_version_id": task.route_identity.get("profile_version_id", ""),
            "remote_task_id": task.remote_task_id,
            "raw_usage": raw_usage,
            "fencing_token": task.fencing_token,
        }
        cursor.execute(
            """INSERT INTO model_calls(id,job_id,provider,model,prompt_hash,input_tokens,output_tokens,status,metadata)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
            (
                identity,
                task.job_id,
                task.route_identity["provider"],
                task.route_identity["model_name"],
                hashlib.sha256(task.request.prompt.encode()).hexdigest(),
                tokens("input_tokens", "prompt_tokens"),
                tokens("output_tokens", "completion_tokens"),
                "completed" if task.state == "succeeded" else task.state,
                _json(metadata),
            ),
        )

    def begin_cancel(self, job, worker_id: str) -> MediaTask:
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            if task.state not in {"running", "cancel_pending"}:
                raise ValueError("invalid media task transition to cancel_pending")
            return self._write(cursor, job, worker_id, {"state": "cancel_pending"})

    def mark_cancelled(self, job, worker_id: str) -> MediaTask:
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            if task.state not in {"prepared", "cancel_pending", "cancelled"}:
                raise ValueError("invalid media task transition to cancelled")
            updated = self._write(cursor, job, worker_id, {"state": "cancelled"})
            self._record_video_usage(cursor, updated)
            return updated

    def _required(self, cursor, job) -> MediaTask:
        task = self._load(cursor, job)
        if task is None:
            raise KeyError("media task not reserved")
        return task

    def _write(self, cursor, job, worker_id, changes: dict) -> MediaTask:
        # Column names originate only from this class, never from a user request.
        allowed = {"state", "remote_task_id", "content_url", "usage", "error", "review"}
        if not changes.keys() <= allowed:
            raise ValueError("invalid media task update")
        assignment = ",".join(f"{key}=%s" for key in changes)
        values = [_json(value) if key in {"usage", "error", "review"} else value for key, value in changes.items()]
        cursor.execute(
            f"""UPDATE media_tasks SET {assignment},fencing_token=%s,updated_at=now()
            WHERE workspace_id=%s AND project_id=%s AND job_id=%s AND EXISTS
            (SELECT 1 FROM runtime_jobs WHERE id=%s AND workspace_id=%s AND project_id=%s
            AND lease_owner=%s AND fencing_token=%s AND status IN ('running','cancel_requested')
            AND lease_expires_at>clock_timestamp()) RETURNING job_id""",
            (
                *values,
                job.fencing_token,
                job.command.workspace_id,
                job.command.project_id,
                job.command.job_id,
                *self._lease(job, worker_id),
            ),
        )
        if not cursor.fetchone():
            raise FencingTokenError("worker lease expired during media commit")
        return self._required(cursor, job)
