"""Fenced, repeatable local rendering with immutable input and artifact provenance."""

import hashlib
import json

from agent_runtime.domain.generated_media import MediaArtifact
from agent_runtime.execution.leases import FencingTokenError
from agent_runtime.infrastructure.postgres.jobs import _json
from agent_runtime.infrastructure.postgres.media import PostgresMediaTaskStore


class CompositionStore(PostgresMediaTaskStore):
    @staticmethod
    def _load(cursor, job):
        cursor.execute(
            "SELECT * FROM composition_runs WHERE workspace_id=%s AND project_id=%s AND job_id=%s FOR UPDATE",
            (job.command.workspace_id, job.command.project_id, job.command.job_id),
        )
        row = cursor.fetchone()
        return dict(zip((c.name for c in cursor.description), row, strict=True)) if row else None

    def get(self, job, worker_id):
        with self._scope(job, worker_id) as cursor:
            run = self._load(cursor, job)
            if run and run["state"] == "rendering" and run["fencing_token"] != job.fencing_token:
                run = self._write(cursor, job, worker_id, {"state": "prepared"})
            return run

    def reserve(self, job, worker_id, request):
        digest = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        with self._scope(job, worker_id) as cursor:
            previous = self._load(cursor, job)
            if previous:
                if previous["request_hash"] != digest:
                    raise ValueError("composition request conflict")
                return previous
            cursor.execute(
                """INSERT INTO composition_runs(workspace_id,project_id,job_id,request,request_hash,state,
                fencing_token) SELECT workspace_id,project_id,id,%s,%s,'prepared',fencing_token FROM runtime_jobs
                WHERE id=%s AND workspace_id=%s AND project_id=%s AND lease_owner=%s AND fencing_token=%s
                AND status IN ('running','cancel_requested') AND lease_expires_at>clock_timestamp() RETURNING job_id""",
                (_json(request), digest, *self._lease(job, worker_id)),
            )
            if not cursor.fetchone():
                raise FencingTokenError("worker lease expired while reserving composition")
            return self._load(cursor, job)

    def begin(self, job, worker_id):
        with self._scope(job, worker_id) as cursor:
            run = self._required(cursor, job)
            if run["state"] not in {"prepared", "rendering"}:
                raise ValueError("composition cannot render from " + run["state"])
            return self._write(cursor, job, worker_id, {"state": "rendering"})

    def archived(self, job, worker_id, artifact, probe):
        artifact = MediaArtifact.model_validate(artifact).model_dump(mode="json")
        prefix = f"workspaces/{job.command.workspace_id}/projects/{job.command.project_id}/assets/"
        if artifact["content_type"] != "video/mp4" or not artifact["object_key"].startswith(prefix):
            raise ValueError("composition artifact scope conflict")
        with self._scope(job, worker_id) as cursor:
            run = self._required(cursor, job)
            if run["artifact"] is not None:
                if run["artifact"] != artifact or run["probe"] != probe:
                    raise ValueError("composition output conflict")
                return run
            if run["state"] not in {"prepared", "rendering", "outputs_ready"}:
                raise ValueError("composition result cannot be committed")
            return self._write(cursor, job, worker_id, {"state": "outputs_ready", "artifact": artifact, "probe": probe})

    def cancelled(self, job, worker_id):
        with self._scope(job, worker_id) as cursor:
            run = self._required(cursor, job)
            if run["state"] == "outputs_ready":
                return run
            return self._write(cursor, job, worker_id, {"state": "cancelled"})

    def _write(self, cursor, job, worker_id, changes):
        if not changes.keys() <= {"state", "artifact", "probe"}:
            raise ValueError("invalid composition update")
        assignment = ",".join(f"{k}=%s" for k in changes)
        values = [_json(v) if k in {"artifact", "probe"} else v for k, v in changes.items()]
        cursor.execute(
            f"""UPDATE composition_runs SET {assignment},fencing_token=%s,updated_at=now()
            WHERE workspace_id=%s AND project_id=%s AND job_id=%s AND EXISTS(SELECT 1 FROM runtime_jobs
            WHERE id=%s AND workspace_id=%s AND project_id=%s AND lease_owner=%s AND fencing_token=%s
            AND status IN ('running','cancel_requested') AND lease_expires_at>clock_timestamp()) RETURNING job_id""",
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
            raise FencingTokenError("worker lease expired while committing composition")
        return self._required(cursor, job)
