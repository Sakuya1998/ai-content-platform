"""Evidence-based project Canon contract, shared by all creation workflows."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.domain.policies import CanonPolicy


class CanonModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(CanonModel):
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    block_id: str | None = None


class CanonEntity(CanonModel):
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class CanonFact(CanonModel):
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    value: Any
    status: Literal["planned", "draft", "confirmed", "deprecated"] = "confirmed"
    evidence: list[Evidence] = Field(default_factory=list)
    source_id: str = ""
    source_version: str = ""


class CanonRelation(CanonModel):
    from_entity_id: str
    to_entity_id: str
    kind: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class CanonState(CanonModel):
    schema_version: Literal["canon@1"] = "canon@1"
    workspace_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    entities: list[CanonEntity] = Field(default_factory=list)
    relations: list[CanonRelation] = Field(default_factory=list)
    facts: list[CanonFact] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)


def validate_canon(state: CanonState, policy: CanonPolicy):
    ids = {entity.id for entity in state.entities}
    if len(ids) != len(state.entities) or len({fact.id for fact in state.facts}) != len(state.facts):
        raise ValueError("duplicate Canon identity")
    for entity in state.entities:
        if entity.kind not in policy.entity_types:
            raise ValueError("Canon entity type conflicts with policy")
    for relation in state.relations:
        if relation.from_entity_id not in ids or relation.to_entity_id not in ids:
            raise ValueError("Canon relation must reference existing entities")
    for fact in state.facts:
        if fact.kind not in policy.fact_types:
            raise ValueError("Canon fact type conflicts with policy")
        if policy.require_evidence and fact.status == "confirmed" and not fact.evidence:
            raise ValueError("confirmed Canon facts require evidence")
    return state


def commit_facts(
    state: CanonState, facts: list[CanonFact], policy: CanonPolicy, *, source_id: str, source_version: str
):
    prepared = [fact.model_copy(update={"source_id": source_id, "source_version": source_version}) for fact in facts]
    retained = {fact.id: fact for fact in state.facts if fact.source_id != source_id}
    retained.update({fact.id: fact for fact in prepared})
    updated = state.model_copy(update={"facts": list(retained.values()), "version": state.version + 1})
    return validate_canon(updated, policy)
