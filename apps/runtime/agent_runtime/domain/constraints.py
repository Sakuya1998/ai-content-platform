"""Shared type-safe constraint validation and rendering."""

import json
from copy import deepcopy

from jsonschema import Draft202012Validator

from agent_runtime.domain.policies import ConstraintPolicy


def normalize_constraints(values: dict, policy: ConstraintPolicy) -> dict:
    merged = {**deepcopy(policy.defaults), **values}
    if list(Draft202012Validator(policy.schema_).iter_errors(merged)):
        raise ValueError("creation constraints failed schema validation")
    return merged


def format_constraints(values: dict, policy: ConstraintPolicy) -> str:
    normalized = normalize_constraints(values, policy)
    lines = [
        f"{policy.labels.get(key, key)}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for key, value in normalized.items()
    ]
    text = "\n".join(lines)
    if len(text) > policy.max_prompt_chars:
        raise ValueError("constraints exceed frozen prompt budget")
    return text
