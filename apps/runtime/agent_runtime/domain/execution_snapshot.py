"""Immutable task input transferred from Control to Runtime through object storage."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_runtime.domain.capabilities import CapabilitySnapshot
from agent_runtime.domain.protocol import RuntimeCommand


class ExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    workspace_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=200)
    snapshot: CapabilitySnapshot
    request: dict[str, Any]
    source: dict[str, Any] | None
    inputs: list[dict[str, Any]]
    resolved_resources: dict[str, Any]
    committed_version: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_request_scope(self) -> ExecutionSnapshot:
        request_project = self.request.get("project_id")
        if request_project is not None and request_project != self.project_id:
            raise ValueError("execution snapshot request project does not match snapshot scope")
        return self

    def verify_command(self, command: RuntimeCommand) -> None:
        if (
            command.workspace_id != self.workspace_id
            or command.project_id != self.project_id
            or command.job_id != self.job_id
        ):
            raise ValueError("execution snapshot identity conflict")
        if command.execution_snapshot is None:
            raise ValueError("execution snapshot reference is required")
        if self.snapshot.snapshot_hash != command.capability_snapshot_hash:
            raise ValueError("capability snapshot checksum conflict")
