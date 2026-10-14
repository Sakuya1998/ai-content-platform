"""Novel field adapter over content-neutral workflow persistence."""

from uuid import uuid4

from agent_runtime.infrastructure.postgres.jobs import _json
from agent_runtime.infrastructure.postgres.workflow import WorkflowStore, digest

RECORD_CATEGORIES = {
    "chapters": "document",
    "chapter_versions": "document",
    "planning_versions": "artifact",
    "candidates": "artifact",
    "evaluations": "evaluation",
    "benchmarks": "evaluation",
    "book_audits": "audit",
    "memory_snapshots": "memory_report",
    "memory_quality": "memory_report",
}


class PostgresNovelStore(WorkflowStore):
    def __init__(self, execution):
        super().__init__(execution, workflow_id="novel.write@1")

    def initialize(self, state):
        self.check_scope(state["novel_id"])
        super().initialize(state)

    def save_state(self, state):
        self.check_scope(state["novel_id"])
        with self.transaction(write=True) as cursor:
            if self.execution.creation_policy and state.get("canon"):
                from agent_runtime.domain.policies import policy_hash
                from agent_runtime.workflows.novel.memory.canonical_adapter import canonical_projection

                canon = canonical_projection(
                    state["canon"],
                    workspace_id=self.workspace_id,
                    project_id=self.project_id,
                    source_version=digest(state["canon"]),
                    policy=self.execution.creation_policy.payload.canon,
                )
                payload = canon.model_dump(mode="json")
                cursor.execute(
                    """INSERT INTO canonical_snapshots(workspace_id,project_id,content_hash,payload,
                    policy_version_id,source_job_id,fencing_token) VALUES(%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING""",
                    (
                        self.workspace_id,
                        self.project_id,
                        policy_hash(payload),
                        _json(payload),
                        self.execution.creation_policy.version_id,
                        self.execution.job.command.job_id,
                        self.execution.job.fencing_token,
                    ),
                )
            self._save_state(cursor, state)

    def save_progress(self, novel_id, current_chapter, current_phase, state=None):
        self.check_scope(novel_id)
        super().save_progress({**(state or {}), "current_chapter": current_chapter, "current_phase": current_phase})

    def record(self, kind, payload, *, chapter_number=0, record_id=None, deduplicate=True):
        return super().record(
            kind,
            {
                **payload,
                "chapter_number": chapter_number,
                "novel_id": self.project_id,
                "content_hash": payload.get("content_hash", digest(payload)),
            },
            category=RECORD_CATEGORIES.get(kind, "execution_event"),
            scope_key=str(chapter_number),
            record_id=record_id,
            deduplicate=deduplicate,
            content_hash=digest(payload),
        )

    def import_envelope(self, envelope):
        allowed = (
            "chapter_versions",
            "planning_versions",
            "candidates",
            "evaluations",
            "book_audits",
            "memory_snapshots",
            "memory_quality",
            "benchmarks",
        )
        with self.transaction(write=True) as cursor:
            for kind in allowed:
                for index, payload in enumerate(envelope.get(kind) or [], start=1):
                    chapter = int(payload.get("chapter_number", 0) or 0)
                    version = int(payload.get("version_number", index) or index)
                    identity = str(payload.get("id") or uuid4())
                    payload = {
                        **payload,
                        "id": identity,
                        "novel_id": self.project_id,
                        "chapter_number": chapter,
                        "version_number": version,
                    }
                    cursor.execute(
                        """INSERT INTO workflow_records(workspace_id,project_id,workflow_id,category,record_kind,
                           record_id,scope_key,version_number,content_hash,payload,source_job_id,fencing_token)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (
                            self.workspace_id,
                            self.project_id,
                            self.workflow_id,
                            RECORD_CATEGORIES.get(kind, "execution_event"),
                            kind,
                            identity,
                            str(chapter),
                            version,
                            digest(payload),
                            _json(payload),
                            self.execution.job.command.job_id,
                            self.execution.job.fencing_token,
                        ),
                    )

    def records(self, kind, chapter_number=None):
        result = super().records(kind, str(chapter_number) if chapter_number is not None else None)
        return sorted(result, key=lambda item: (item.get("chapter_number", 0), item.get("version_number", 0)))

    def save_chapter(self, novel_id, chapter_number, **payload):
        self.check_scope(novel_id)
        return self.record(
            "chapters",
            {**payload, **(payload.get("digest") or {})},
            chapter_number=chapter_number,
            record_id=str(chapter_number),
        )

    def save_chapter_version(self, novel_id, chapter_number, **payload):
        self.check_scope(novel_id)
        payload["word_count"] = len(payload.get("content", ""))
        return self.record("chapter_versions", payload, chapter_number=chapter_number)

    def get_chapter_version(self, novel_id, chapter_number, version_number):
        self.check_scope(novel_id)
        return next(
            (v for v in self.records("chapter_versions", chapter_number) if v["version_number"] == version_number), None
        )

    def save_planning_version(self, novel_id, artifact_type, chapter_number, **payload):
        self.check_scope(novel_id)
        return self.record(
            "planning_versions", {"artifact_type": artifact_type, **payload}, chapter_number=chapter_number
        )

    def save_chapter_candidate(self, novel_id, chapter_number, **payload):
        self.check_scope(novel_id)
        return self.record("candidates", {"status": "generated", **payload}, chapter_number=chapter_number)

    def get_chapter_candidate(self, novel_id, candidate_id):
        self.check_scope(novel_id)
        return next((v for v in self.records("candidates") if v["id"] == candidate_id), None)

    def mark_chapter_candidate_selected(self, novel_id, chapter_number, candidate_id):
        self.check_scope(novel_id)
        with self.transaction(write=True) as cursor:
            cursor.execute(
                "SELECT record_id FROM workflow_records WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s "
                "AND record_kind='candidates' AND scope_key=%s AND record_id=%s",
                (*self.scope, str(chapter_number), candidate_id),
            )
            if cursor.fetchone() is None:
                raise ValueError("chapter candidate does not exist")

    def get_planning_version(self, novel_id, artifact_type, chapter_number, version_number):
        self.check_scope(novel_id)
        return next(
            (
                item
                for item in self.records("planning_versions", chapter_number)
                if item["artifact_type"] == artifact_type and item["version_number"] == version_number
            ),
            None,
        )

    def save_book_audit(self, novel_id, **payload):
        self.check_scope(novel_id)
        return self.record("book_audits", payload)

    def save_memory_snapshot(self, novel_id, **payload):
        self.check_scope(novel_id)
        return self.record("memory_snapshots", payload)

    def save_chapter_evaluation(self, novel_id, chapter_number, **payload):
        self.check_scope(novel_id)
        return self.record("evaluations", payload, chapter_number=chapter_number)

    def envelope(self, state=None):
        result = {
            "state": state or self.state() or {},
            **{
                kind: self.records(kind)
                for kind in (
                    "chapter_versions",
                    "planning_versions",
                    "candidates",
                    "evaluations",
                    "book_audits",
                    "memory_snapshots",
                    "memory_quality",
                    "benchmarks",
                )
            },
        }
        with self.transaction() as cursor:
            cursor.execute(
                """SELECT m.id,m.job_id,m.provider,m.model,m.prompt_hash,m.input_tokens,m.output_tokens,
                   m.cost,m.status,m.metadata,m.created_at FROM model_calls m JOIN runtime_jobs j ON j.id=m.job_id
                   WHERE j.workspace_id=%s AND j.project_id=%s ORDER BY m.created_at,m.id""",
                (self.workspace_id, self.project_id),
            )
            traces = []
            for (
                identity,
                job,
                provider,
                model,
                prompt_hash,
                inputs,
                outputs,
                cost,
                status,
                metadata,
                created,
            ) in cursor.fetchall():
                safe_metadata = {
                    key: value
                    for key, value in metadata.items()
                    if key
                    in {
                        "agent",
                        "purpose",
                        "elapsed_ms",
                        "fencing_token",
                        "attempt",
                        "fallback_used",
                        "error_type",
                        "prompt_version_id",
                        "prompt_pack_version_id",
                        "prompt_node",
                        "renderer_version",
                        "variables_hash",
                    }
                }
                traces.append(
                    {
                        "id": str(identity),
                        "job_id": job,
                        "provider": provider,
                        "model": model,
                        "prompt_hash": prompt_hash,
                        "input_tokens": inputs,
                        "output_tokens": outputs,
                        "total_tokens": inputs + outputs,
                        "cost": float(cost),
                        "status": status,
                        "metadata": safe_metadata,
                        "created_at": created.isoformat(),
                    }
                )
            result["traces"] = traces
            result["usage"] = [
                {key: value for key, value in trace.items() if key not in {"prompt_hash", "metadata"}}
                for trace in traces
            ]
        return result

    def publish_state(self, reference, state_hash):
        with self.transaction(write=True) as cursor:
            self.execution.store._event(
                cursor,
                self.execution.job.command,
                "project.state.changed",
                {"artifact": reference, "state_hash": state_hash, "project_id": self.project_id},
            )


class NovelQueryAdapter:
    """Read-only scoped exporter used by migration and integrity tooling."""

    def __init__(self, runtime_store, workspace_id, project_id):
        self.runtime_store, self.workspace_id, self.project_id = runtime_store, workspace_id, project_id

    def envelope(self):
        with self.runtime_store.scope(self.workspace_id) as cursor:
            cursor.execute(
                "SELECT state FROM workflow_states WHERE workspace_id=%s AND project_id=%s "
                "AND workflow_id='novel.write@1'",
                (self.workspace_id, self.project_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError("novel project not found")
            result = {"state": row[0]}
            cursor.execute(
                "SELECT record_kind,payload FROM workflow_records WHERE workspace_id=%s AND project_id=%s "
                "AND workflow_id='novel.write@1' ORDER BY record_kind,scope_key,version_number",
                (self.workspace_id, self.project_id),
            )
            for kind, payload in cursor.fetchall():
                result.setdefault(kind, []).append(payload)
            return result
