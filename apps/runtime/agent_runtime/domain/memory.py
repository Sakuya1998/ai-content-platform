"""Project-scoped structured memory with explicit knowledge-pack sharing."""

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class MemoryNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=lambda: f"memory_{uuid4().hex[:16]}")
    project_id: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=200)
    memory_type: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=200)
    source_version: int = Field(ge=1)
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SharedKnowledgePack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=lambda: f"knowledge_{uuid4().hex[:16]}")
    workspace_id: str = Field(min_length=1, max_length=200)
    source_project_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=160)
    node_ids: tuple[str, ...] = ()


class ProjectMemoryStore:
    """Reference implementation of memory isolation and explicit sharing."""

    def __init__(self) -> None:
        self._nodes: dict[str, MemoryNode] = {}
        self._packs: dict[str, SharedKnowledgePack] = {}
        self._shares: dict[tuple[str, str], str] = {}

    def commit(self, node: MemoryNode) -> MemoryNode:
        self._nodes[node.id] = node
        return node

    def list_for_project(self, project_id: str, workspace_id: str) -> list[MemoryNode]:
        nodes = [
            node for node in self._nodes.values() if node.project_id == project_id and node.workspace_id == workspace_id
        ]
        pack_ids = [
            pack_id for (target, ws), pack_id in self._shares.items() if target == project_id and ws == workspace_id
        ]
        for pack_id in pack_ids:
            pack = self._packs[pack_id]
            nodes.extend(self._nodes[node_id] for node_id in pack.node_ids if node_id in self._nodes)
        return sorted(nodes, key=lambda node: (node.source_version, node.id))

    def publish_knowledge_pack(self, project_id: str, workspace_id: str, name: str) -> SharedKnowledgePack:
        node_ids = tuple(
            node.id
            for node in self._nodes.values()
            if node.project_id == project_id and node.workspace_id == workspace_id
        )
        pack = SharedKnowledgePack(
            workspace_id=workspace_id,
            source_project_id=project_id,
            name=name,
            node_ids=node_ids,
        )
        self._packs[pack.id] = pack
        return pack

    def share_pack(self, pack_id: str, target_project_id: str, workspace_id: str) -> None:
        try:
            pack = self._packs[pack_id]
        except KeyError as exc:
            raise ValueError("knowledge pack not found") from exc
        if pack.workspace_id != workspace_id:
            raise ValueError("knowledge pack belongs to another workspace")
        self._shares[(target_project_id, workspace_id)] = pack_id
