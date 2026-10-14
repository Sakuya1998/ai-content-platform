"""Execution-scoped persistence, memory and models for the builtin novel workflow."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class NovelDependencies:
    store: object
    memory: object
    models: object
    style_snapshot: dict | None = None


_NOVEL = ContextVar("runtime_novel_dependencies", default=None)


def current_novel():
    return _NOVEL.get()


@contextmanager
def novel_context(store, memory, models, *, style_snapshot=None):
    token = _NOVEL.set(NovelDependencies(store, memory, models, style_snapshot))
    try:
        yield
    finally:
        _NOVEL.reset(token)
