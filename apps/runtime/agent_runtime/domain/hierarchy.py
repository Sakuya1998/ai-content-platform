"""Content-unit hierarchy independent of chapters, scenes or content type."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.domain.policies import MemoryPolicy, policy_hash


class MemoryUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    position: int = Field(ge=0)
    title: str = ""
    summary: str = ""
    source_version: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


def build_hierarchy(units: list[MemoryUnit], policy: MemoryPolicy, *, render_unit=None, render_group=None):
    if len({unit.id for unit in units}) != len(units):
        raise ValueError("duplicate memory unit IDs")
    ordered = sorted(units, key=lambda unit: (unit.position, unit.id))
    leaves = [{**unit.model_dump(), "summary": unit.summary[: policy.leaf_summary_chars]} for unit in ordered]
    groups = []
    for offset in range(0, len(leaves), policy.group_size):
        children = leaves[offset : offset + policy.group_size]
        summary = " ".join(
            (render_unit(unit) if render_unit else unit["summary"]) for unit in children if unit["summary"]
        )
        groups.append(
            {
                "id": f"group-{len(groups) + 1}",
                "position": len(groups),
                "unit_ids": [unit["id"] for unit in children],
                "start_position": children[0]["position"],
                "end_position": children[-1]["position"],
                "summary": summary[: policy.group_summary_chars],
            }
        )
    document_summary = "\n".join(render_group(group) if render_group else group["summary"] for group in groups)
    result = {
        "schema_version": "memory-hierarchy@1",
        "units": leaves,
        "groups": groups,
        "document_summary": document_summary[: policy.document_summary_chars],
        "policy": policy.model_dump(),
    }
    return {**result, "content_hash": policy_hash(result)}
