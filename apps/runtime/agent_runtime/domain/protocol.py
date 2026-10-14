"""Runtime command/event contracts shared by the control plane and workers."""

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ExecutionSnapshotReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object_key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1)


class RuntimeCommand(BaseModel):
    """A durable command accepted by Runtime before RocketMQ acknowledgement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    occurred_at: AwareDatetime | None = None
    causation_id: str | None = None
    command_id: str = Field(min_length=1, max_length=200)
    command_type: str = Field(min_length=1, max_length=120)
    workspace_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=200)
    workflow_run_id: str = Field(min_length=1, max_length=200)
    capability_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_snapshot: ExecutionSnapshotReference | None = None
    trace_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_execution_snapshot_scope(self) -> RuntimeCommand:
        if self.execution_snapshot is not None:
            prefix = f"workspaces/{self.workspace_id}/projects/{self.project_id}/"
            parts = self.execution_snapshot.object_key.split("/")
            if not self.execution_snapshot.object_key.startswith(prefix) or any(
                part in {"", ".", ".."} for part in parts
            ):
                raise ValueError("execution snapshot object key must match command scope")
        return self


class RuntimeEvent(BaseModel):
    """An ordered Runtime event that can be projected idempotently by Go."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    occurred_at: AwareDatetime | None = None
    causation_id: str | None = None
    event_id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=120)
    workspace_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=200)
    workflow_run_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
