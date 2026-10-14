"""Fenced synchronous media calls with persistent approval and immutable outputs."""

import hashlib
import json
import math
import re
from uuid import NAMESPACE_URL, uuid5

from agent_runtime.domain.generated_media import GeneratedMediaTask, MediaArtifact, StoredMediaRequest
from agent_runtime.execution.leases import FencingTokenError
from agent_runtime.infrastructure.postgres.jobs import _json
from agent_runtime.infrastructure.postgres.media import PostgresMediaTaskStore
from agent_runtime.infrastructure.storage.keys import object_key

_USAGE_METRICS = {
    "input_tokens",
    "prompt_tokens",
    "output_tokens",
    "completion_tokens",
    "total_tokens",
    "text_tokens",
    "image_tokens",
    "audio_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "generated_images",
    "images",
    "output_images",
    "text_words",
    "input_characters",
    "output_characters",
}
_USAGE_DETAILS = {"input_tokens_details", "output_tokens_details", "prompt_tokens_details", "completion_tokens_details"}
_MEDIA_TYPES = {
    "image": {"image/png", "image/jpeg", "image/webp"},
    "audio": {"audio/mpeg", "audio/wav", "audio/ogg", "audio/aac", "audio/flac", "audio/pcm"},
    "music": {"audio/mpeg"},
    "effect": {"audio/mpeg"},
}


def _safe_usage(usage, *, depth=0):
    """Persist only known metrics that fit accounting's signed integer columns."""
    if not isinstance(usage, dict):
        raise ValueError("media usage must be an object")
    safe = {}
    for key, value in usage.items():
        if key in _USAGE_DETAILS and isinstance(value, dict) and depth < 2:
            safe[key] = _safe_usage(value, depth=depth + 1)
        elif (
            key in _USAGE_METRICS
            and type(value) in {int, float}
            and 0 <= value <= 2**31 - 1
            and (type(value) is int or math.isfinite(value))
        ):
            safe[key] = value
    return safe


