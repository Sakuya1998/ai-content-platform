"""Immutable database prompt contract and expression-free typed rendering."""

import hashlib
import json
import re
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def prompt_hash(payload):
    keys = ["messages", "variables_schema", "output_schema", "renderer_version"]
    value = {key: payload[key] for key in keys}
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_schema(schema):
    def check_refs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise ValueError("external schema references are unsupported")
                check_refs(item)
        elif isinstance(value, list):
            for item in value:
                check_refs(item)

    check_refs(schema)
    Draft202012Validator.check_schema(schema)


class PromptMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=200000)


class PromptVersion(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    version_id: str
    key: str
    node_key: str
    messages: tuple[PromptMessage, ...] = Field(min_length=1, max_length=20)
    variables_schema: dict[str, Any]
    output_schema: dict[str, Any]
    renderer_version: Literal["literal-v1"]
    status: Literal["published"]
    content_hash: str

    @model_validator(mode="after")
    def validate_contract(self):
        validate_schema(self.variables_schema)
        validate_schema(self.output_schema)
        if (
            self.variables_schema.get("type") != "object"
            or self.variables_schema.get("additionalProperties") is not False
        ):
            raise ValueError("prompt variables must be a closed object schema")
        properties = set(self.variables_schema.get("properties", {}))
        required = set(self.variables_schema.get("required", []))
        variables = {name for message in self.messages for name in PLACEHOLDER.findall(message.content)}
        if variables != properties or not required.issubset(properties):
            raise ValueError("prompt template variables conflict")
        if self.content_hash != prompt_hash(self.model_dump(mode="json")):
            raise ValueError("prompt content hash conflict")
        return self


def render_prompt(version: PromptVersion, variables: dict):
    errors = list(Draft202012Validator(version.variables_schema).iter_errors(variables))
    if errors:
        raise ValueError("prompt variables failed schema validation")

    def replacement(match):
        value = variables.get(match.group(1), "")
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if isinstance(value, bool):
            return "true" if value else "false"
        return "" if value is None else str(value)

    return [
        {"role": message.role, "content": PLACEHOLDER.sub(replacement, message.content)} for message in version.messages
    ]


def render_node(node_key, variables):
    from agent_runtime.execution.context import current_execution

    context = current_execution()
    version = context.prompt_versions.get(node_key)
    if version is None:
        raise ValueError("missing frozen database prompt binding for " + node_key)
    result = render_prompt(version, variables)
    context.prompt_renderings[hashlib.sha256(str(result).encode()).hexdigest()] = {
        "prompt_version_id": version.version_id,
        "prompt_node": node_key,
        "renderer_version": version.renderer_version,
        "prompt_pack_version_id": context.prompt_pack_versions.get(node_key, ""),
        "output_schema": json.dumps(version.output_schema, sort_keys=True, separators=(",", ":")),
        "variables_hash": hashlib.sha256(
            json.dumps(variables, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    return result


def rendering_metadata(prompt):
    from agent_runtime.execution.context import current_execution

    context = current_execution(required=False)
    return context.prompt_renderings.get(hashlib.sha256(str(prompt).encode()).hexdigest(), {}) if context else {}


def inherit_rendering(original, corrected):
    from agent_runtime.execution.context import current_execution

    metadata = rendering_metadata(original)
    if metadata:
        current_execution().prompt_renderings[hashlib.sha256(str(corrected).encode()).hexdigest()] = metadata


def validate_rendered_output(prompt, value):
    from agent_runtime.execution.context import current_execution

    metadata = rendering_metadata(prompt)
    if not metadata:
        return
    schema = current_execution().prompt_versions[metadata["prompt_node"]].output_schema
    if schema.get("type") == "object" and isinstance(value, list) and len(value) == 1:
        value = value[0]
    if list(Draft202012Validator(schema).iter_errors(value)):
        raise ValueError("prompt output failed frozen schema validation")
