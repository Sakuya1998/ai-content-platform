"""Versioned Prompt, Skill, Tool and MCP policy governance.

The production adapter persists these records in Control PostgreSQL. This
catalog keeps the same publish/clone/snapshot invariants for unit tests and
offline Runtime planning.
"""

from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.domain.capabilities import CapabilitySnapshot


class CapabilityStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"


class CapabilityVisibility(StrEnum):
    PLATFORM = "platform"
    WORKSPACE = "workspace"


class CapabilityDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern=r"^(prompt_pack|skill|tool|mcp_policy)$")
    key: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1)
    workspace_id: str | None = None
    parent_id: str | None = None
    visibility: CapabilityVisibility
    status: CapabilityStatus = CapabilityStatus.DRAFT
    payload: dict[str, Any] = Field(default_factory=dict)


class CapabilityCatalog:
    def __init__(self) -> None:
        self._items: dict[str, CapabilityDefinition] = {}

    def register(
        self,
        *,
        kind: str,
        key: str,
        version: int,
        visibility: CapabilityVisibility,
        payload: dict[str, Any],
        workspace_id: str | None = None,
        parent_id: str | None = None,
    ) -> CapabilityDefinition:
        if visibility is CapabilityVisibility.WORKSPACE and not workspace_id:
            raise ValueError("workspace capability requires workspace_id")
        if visibility is CapabilityVisibility.PLATFORM and workspace_id:
            raise ValueError("platform capability cannot have workspace_id")
        item = CapabilityDefinition(
            id=f"{kind}_{uuid4().hex[:16]}",
            kind=kind,
            key=key,
            version=version,
            visibility=visibility,
            workspace_id=workspace_id,
            parent_id=parent_id,
            payload=payload,
        )
        self._items[item.id] = item
        return item

    def get(self, capability_id: str) -> CapabilityDefinition:
        try:
            return self._items[capability_id]
        except KeyError as exc:
            raise ValueError("capability not found") from exc

    def publish(self, capability_id: str) -> CapabilityDefinition:
        item = self.get(capability_id)
        if item.status is CapabilityStatus.DISABLED:
            raise ValueError("disabled capability cannot be published")
        published = item.model_copy(update={"status": CapabilityStatus.PUBLISHED})
        self._items[capability_id] = published
        return published

    def clone_for_workspace(self, capability_id: str, workspace_id: str) -> CapabilityDefinition:
        source = self.get(capability_id)
        if source.status is not CapabilityStatus.PUBLISHED:
            raise ValueError("only published capabilities can be cloned")
        clone = self.register(
            kind=source.kind,
            key=source.key,
            version=source.version,
            visibility=CapabilityVisibility.WORKSPACE,
            workspace_id=workspace_id,
            parent_id=source.id,
            payload=source.payload,
        )
        return clone

    def snapshot(
        self,
        *,
        workspace_id: str,
        workflow: str,
        capability_ids: list[str],
        schema_versions: dict[str, str],
        model_routes: dict[str, str] | None = None,
    ) -> CapabilitySnapshot:
        selected = [self.get(item_id) for item_id in capability_ids]
        for item in selected:
            visible = item.visibility is CapabilityVisibility.PLATFORM or item.workspace_id == workspace_id
            if not visible:
                raise ValueError("capability is not visible to workspace")
            if item.status is not CapabilityStatus.PUBLISHED:
                raise ValueError("only published capabilities can be included in a snapshot")
        return CapabilitySnapshot(
            workflow=workflow,
            skills=[item.key + "@" + str(item.version) for item in selected if item.kind == "skill"],
            prompt_packs=[item.key + "@" + str(item.version) for item in selected if item.kind == "prompt_pack"],
            tools=[item.key + "@" + str(item.version) for item in selected if item.kind == "tool"],
            mcp_policies=[item.key + "@" + str(item.version) for item in selected if item.kind == "mcp_policy"],
            model_routes=model_routes or {},
            schema_versions=schema_versions,
        )
