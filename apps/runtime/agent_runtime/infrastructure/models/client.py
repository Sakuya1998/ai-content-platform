"""Runtime model clients with no dependency on legacy SQLite model routes."""

import asyncio
import hashlib
import time

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from agent_runtime.execution.context import current_execution
from agent_runtime.execution.errors import RetryableTaskError
from agent_runtime.prompts.renderer import rendering_metadata, validate_rendered_output


def retryable(exc):
    return (
        isinstance(exc, (TimeoutError, ConnectionError))
        or type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}
        or getattr(exc, "status_code", 0) in {429, 500, 502, 503, 504}
    )


class RecordedModel:
    def __init__(self, model, settings, purpose, fallbacks=None):
        self.model, self.settings, self.purpose = model, settings, purpose
        self.fallbacks = fallbacks or []

    async def ainvoke(self, prompt):
        context = current_execution(required=False)
        original_prompt = prompt
        rendered = rendering_metadata(prompt)
        if context:
            if hasattr(context.store, "model_budget"):
                await asyncio.to_thread(
                    context.store.model_budget,
                    context.job,
                    context.settings.worker_id,
                    context.settings.max_project_tokens,
                )
            instructions = []
            for resource in context.resource_versions.values():
                if resource.get("kind") == "skill":
                    text = resource.get("payload", {}).get("instructions", "")
                    if not isinstance(text, str):
                        raise ValueError("skill instructions must be text")
                    instructions.append(text)
            if instructions:
                text = "\nPublished skill instructions:\n" + "\n".join(instructions)
                prompt = [*prompt, {"role": "user", "content": text}] if isinstance(prompt, list) else prompt + text
        started = time.monotonic()
        chosen_model = self.model
        try:
            response = await self.model.ainvoke(prompt)
        except Exception as exc:
            recovered = False
            for fallback in self.fallbacks:
                try:
                    response = await fallback.ainvoke(prompt)
                    chosen_model = fallback
                    recovered = True
                    break
                except Exception:
                    continue
            if not recovered:
                if retryable(exc):
                    raise RetryableTaskError(type(exc).__name__) from None
                raise
        if context and context.job is not None:
            usage = getattr(response, "usage_metadata", {}) or {}
            await context.record_model_call(
                self.purpose,
                getattr(chosen_model, "_platform_provider", self.settings.llm_provider),
                getattr(chosen_model, "model_name", None) or getattr(chosen_model, "model", self.settings.llm_model),
                hashlib.sha256(str(prompt).encode()).hexdigest(),
                usage,
                {**rendered, "elapsed_ms": int((time.monotonic() - started) * 1000)},
            )
        if rendered and context.prompt_versions[rendered["prompt_node"]].output_schema.get("type") == "string":
            validate_rendered_output(original_prompt, response.content)
        return response


class RuntimeModels:
    def __init__(self, settings):
        self.settings = settings

    def embedding_identity(self, *, execution=None):
        execution = execution or current_execution(required=False)
        route = execution.model_routes.get("embedding", {}) if execution else {}
        model = route.get("model_name") or getattr(self.settings, "embedding_model", "")
        if not model:
            raise ValueError("embedding model provenance is not configured")
        base_url = route.get("base_url") or getattr(self.settings, "embedding_base_url", "") or ""
        return {
            "embedding_model": model,
            "embedding_profile_version_id": route.get("profile_version_id", ""),
            "embedding_provider": route.get("provider", "openai"),
            "embedding_base_url_hash": hashlib.sha256(base_url.encode()).hexdigest(),
            "index_version": "novel-memory@1",
        }

    def chat(self, purpose):
        cfg = self.settings
        execution = current_execution(required=False)
        route = execution.model_routes.get(purpose) if execution else None
        if route:
            cfg = cfg.model_copy(
                update={
                    "llm_provider": route["provider"],
                    "llm_model": route["model_name"],
                    "analysis_model": route["model_name"],
                    "llm_base_url": route.get("base_url", ""),
                    "llm_max_tokens": route.get("max_tokens", cfg.llm_max_tokens),
                }
            )
            from pydantic import SecretStr

            cfg.llm_api_key = SecretStr(route.get("api_key") or "not-required")
        if not cfg.llm_model or not cfg.llm_api_key.get_secret_value():
            raise ValueError("Runtime LLM model and API key must be configured")
        model_name = cfg.analysis_model or cfg.llm_model if purpose == "analysis" else cfg.llm_model
        params = {
            "model": model_name,
            "api_key": cfg.llm_api_key.get_secret_value(),
            "max_tokens": cfg.llm_max_tokens,
            "timeout": cfg.llm_timeout_seconds,
            "max_retries": 0,
            "temperature": 0.2 if purpose == "analysis" else 0.7,
        }
        if cfg.llm_base_url:
            params["base_url"] = cfg.llm_base_url
        if cfg.llm_provider == "openai_compatible" and not cfg.llm_base_url:
            raise ValueError("compatible Runtime model base URL must be configured")
        model = ChatAnthropic(**params) if cfg.llm_provider == "anthropic" else ChatOpenAI(**params)
        fallbacks = []
        for backup in route.get("fallback", []) if route else []:
            options = {
                **params,
                "model": backup["model_name"],
                "api_key": backup.get("api_key") or "not-required",
                "max_tokens": backup.get("max_tokens", cfg.llm_max_tokens),
            }
            options.pop("base_url", None)
            if backup.get("base_url"):
                options["base_url"] = backup["base_url"]
            fallbacks.append(ChatAnthropic(**options) if backup["provider"] == "anthropic" else ChatOpenAI(**options))
            object.__setattr__(fallbacks[-1], "_platform_provider", backup["provider"])
        object.__setattr__(model, "_platform_provider", cfg.llm_provider)
        return RecordedModel(model, cfg, purpose, fallbacks)

    async def embed(self, texts):
        cfg = self.settings
        execution = current_execution(required=False)
        route = execution.model_routes.get("embedding") if execution else None
        if route:
            from pydantic import SecretStr

            cfg = cfg.model_copy(
                update={
                    "embedding_model": route["model_name"],
                    "embedding_base_url": route.get("base_url", ""),
                    "embedding_api_key": SecretStr(route.get("api_key") or "not-required"),
                }
            )
        if not cfg.embedding_model or not cfg.embedding_api_key.get_secret_value():
            raise ValueError("Runtime embedding model and API key must be configured")
        model = OpenAIEmbeddings(
            model=cfg.embedding_model,
            api_key=cfg.embedding_api_key.get_secret_value(),
            base_url=cfg.embedding_base_url or None,
            dimensions=1536,
            check_embedding_ctx_length=False,
            max_retries=0,
            request_timeout=cfg.llm_timeout_seconds,
        )
        try:
            return await model.aembed_documents(texts)
        except Exception as exc:
            for backup in route.get("fallback", []) if route else []:
                if (backup.get("model_name"), backup.get("base_url", ""), backup.get("provider")) != (
                    route.get("model_name"),
                    route.get("base_url", ""),
                    route.get("provider"),
                ):
                    continue
                candidate = OpenAIEmbeddings(
                    model=backup["model_name"],
                    api_key=backup.get("api_key") or "not-required",
                    base_url=backup.get("base_url") or None,
                    dimensions=1536,
                    check_embedding_ctx_length=False,
                    max_retries=0,
                    request_timeout=cfg.llm_timeout_seconds,
                )
                try:
                    return await candidate.aembed_documents(texts)
                except Exception:
                    continue
            if retryable(exc):
                raise RetryableTaskError(type(exc).__name__) from None
            raise
