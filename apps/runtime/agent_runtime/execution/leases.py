"""Runtime job lease, fencing and event sequencing reference implementation."""

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent


class LeaseLostError(RuntimeError):
    """The worker no longer owns the job lease."""


class FencingTokenError(LeaseLostError):
    """A stale worker attempted to mutate a newer execution attempt."""


@dataclass
class RuntimeJob:
    command: RuntimeCommand
    status: str = "accepted"
    worker_id: str = ""
    fencing_token: int = 0
    lease_expires_at: datetime | None = None
    result: dict = field(default_factory=dict)
    error: dict = field(default_factory=dict)
    next_sequence: int = 1
    attempt: int = 0
    resume_input: dict = field(default_factory=dict)


class InMemoryRuntimeJobStore:
    """Reference implementation matching the Runtime PostgreSQL invariants."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._jobs: dict[str, RuntimeJob] = {}
        self._command_ids: dict[str, str] = {}
        self.artifacts: dict[str, dict] = {}
        self._artifact_events: dict[tuple[str, str], RuntimeEvent] = {}
        self._lock = RLock()

    def accept(self, command: RuntimeCommand) -> RuntimeJob:
        with self._lock:
            existing_job_id = self._command_ids.get(command.command_id)
            if existing_job_id is not None:
                existing = self._jobs[existing_job_id]
                if existing.command.model_dump(mode="json") != command.model_dump(mode="json"):
                    raise ValueError("command_id payload conflict")
                return replace(existing)
            existing_job = self._jobs.get(command.job_id)
            if existing_job is not None and existing_job.command.command_id != command.command_id:
                raise ValueError("job_id is already bound to another command")
            job = RuntimeJob(command=command)
            self._jobs[command.job_id] = job
            self._command_ids[command.command_id] = command.job_id
            return replace(job)

    def get(self, job_id: str) -> RuntimeJob:
        try:
            return replace(self._jobs[job_id])
        except KeyError as exc:
            raise KeyError(f"runtime job not found: {job_id}") from exc

    def get_scoped(self, job_id: str, workspace_id: str) -> RuntimeJob:
        job = self.get(job_id)
        if job.command.workspace_id != workspace_id:
            raise KeyError("runtime job not found")
        return job

    def record_artifact(self, job_id, workspace_id, worker_id, fencing_token, reference) -> RuntimeEvent:
        with self._lock:
            self.get_scoped(job_id, workspace_id)
            self._owned(job_id, worker_id, fencing_token)
            event_key = (job_id, reference.object_key)
            if event_key in self._artifact_events:
                event = self._artifact_events[event_key]
                if event.payload.get("sha256") != reference.sha256:
                    raise ValueError("artifact checksum conflict")
                return event
            payload = asdict(reference)
            event = self.next_event(job_id, "artifact.created", payload)
            self.artifacts[reference.object_key] = payload
            self._artifact_events[event_key] = event
            return event

    def claim(self, job_id: str, worker_id: str, *, lease_seconds: int) -> RuntimeJob:
        with self._lock:
            job = self._get(job_id)
            now = self._clock()
            active = job.status == "running" and job.lease_expires_at and job.lease_expires_at > now
            if active and job.worker_id != worker_id:
                raise LeaseLostError("job is owned by another worker")
            if job.status not in {"accepted", "running"}:
                raise LeaseLostError(f"job cannot be claimed from {job.status}")
            job.status = "running"
            job.worker_id = worker_id
            job.fencing_token += 1
            job.lease_expires_at = now + timedelta(seconds=max(lease_seconds, 1))
            return replace(job)

    def heartbeat(self, job_id: str, worker_id: str, fencing_token: int, *, lease_seconds: int) -> None:
        with self._lock:
            job = self._owned(job_id, worker_id, fencing_token)
            job.lease_expires_at = self._clock() + timedelta(seconds=max(lease_seconds, 1))

    def complete(self, job_id: str, worker_id: str, fencing_token: int, result: dict) -> None:
        with self._lock:
            job = self._owned(job_id, worker_id, fencing_token)
            job.status = "completed"
            job.result = dict(result)
            job.lease_expires_at = None

    def fail(self, job_id: str, worker_id: str, fencing_token: int, error: dict) -> None:
        with self._lock:
            job = self._owned(job_id, worker_id, fencing_token)
            job.status = "failed"
            job.error = dict(error)
            job.lease_expires_at = None

    def expire(self, job_id: str) -> None:
        with self._lock:
            job = self._get(job_id)
            job.lease_expires_at = self._clock() - timedelta(seconds=1)

    def next_event(self, job_id: str, event_type: str, payload: dict) -> RuntimeEvent:
        with self._lock:
            job = self._get(job_id)
            event = RuntimeEvent(
                event_id=f"event_{uuid4().hex[:16]}",
                event_type=event_type,
                workspace_id=job.command.workspace_id,
                project_id=job.command.project_id,
                job_id=job.command.job_id,
                workflow_run_id=job.command.workflow_run_id,
                sequence=job.next_sequence,
                operation_id=f"op_{uuid4().hex[:16]}",
                trace_id=job.command.trace_id,
                payload=payload,
            )
            job.next_sequence += 1
            return event

    def _owned(self, job_id: str, worker_id: str, fencing_token: int) -> RuntimeJob:
        job = self._get(job_id)
        if (
            job.status != "running"
            or job.worker_id != worker_id
            or job.fencing_token != fencing_token
            or job.lease_expires_at is None
            or job.lease_expires_at <= self._clock()
        ):
            raise FencingTokenError("worker lease is no longer valid")
        return job

    def _get(self, job_id: str) -> RuntimeJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"runtime job not found: {job_id}") from exc


class RuntimeWorker:
    def __init__(self, store: InMemoryRuntimeJobStore, emit: Callable[[RuntimeEvent], None]) -> None:
        self.store = store
        self.emit = emit

    async def run(
        self,
        job_id: str,
        worker_id: str,
        handler: Callable[[RuntimeCommand], Awaitable[dict]],
        *,
        lease_seconds: int = 60,
    ) -> dict:
        job = self.store.claim(job_id, worker_id, lease_seconds=lease_seconds)
        self.emit(self.store.next_event(job_id, "job.running", {"fencing_token": job.fencing_token}))
        try:
            result = await handler(job.command)
            self.store.complete(job_id, worker_id, job.fencing_token, result)
        except FencingTokenError as exc:
            self.emit(self.store.next_event(job_id, "job.lease_lost", {"error": str(exc)}))
            raise LeaseLostError(str(exc)) from exc
        except Exception as exc:
            try:
                self.store.fail(job_id, worker_id, job.fencing_token, {"type": type(exc).__name__})
            except FencingTokenError:
                self.emit(self.store.next_event(job_id, "job.lease_lost", {"error": "lease lost"}))
                raise LeaseLostError("worker lease is no longer valid") from exc
            self.emit(self.store.next_event(job_id, "job.failed", {"error": type(exc).__name__}))
            raise
        self.emit(self.store.next_event(job_id, "job.completed", result))
        return result
