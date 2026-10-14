"""Immutable capability snapshots used to compile a runtime execution plan."""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_runtime.domain.workflow_contracts import WORKFLOWS


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    template: str = Field(min_length=1, max_length=200)
    nodes: list[str] = Field(min_length=1, max_length=100)


class FrozenWorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    definition: WorkflowDefinition

    @model_validator(mode="after")
    def verify_definition_hash(self) -> FrozenWorkflowDefinition:
        data = json.dumps(
            self.definition.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if hashlib.sha256(data.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("workflow definition checksum conflict")
        registered = WORKFLOWS.get(self.definition.template)
        if (
            registered is None
            or len(self.definition.nodes) != len(registered)
            or set(self.definition.nodes) != set(registered)
        ):
            raise ValueError("unregistered workflow node contract")
        return self


class CapabilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    workflow: str = Field(min_length=1, max_length=200)
    workflow_definition: FrozenWorkflowDefinition | None = None
    skills: list[str] = Field(default_factory=list)
    prompt_packs: list[str] = Field(default_factory=list)
    prompt_bindings: dict[str, str] | None = None
    creation_policy: dict[str, str] | None = None
    tools: list[str] = Field(default_factory=list)
    mcp_policies: list[str] = Field(default_factory=list)
    model_routes: dict[str, str] = Field(default_factory=dict)
    schema_versions: dict[str, str] = Field(min_length=1)
    snapshot_hash: str = ""

    @model_validator(mode="after")
    def set_snapshot_hash(self) -> CapabilitySnapshot:
        if self.workflow_definition is not None and self.workflow_definition.definition.template != self.workflow:
            raise ValueError("workflow definition template conflict")
        payload = self.model_dump(exclude={"snapshot_hash"}, mode="json")
        if self.prompt_bindings is None:
            payload.pop("prompt_bindings", None)
        if self.creation_policy is None:
            payload.pop("creation_policy", None)
        if self.workflow_definition is None:
            payload.pop("workflow_definition", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if self.snapshot_hash and self.snapshot_hash != expected:
            raise ValueError("capability snapshot checksum conflict")
        object.__setattr__(self, "snapshot_hash", expected)
        return self