class PostgresGeneratedMediaStore(PostgresMediaTaskStore):
    """Borrow the common lease scope; keep synchronous response state separate."""

    @staticmethod
    def _load(cursor, job):
        cursor.execute(
            "SELECT * FROM generated_media_tasks WHERE workspace_id=%s AND project_id=%s AND job_id=%s FOR UPDATE",
            (job.command.workspace_id, job.command.project_id, job.command.job_id),
        )
        row = cursor.fetchone()
        return (
            GeneratedMediaTask.model_validate(dict(zip((c.name for c in cursor.description), row, strict=True)))
            if row
            else None
        )

    def get(self, job, worker_id):
        with self._scope(job, worker_id) as cursor:
            task = self._load(cursor, job)
            if task and task.state == "submitting" and task.fencing_token != job.fencing_token:
                task = self._write(cursor, job, worker_id, {"state": "submission_unknown"})
            return task

    def reserve(self, job, worker_id, modality, request: StoredMediaRequest, route_identity, *, review):
        if modality not in _MEDIA_TYPES or "api_key" in route_identity:
            raise ValueError("invalid media modality or secret-bearing route")
        payload = request.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(
                {"request": payload, "route": route_identity, "modality": modality},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        with self._scope(job, worker_id) as cursor:
            existing = self._load(cursor, job)
            if existing:
                if existing.request_hash != digest:
                    raise ValueError("media request conflict")
                if existing.state == "submitting" and existing.fencing_token != job.fencing_token:
                    existing = self._write(cursor, job, worker_id, {"state": "submission_unknown"})
                return existing
            cursor.execute(
                """INSERT INTO generated_media_tasks(workspace_id,project_id,job_id,modality,request,
                request_hash,route_identity,review,state,fencing_token)
                SELECT workspace_id,project_id,id,%s,%s,%s,%s,%s,'prepared',fencing_token
                FROM runtime_jobs WHERE id=%s AND workspace_id=%s AND project_id=%s AND lease_owner=%s
                AND fencing_token=%s AND status IN ('running','cancel_requested')
                AND lease_expires_at>clock_timestamp() RETURNING job_id""",
                (
                    modality,
                    _json(payload),
                    digest,
                    _json(route_identity),
                    _json(review),
                    *self._lease(job, worker_id),
                ),
            )
            if not cursor.fetchone():
                raise FencingTokenError("worker lease expired during media commit")
            return self._load(cursor, job)

    def begin_submit(self, job, worker_id):
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            if task.state != "prepared":
                raise ValueError("only prepared media can submit")
            if task.review.get("approved") is not True:
                raise ValueError("media plan must be approved")
            return self._write(cursor, job, worker_id, {"state": "submitting"})

    def approve_plan(self, job, worker_id):
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            if task.state != "prepared":
                raise ValueError("only prepared media can be approved")
            return self._write(cursor, job, worker_id, {"review": {**task.review, "approved": True}})

    def mark_unknown(self, job, worker_id):
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            if task.state not in {"submitting", "submission_unknown"}:
                raise ValueError("invalid unknown media transition")
            return self._write(cursor, job, worker_id, {"state": "submission_unknown"})

    def rejected(self, job, worker_id):
        with self._scope(job, worker_id) as cursor:
            if self._required(cursor, job).state != "submitting":
                raise ValueError("invalid rejected media transition")
            return self._write(cursor, job, worker_id, {"state": "failed", "error": {"code": "provider_rejected"}})

    def archived(self, job, worker_id, outputs, usage, *, vendor_request_id=None):
        outputs = [MediaArtifact.model_validate(item, strict=True).model_dump(mode="json") for item in outputs]
        if not outputs:
            raise ValueError("media output must be non-empty")
        usage = _safe_usage(usage)
        if vendor_request_id is not None and (
            not isinstance(vendor_request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", vendor_request_id)
        ):
            raise ValueError("invalid media provider request ID")
        with self._scope(job, worker_id) as cursor:
            task = self._required(cursor, job)
            self._validate_outputs(job, task.modality, outputs)
            if task.state == "outputs_ready":
                if (
                    [item.model_dump(mode="json") for item in task.outputs] != outputs
                    or task.usage != usage
                    or task.vendor_request_id != vendor_request_id
                ):
                    raise ValueError("archived media output conflict")
                return task
            if task.state not in {"submitting", "submission_unknown"}:
                raise ValueError("invalid media archive transition")
            updated = self._write(
                cursor,
                job,
                worker_id,
                {"state": "outputs_ready", "outputs": outputs, "usage": usage, "vendor_request_id": vendor_request_id},
            )
            self._record_usage(cursor, updated)
            return updated

    @staticmethod
    def _validate_outputs(job, modality, outputs):
        for item in outputs:
            segments = item["object_key"].split("/")
            if len(segments) != 9 or segments[0] != "workspaces" or segments[2] != "projects":
                raise ValueError("media artifact must use a versioned project asset key")
            if segments[4] != "assets" or segments[6] != "versions":
                raise ValueError("media artifact must use a versioned project asset key")
            expected = object_key(
                job.command.workspace_id,
                job.command.project_id,
                "assets",
                segments[5],
                segments[8],
                version_id=segments[7],
            )
            if expected != item["object_key"]:
                raise ValueError("media artifact belongs to another workspace or project")
            if item["content_type"] not in _MEDIA_TYPES[modality]:
                raise ValueError("media artifact content type does not match modality")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", item["bucket"]):
                raise ValueError("invalid media artifact bucket")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{0,200}", item["etag"]):
                raise ValueError("invalid media artifact etag")

    @staticmethod
    def _record_usage(cursor, task):
        numeric = task.usage

        def tokens(*keys):
            return next((numeric[k] for k in keys if type(numeric.get(k)) is int), 0)

        purpose = {
            "image": "image_generation",
            "audio": "speech_synthesis",
            "music": "music_generation",
            "effect": "sound_effect_generation",
        }[task.modality]
        identity = uuid5(NAMESPACE_URL, f"generated-media:{task.workspace_id}:{task.job_id}")
        metadata = {
            "purpose": purpose,
            "modality": task.modality,
            "raw_usage": numeric,
            "profile_version_id": task.route_identity.get("profile_version_id", ""),
            "vendor_request_id": task.vendor_request_id,
            "fencing_token": task.fencing_token,
        }
        cursor.execute(
            """INSERT INTO model_calls(id,job_id,provider,model,prompt_hash,input_tokens,output_tokens,status,metadata)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'completed',%s) ON CONFLICT(id) DO NOTHING""",
            (
                identity,
                task.job_id,
                task.route_identity["provider"],
                task.route_identity["model_name"],
                hashlib.sha256(task.request.prompt.encode()).hexdigest(),
                tokens("input_tokens", "prompt_tokens"),
                tokens("output_tokens", "completion_tokens"),
                _json(metadata),
            ),
        )

    def _write(self, cursor, job, worker_id, changes):
        if not changes.keys() <= {"state", "review", "outputs", "usage", "error", "vendor_request_id"}:
            raise ValueError("invalid generated media mutation")
        assignments = ",".join(f"{k}=%s" for k in changes)
        values = [_json(v) if k in {"review", "outputs", "usage", "error"} else v for k, v in changes.items()]
        cursor.execute(
            f"""UPDATE generated_media_tasks SET {assignments},fencing_token=%s,updated_at=now()
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
            raise FencingTokenError("worker lease expired during media commit")
        return self._required(cursor, job)
