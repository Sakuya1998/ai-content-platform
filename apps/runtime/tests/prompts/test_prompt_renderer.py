import hashlib
import json

import pytest

from agent_runtime.prompts.renderer import PromptVersion, render_prompt


def version(template='Write {title}. JSON example: {"text":"value"}'):
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "additionalProperties": False,
    }
    payload = {
        "messages": [{"role": "user", "content": template}],
        "variables_schema": schema,
        "output_schema": {"type": "object"},
        "renderer_version": "literal-v1",
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PromptVersion(
        version_id="version",
        key="content.writer",
        node_key="content.write",
        status="published",
        content_hash=digest,
        **payload,
    )


def test_literal_renderer_validates_types_and_does_not_execute_data():
    prompt = version()
    result = render_prompt(prompt, {"title": "{dangerous_expression} $(never executed)"})
    assert result == [
        {"role": "user", "content": 'Write {dangerous_expression} $(never executed). JSON example: {"text":"value"}'}
    ]
    with pytest.raises(ValueError):
        render_prompt(prompt, {"title": 123})
    with pytest.raises(ValueError):
        render_prompt(prompt, {})
    with pytest.raises(ValueError):
        render_prompt(prompt, {"title": "Title", "unknown": "extra"})


def test_prompt_hash_and_unsupported_renderer_fail_before_render():
    original = version().model_dump()
    original["messages"][0]["content"] = "tampered {title}"
    with pytest.raises(ValueError, match="hash"):
        PromptVersion.model_validate(original)
    original = version().model_dump()
    original["renderer_version"] = "jinja"
    with pytest.raises(ValueError):
        PromptVersion.model_validate(original)


def test_runtime_never_falls_back_to_local_file_for_missing_prompt():
    from agent_runtime.execution.context import _EXECUTION, ExecutionContext
    from agent_runtime.prompts.loader import fill_template
    from agent_runtime.settings import RuntimeSettings

    token = _EXECUTION.set(ExecutionContext(None, None, RuntimeSettings(_env_file=None)))
    try:
        with pytest.raises(ValueError, match="binding"):
            fill_template("world_builder", title="Title", genre="fiction", inspiration="story")
    finally:
        _EXECUTION.reset(token)


def test_runtime_preserves_database_message_roles_and_render_provenance():
    from agent_runtime.execution.context import _EXECUTION, ExecutionContext
    from agent_runtime.prompts.loader import fill_template
    from agent_runtime.prompts.renderer import prompt_hash, rendering_metadata
    from agent_runtime.settings import RuntimeSettings

    raw = version().model_dump(mode="json")
    raw["messages"].insert(0, {"role": "system", "content": "Keep the original tone."})
    raw["content_hash"] = prompt_hash(raw)
    prompt = PromptVersion.model_validate(raw)
    context = ExecutionContext(None, None, RuntimeSettings(_env_file=None), prompt_versions={"content.write": prompt})
    token = _EXECUTION.set(context)
    try:
        messages = fill_template("content.write", title="Private manuscript")
        assert [message["role"] for message in messages] == ["system", "user"]
        metadata = rendering_metadata(messages)
        assert metadata["prompt_version_id"] == prompt.version_id
        assert metadata["prompt_node"] == "content.write"
        assert "Private manuscript" not in str(metadata)
    finally:
        _EXECUTION.reset(token)


def test_remote_schema_references_are_rejected_without_network_access():
    from agent_runtime.prompts.renderer import prompt_hash

    raw = version().model_dump(mode="json")
    raw["output_schema"] = {"$ref": "https://untrusted.example/schema.json"}
    raw["content_hash"] = prompt_hash(raw)
    with pytest.raises(ValueError, match="reference"):
        PromptVersion.model_validate(raw)


async def test_structured_retry_preserves_roles_and_validates_frozen_output_schema():
    from types import SimpleNamespace

    from agent_runtime.execution.context import _EXECUTION, ExecutionContext
    from agent_runtime.execution.structured import invoke_structured, parse_json_block
    from agent_runtime.prompts.renderer import prompt_hash, render_node, rendering_metadata
    from agent_runtime.settings import RuntimeSettings

    raw = version().model_dump(mode="json")
    raw["messages"].insert(0, {"role": "system", "content": "Write valid JSON."})
    raw["output_schema"] = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
    raw["content_hash"] = prompt_hash(raw)
    prompt = PromptVersion.model_validate(raw)
    context = ExecutionContext(None, None, RuntimeSettings(_env_file=None), prompt_versions={"content.write": prompt})
    calls = []

    class Model:
        async def ainvoke(self, messages):
            calls.append(messages)
            assert rendering_metadata(messages)["prompt_version_id"] == prompt.version_id
            return SimpleNamespace(content='{"text":"valid"}' if len(calls) == 2 else '{"text":123}')

    token = _EXECUTION.set(context)
    try:
        _, result = await invoke_structured(
            Model(),
            render_node("content.write", {"title": "Title"}),
            parser=parse_json_block,
            validator=lambda value: None,
            agent_name="content.write",
            format_name="JSON",
        )
        assert result == [{"text": "valid"}]
        assert len(calls) == 2
        assert calls[1][:2] == calls[0]
    finally:
        _EXECUTION.reset(token)
