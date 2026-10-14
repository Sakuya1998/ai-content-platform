"""ElevenLabs music and sound-effect protocols with bounded binary responses."""

import re
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from agent_runtime.domain.audio_generation import (
    AudioGenerationResult,
    MusicProvider,
    MusicRequest,
    MusicRoute,
    SoundEffectProvider,
    SoundEffectRequest,
    SoundEffectRoute,
)
from agent_runtime.infrastructure.media.errors import SubmissionUnknown, VideoProviderError


def _mp3(data: bytes) -> bool:
    return data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)


def _request_id(response: httpx.Response, secret: str) -> str | None:
    identity = response.headers.get("request-id") or response.headers.get("x-request-id")
    if not identity or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", identity) or secret in identity:
        return None
    return identity


class _ElevenLabsAudio:
    def __init__(self, base_url: str, api_key: str, *, client: httpx.AsyncClient | None = None):
        self._base_url = base_url.rstrip("/")
        if not urlsplit(self._base_url).path.rstrip("/").endswith("/v1"):
            self._base_url += "/v1"
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=180, follow_redirects=False)
        self._owns_client = client is None

    async def _post(self, endpoint: str, output_format: str, payload: dict, *, max_bytes: int) -> AudioGenerationResult:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("audio byte limit must be a positive integer")
        try:
            async with self._client.stream(
                "POST",
                self._base_url + endpoint,
                params={"output_format": output_format},
                json=payload,
                headers={"xi-api-key": self._api_key},
                follow_redirects=False,
            ) as response:
                if not response.is_success:
                    error = (
                        SubmissionUnknown
                        if response.status_code >= 500 or response.status_code == 408
                        else VideoProviderError
                    )
                    raise error(f"audio generation provider returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"", "audio/mpeg", "audio/mp3", "application/octet-stream"}:
                    raise SubmissionUnknown("audio generation provider returned an invalid content type")
                length = response.headers.get("content-length")
                if length is not None:
                    if not length.isdecimal():
                        raise SubmissionUnknown("audio generation provider returned an invalid response length")
                    if int(length) > max_bytes:
                        raise SubmissionUnknown("generated audio exceeds byte limit")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        raise SubmissionUnknown("generated audio exceeds byte limit")
                    data.extend(chunk)
                if length is not None and not response.headers.get("content-encoding") and len(data) != int(length):
                    raise SubmissionUnknown("audio generation response is incomplete")
                audio = bytes(data)
                if not _mp3(audio):
                    raise SubmissionUnknown("audio generation provider returned invalid MP3 data")
                return AudioGenerationResult(
                    data=audio, content_type="audio/mpeg", request_id=_request_id(response, self._api_key)
                )
        except httpx.HTTPError:
            raise SubmissionUnknown("audio generation provider transport failed") from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ElevenLabsMusicProvider(_ElevenLabsAudio):
    def __init__(self, route: MusicRoute, *, client: httpx.AsyncClient | None = None):
        super().__init__(route.base_url, route.api_key.get_secret_value(), client=client)
        self.route = route

    async def compose(self, request: MusicRequest, *, max_bytes: int) -> AudioGenerationResult:
        self.route.validate_request(request)
        parameters = request.parameters
        payload = {"model_id": self.route.model_name, "prompt": request.prompt}
        for key in ("music_length_ms", "force_instrumental"):
            if key in parameters:
                payload[key] = parameters[key]
        return await self._post(
            "/music",
            parameters.get("output_format", self.route.music_capabilities.output_formats[0]),
            payload,
            max_bytes=max_bytes,
        )


class ElevenLabsSoundEffectProvider(_ElevenLabsAudio):
    def __init__(self, route: SoundEffectRoute, *, client: httpx.AsyncClient | None = None):
        super().__init__(route.base_url, route.api_key.get_secret_value(), client=client)
        self.route = route

    async def generate(self, request: SoundEffectRequest, *, max_bytes: int) -> AudioGenerationResult:
        self.route.validate_request(request)
        parameters = request.parameters
        payload = {"model_id": self.route.model_name, "text": request.text}
        for key in ("duration_seconds", "prompt_influence", "loop"):
            if key in parameters:
                payload[key] = parameters[key]
        return await self._post(
            "/sound-generation",
            parameters.get("output_format", self.route.sound_effect_capabilities.output_formats[0]),
            payload,
            max_bytes=max_bytes,
        )


_MUSIC_PROVIDERS: dict[str, Callable[..., MusicProvider]] = {"elevenlabs": ElevenLabsMusicProvider}
_SOUND_EFFECT_PROVIDERS: dict[str, Callable[..., SoundEffectProvider]] = {"elevenlabs": ElevenLabsSoundEffectProvider}


def music_provider_factory(provider: str) -> Callable[..., MusicProvider]:
    try:
        return _MUSIC_PROVIDERS[provider]
    except KeyError:
        raise ValueError("music provider protocol is not implemented") from None


def sound_effect_provider_factory(provider: str) -> Callable[..., SoundEffectProvider]:
    try:
        return _SOUND_EFFECT_PROVIDERS[provider]
    except KeyError:
        raise ValueError("sound effect provider protocol is not implemented") from None


def create_music_provider(route: MusicRoute, *, client: httpx.AsyncClient | None = None) -> MusicProvider:
    return music_provider_factory(route.provider)(route, client=client)


def create_sound_effect_provider(
    route: SoundEffectRoute, *, client: httpx.AsyncClient | None = None
) -> SoundEffectProvider:
    return sound_effect_provider_factory(route.provider)(route, client=client)
