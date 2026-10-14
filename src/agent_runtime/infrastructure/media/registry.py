"""Explicit registry of implemented video protocols."""

from collections.abc import Callable

import httpx

from agent_runtime.domain.media import VideoProvider, VideoRoute
from agent_runtime.infrastructure.media.volcengine import VolcengineVideoProvider

_PROVIDERS: dict[str, Callable[..., VideoProvider]] = {"volcengine": VolcengineVideoProvider}


def video_provider_factory(provider: str) -> Callable[..., VideoProvider]:
    """Validate protocol availability before any planning model calls."""
    try:
        return _PROVIDERS[provider]
    except KeyError:
        raise ValueError("video provider protocol is not implemented") from None


def create_video_provider(route: VideoRoute, *, client: httpx.AsyncClient | None = None) -> VideoProvider:
    factory = video_provider_factory(route.provider)
    return factory(route, client=client)
