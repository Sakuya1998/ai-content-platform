"""Execution-scoped model call attribution shared by the builtin novel nodes."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class ModelBudgetExceededError(RuntimeError):
    """The project model token budget has been exhausted."""


@dataclass(frozen=True)
class ModelCallContext:
    novel_id: str = ""
    agent: str = "unknown"


_CALL_CONTEXT: ContextVar[ModelCallContext | None] = ContextVar("model_call_context", default=None)


@contextmanager
def model_call_context(novel_id: str, agent: str):
    token = _CALL_CONTEXT.set(ModelCallContext(novel_id=novel_id, agent=agent))
    try:
        yield
    finally:
        _CALL_CONTEXT.reset(token)
