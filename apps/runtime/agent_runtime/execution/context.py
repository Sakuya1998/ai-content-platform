"""Task-scoped dependencies and frozen capability provenance."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class ExecutionContext:
    job: object
    store: object
    settings: object
    model_routes: dict = field(default_factory=dict)
    resource_versions: dict = field(default_factory=dict)
    prompt_versions: dict = field(default_factory=dict)
    prompt_pack_versions: dict = field(default_factory=dict)
    prompt_renderings: dict = field(default_factory=dict)
    creation_policy: object | None = None
    cancel_requested: bool = False

    async def record_model_call(self, purpose, provider, model, prompt_hash, usage, metadata):
        await asyncio.to_thread(
            self.store.record_model_call,
            self.job,
            self.settings.worker_id,
            purpose,
            provider,
            model,
            prompt_hash,
            usage,
            metadata,
        )


_EXECUTION: ContextVar[ExecutionContext | None] = ContextVar("platform_execution", default=None)


def current_execution(*, required=True):
    context = _EXECUTION.get()
    if context is None and required:
        raise RuntimeError("handler requires a claimed Runtime execution")
    return context
