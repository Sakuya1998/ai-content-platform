"""Typed, versioned creation rules; executable checks are registered in code."""

import hashlib
import json
import math
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CanonPolicy(PolicyModel):
    entity_types: list[str] = Field(default_factory=lambda: ["entity", "content_unit"], min_length=1)
    fact_types: list[str] = Field(default_factory=lambda: ["event", "state", "other"], min_length=1)
    require_evidence: bool = True
    max_audit_entries: int = Field(default=200, ge=1, le=10000)

    @model_validator(mode="after")
    def unique_types(self):
        for values in [self.entity_types, self.fact_types]:
            if len(values) != len(set(values)) or any(not value.strip() or len(value) > 80 for value in values):
                raise ValueError("policy types must be non-empty and unique")
        return self


class MemoryPolicy(PolicyModel):
    group_size: int = Field(default=5, ge=1, le=100)
    leaf_summary_chars: int = Field(default=500, ge=1, le=20000)
    group_summary_chars: int = Field(default=1800, ge=1, le=100000)
    document_summary_chars: int = Field(default=6000, ge=1, le=200000)
    neighbor_window: int = Field(default=5, ge=0, le=100)


def check_schema(schema):
    def references(value):
        if isinstance(value, dict):
            if "$id" in value:
                raise ValueError("schema identifiers and remote references are unsupported")
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise ValueError("remote schema references are unsupported")
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)

    references(schema)
    Draft202012Validator.check_schema(schema)
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError("constraint schema must be a closed object")


class ConstraintPolicy(PolicyModel):
    schema_: dict[str, Any] = Field(alias="schema")
    defaults: dict[str, Any]
    labels: dict[str, str] = Field(default_factory=dict)
    max_prompt_chars: int = Field(default=3500, ge=100, le=50000)

    @model_validator(mode="after")
    def valid_schema(self):
        check_schema(self.schema_)
        if list(Draft202012Validator(self.schema_).iter_errors(self.defaults)):
            raise ValueError("constraint defaults failed schema validation")
        if set(self.labels) - set(self.schema_.get("properties", {})):
            raise ValueError("constraint labels must reference defined properties")
        if any(not value.strip() for value in self.labels.values()):
            raise ValueError("constraint labels must not be empty")
        return self


PARAMETERS = {
    "text.length@1": {"scale": (0, 1000, False)},
    "text.structure@1": {"min_paragraphs": (1, 100, True), "min_chars": (1, 100000, True)},
    "text.repetition@1": {
        "duplicate_penalty": (0, 100, False),
        "window_size": (2, 100, True),
        "window_penalty": (0, 1000, False),
        "max_window_penalty": (0, 100, False),
    },
    "text.consistency@1": {name: (0, 100, False) for name in ["high", "medium", "low"]},
    "novel.scenes@1": {"duplicate_penalty": (0, 100, False)},
    "novel.narrative@1": {"invalid_penalty": (0, 100, False), "duplicate_penalty": (0, 100, False)},
}


class EvaluationRule(PolicyModel):
    key: str
    dimension: str = Field(min_length=1, max_length=80)
    weight: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    critical: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def registered_check(self):
        if not self.dimension.strip():
            raise ValueError("evaluation dimension must not be empty")
        if self.key not in PARAMETERS:
            raise ValueError("unregistered evaluation check")
        for name, value in self.parameters.items():
            bounds = PARAMETERS[self.key].get(name)
            if (
                bounds is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("invalid check parameters")
            minimum, maximum, integer = bounds
            if not minimum <= value <= maximum or (integer and not isinstance(value, int)):
                raise ValueError("check parameter outside registered bounds")
        return self


class EvaluationPolicy(PolicyModel):
    engine_version: Literal["checks@1"] = "checks@1"
    gate_threshold: float = Field(default=70, ge=0, le=100, allow_inf_nan=False)
    critical_threshold: float = Field(default=40, ge=0, le=100, allow_inf_nan=False)
    rules: list[EvaluationRule] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_dimensions(self):
        if len({rule.dimension for rule in self.rules}) != len(self.rules):
            raise ValueError("duplicate evaluation dimensions")
        return self


class CreationPolicy(PolicyModel):
    schema_version: Literal["creation-policy@1"] = "creation-policy@1"
    canon: CanonPolicy
    memory: MemoryPolicy
    constraints: ConstraintPolicy
    evaluation: EvaluationPolicy


def policy_hash(payload):
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CreationPolicyVersion(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    version_id: str
    key: str
    status: Literal["published"]
    content_hash: str
    payload: CreationPolicy

    @model_validator(mode="before")
    @classmethod
    def verify_hash(cls, value):
        if isinstance(value, dict):
            payload = value["payload"]
            for key, fields in {
                "canon": {"entity_types", "fact_types", "require_evidence", "max_audit_entries"},
                "memory": {
                    "group_size",
                    "leaf_summary_chars",
                    "group_summary_chars",
                    "document_summary_chars",
                    "neighbor_window",
                },
                "constraints": {"schema", "defaults", "labels", "max_prompt_chars"},
                "evaluation": {"engine_version", "gate_threshold", "critical_threshold", "rules"},
            }.items():
                if not fields.issubset(payload.get(key, {})):
                    raise ValueError("incomplete published creation policy")
            if set(payload) != {"schema_version", "canon", "memory", "constraints", "evaluation"}:
                raise ValueError("invalid creation policy fields")
            if any(
                set(rule) != {"key", "dimension", "weight", "critical", "parameters"}
                for rule in payload["evaluation"]["rules"]
            ):
                raise ValueError("incomplete evaluation rule")
            if value.get("content_hash") != policy_hash(payload):
                raise ValueError("creation policy content hash conflict")
        return value
