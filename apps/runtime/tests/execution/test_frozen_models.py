import pytest

from agent_runtime.execution.bindings import bind_execution_models
from agent_runtime.execution.context import _EXECUTION, ExecutionContext
from agent_runtime.infrastructure.models.client import RuntimeModels
from agent_runtime.settings import RuntimeSettings


async def test_snapshot_model_route_overrides_mutable_environment_and_keeps_secret_private():
    from agent_runtime.domain.capabilities import CapabilitySnapshot
    from tests.fixtures.prompts import prompt_versions

    context = ExecutionContext(
        None, None, RuntimeSettings(
            _env_file=None, llm_model="env-model", llm_api_key="env-key",
            execution_encryption_key=bytes(range(32)).hex(),
        )
    )
    route = {
        "provider": "openai_compatible",
        "model_name": "frozen-model",
        "base_url": "http://provider.test/v1",
        "route_version_id": "version-1",
        "profile_version_id": "profile-1",
        "credential_ref": "profile-1",
        "max_tokens": 4096,
        "fallback": [],
    }

    token = _EXECUTION.set(context)
    try:
        snapshot = CapabilitySnapshot(
            workflow="content.generate@1",
            schema_versions={"content_ir": "1.0"},
            model_routes={"creative": "version-1"},
            prompt_packs=["frozen-pack-version"],
            prompt_bindings={
                "memory.refresh": __import__("tests.fixtures.prompts", fromlist=["prompt_versions"])
                .prompt_versions()["memory.refresh"]
                .version_id
            },
        )
        await bind_execution_models(snapshot, "workspace", resolved_resources={
            "prompts": {"memory.refresh": prompt_versions()["memory.refresh"].model_dump(mode="json")},
            "prompt_pack_id": "frozen-pack-version",
            "model_routes": {"creative": route},
            "credentials": {"profile-1": "AAECAwQFBgcICQoLN3C5bayBp2mgMvLow4wMDbA9ISyez8BLVpsdh2sZiw=="},
        })
        model = RuntimeModels(context.settings).chat("creative")
        assert model.model.model_name == "frozen-model"
        assert model.model.openai_api_key.get_secret_value() == "provider-secret"
    finally:
        _EXECUTION.reset(token)


async def test_snapshot_requires_each_versioned_route_to_resolve(monkeypatch):
    from agent_runtime.domain.capabilities import CapabilitySnapshot
    context = ExecutionContext(None, None, RuntimeSettings(_env_file=None))
    token = _EXECUTION.set(context)
    try:
        with pytest.raises(ValueError):
            await bind_execution_models(
                CapabilitySnapshot(
                    workflow="content.generate@1",
                    schema_versions={"content_ir": "1.0"},
                    model_routes={"creative": "missing"},
                ),
                "workspace",
            )
    finally:
        _EXECUTION.reset(token)


async def test_published_skill_instructions_preserve_chat_message_roles():
    from types import SimpleNamespace

    from agent_runtime.infrastructure.models.client import RecordedModel

    calls = []

    class Model:
        async def ainvoke(self, messages):
            calls.append(messages)
            return SimpleNamespace(content="result")

    context = ExecutionContext(None, None, RuntimeSettings(_env_file=None))
    context.resource_versions["skill"] = {"kind": "skill", "payload": {"instructions": "Keep names consistent."}}
    original = [{"role": "system", "content": "System policy"}, {"role": "user", "content": "Write a scene"}]
    token = _EXECUTION.set(context)
    try:
        await RecordedModel(Model(), context.settings, "creative").ainvoke(original)
        assert calls[0][:2] == original
        assert calls[0][-1]["role"] == "user"
        assert "Keep names consistent." in calls[0][-1]["content"]
        assert len(original) == 2
    finally:
        _EXECUTION.reset(token)


async def test_snapshot_resources_bind_without_control_http(monkeypatch):
    import httpx

    from agent_runtime.domain.capabilities import CapabilitySnapshot
    from tests.fixtures.prompts import prompt_versions

    async def forbidden_request(*args, **kwargs):
        raise AssertionError("snapshot-backed execution called Control HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "get", forbidden_request)
    prompt = prompt_versions()["memory.refresh"]
    context = ExecutionContext(
        None,
        None,
        RuntimeSettings(_env_file=None, execution_encryption_key=bytes(range(32)).hex()),
    )
    snapshot = CapabilitySnapshot(
        workflow="content.generate@1",
        schema_versions={"content_ir": "1.0"},
        model_routes={"creative": "route-1"},
        prompt_packs=["pack-1"],
        prompt_bindings={"memory.refresh": prompt.version_id},
    )
    resources = {
        "prompts": {"memory.refresh": prompt.model_dump(mode="json")},
        "prompt_pack_id": "pack-1",
        "model_routes": {
            "creative": {
                "route_version_id": "route-1",
                "profile_version_id": "profile-1",
                "provider": "openai_compatible",
                "model_name": "writer-v1",
                "base_url": "https://provider.example/v1",
                "credential_ref": "profile-1",
                "fallback": [],
            }
        },
        "credentials": {"profile-1": "AAECAwQFBgcICQoLN3C5bayBp2mgMvLow4wMDbA9ISyez8BLVpsdh2sZiw=="},
        "governance": {},
    }
    token = _EXECUTION.set(context)
    try:
        await bind_execution_models(snapshot, "workspace", resolved_resources=resources)
        assert context.model_routes["creative"]["api_key"] == "provider-secret"
        assert context.prompt_versions["memory.refresh"].version_id == prompt.version_id
        assert "api_key" not in resources["model_routes"]["creative"]
    finally:
        _EXECUTION.reset(token)
