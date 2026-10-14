"""OpenAI-compatible binary speech protocol and an independent provider registry.

Verified against the official API reference:
https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create/
"""

import base64
import binascii
import json
import re
from collections.abc import Callable
from uuid import uuid4

import httpx

from agent_runtime.domain.speech import SpeechProvider, SpeechRequest, SpeechResult, SpeechRoute
from agent_runtime.infrastructure.media.errors import SubmissionUnknown, VideoProviderError

_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "pcm": "audio/pcm",
}
_MIME_ALIASES = {
    "mp3": {"audio/mpeg", "audio/mp3"},
    "wav": {"audio/wav", "audio/wave", "audio/x-wav"},
    "opus": {"audio/ogg", "audio/opus"},
    "aac": {"audio/aac", "audio/aacp", "audio/x-aac"},
    "flac": {"audio/flac", "audio/x-flac"},
    "pcm": {"audio/pcm", "audio/l16", "audio/x-pcm"},
}


def _safe_request_id(identity: str | None, *secrets: str) -> str | None:
    if not identity or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", identity):
        return None
    if any(secret and secret in identity for secret in secrets):
        return None
    return identity


def _valid_audio(data: bytes, audio_format: str) -> bool:
    if not data:
        return False
    if audio_format == "mp3":
        return data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    if audio_format == "wav":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if audio_format == "opus":
        return data.startswith(b"OggS")
    if audio_format == "aac":
        return len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0
    if audio_format == "flac":
        return data.startswith(b"fLaC")
    return len(data) % 2 == 0  # PCM is signed, little-endian 16-bit audio.


class OpenAISpeechProvider:
    def __init__(self, route: SpeechRoute, *, client: httpx.AsyncClient | None = None):
        self.route = route
        self._client = client or httpx.AsyncClient(timeout=120, follow_redirects=False)
        self._owns_client = client is None
        self._url = route.base_url.rstrip("/") + "/audio/speech"

    async def synthesize(self, request: SpeechRequest, *, max_bytes: int) -> SpeechResult:
        self.route.validate_request(request)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("speech audio byte limit must be a positive integer")
        parameters = request.parameters
        audio_format = parameters.get("format", "mp3")
        payload = {
            "model": self.route.model_name,
            "input": request.text,
            "voice": parameters.get("voice", self.route.speech_capabilities.voices[0]),
            "response_format": audio_format,
            "speed": parameters.get("speed", 1.0),
        }
        if "instructions" in parameters:
            payload["instructions"] = parameters["instructions"]
        try:
            async with self._client.stream(
                "POST",
                self._url,
                json=payload,
                headers={"Authorization": "Bearer " + self.route.api_key.get_secret_value()},
                follow_redirects=False,
            ) as response:
                if not response.is_success:
                    error = (
                        SubmissionUnknown
                        if response.status_code >= 500 or response.status_code == 408
                        else VideoProviderError
                    )
                    raise error(f"speech provider returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in _MIME_ALIASES[audio_format] | {"", "application/octet-stream"}:
                    raise SubmissionUnknown("speech provider returned an invalid audio content type")
                length = response.headers.get("content-length")
                if length is not None:
                    if not length.isdecimal():
                        raise SubmissionUnknown("speech provider returned an invalid audio length")
                    if int(length) > max_bytes:
                        raise SubmissionUnknown("speech audio exceeds byte limit")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        raise SubmissionUnknown("speech audio exceeds byte limit")
                    data.extend(chunk)
                if length is not None and not response.headers.get("content-encoding") and len(data) != int(length):
                    raise SubmissionUnknown("speech audio response is incomplete")
                audio = bytes(data)
                if not _valid_audio(audio, audio_format):
                    raise SubmissionUnknown("speech provider returned invalid audio")
                identity = response.headers.get("x-request-id")
                request_id = _safe_request_id(identity, self.route.api_key.get_secret_value())
                return SpeechResult(
                    data=audio,
                    content_type=_CONTENT_TYPES[audio_format],
                    extension=audio_format,
                    request_id=request_id,
                )
        except httpx.HTTPError:
            raise SubmissionUnknown("speech provider transport failed") from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class _DoubaoAudioStream:
    """Bound audio and metadata independently and require a final success event."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.data = bytearray()
        self.pending = bytearray()
        self.wire_bytes = 0
        self.wire_limit = max_bytes * 4 + 65536
        self.complete = False
        self.usage = {}

    def feed(self, chunk: bytes) -> None:
        self.wire_bytes += len(chunk)
        if self.wire_bytes > self.wire_limit:
            raise SubmissionUnknown("speech stream exceeds wire byte limit")
        self.pending.extend(chunk)
        while b"\n" in self.pending:
            line, _, remaining = self.pending.partition(b"\n")
            self.pending = bytearray(remaining)
            self._event(line)

    def finish(self) -> bytes:
        if self.pending.strip():
            self._event(self.pending)
        if not self.complete or not self.data:
            raise SubmissionUnknown("speech audio response is incomplete")
        return bytes(self.data)

    def _event(self, line: bytes) -> None:
        if not line.strip():
            return
        if self.complete:
            raise SubmissionUnknown("speech stream continued after completion")
        try:
            event = json.loads(line)
        except ValueError, UnicodeError:
            raise SubmissionUnknown("speech provider returned invalid audio JSON") from None
        if not isinstance(event, dict) or type(event.get("code")) is not int:
            raise SubmissionUnknown("speech provider returned invalid audio event")
        code = event["code"]
        if code == 20000000:
            if event.get("data") is not None:
                raise SubmissionUnknown("speech provider returned invalid completion event")
            usage = event.get("usage", {})
            if not isinstance(usage, dict):
                raise SubmissionUnknown("speech provider returned invalid usage")
            if "text_words" in usage:
                if type(usage["text_words"]) is not int or usage["text_words"] < 0:
                    raise SubmissionUnknown("speech provider returned invalid usage")
                self.usage = {"text_words": usage["text_words"]}
            self.complete = True
            return
        if code != 0:
            error = VideoProviderError if code in {40402003, 45000000} and not self.data else SubmissionUnknown
            raise error(f"speech provider returned code {code}")
        encoded = event.get("data")
        if encoded is None and isinstance(event.get("sentence"), dict):
            return
        if not isinstance(encoded, str) or not encoded:
            raise SubmissionUnknown("speech provider returned invalid audio chunk")
        if len(encoded) > ((self.max_bytes - len(self.data) + 2) // 3) * 4:
            raise SubmissionUnknown("speech audio exceeds byte limit")
        try:
            audio = base64.b64decode(encoded, validate=True)
        except ValueError, binascii.Error:
            raise SubmissionUnknown("speech provider returned invalid base64 audio") from None
        if len(self.data) + len(audio) > self.max_bytes:
            raise SubmissionUnknown("speech audio exceeds byte limit")
        self.data.extend(audio)


class VolcengineSpeechProvider:
    """Doubao HTTP chunked TTS V3, verified against the official docs and demo.

    https://www.volcengine.com/docs/6561/1598757
    Supports current X-Api-Key auth and legacy APP ID/Access Token auth.
    """

    def __init__(self, route: SpeechRoute, *, client: httpx.AsyncClient | None = None):
        capabilities = route.speech_capabilities
        if not capabilities.formats or set(capabilities.formats) - {"mp3", "opus", "pcm"}:
            raise ValueError("Doubao speech formats must be mp3, opus or pcm")
        if not 0.5 <= capabilities.min_speed <= capabilities.max_speed <= 2:
            raise ValueError("Doubao speech speed limits must be between 0.5 and 2")
        self._auth = self._credentials(route)
        self.route = route
        self._client = client or httpx.AsyncClient(timeout=120, follow_redirects=False)
        self._owns_client = client is None
        base = route.base_url.rstrip("/")
        if not base.endswith("/api/v3/tts/unidirectional"):
            base += "/tts/unidirectional" if base.endswith("/api/v3") else "/api/v3/tts/unidirectional"
        self._url = base

    @staticmethod
    def _credentials(route: SpeechRoute) -> dict[str, str]:
        secret = route.api_key.get_secret_value()
        if secret.lstrip().startswith("{"):
            try:
                credentials = json.loads(secret)
            except ValueError:
                raise ValueError("Doubao speech credentials must be valid JSON") from None
            if not isinstance(credentials, dict) or set(credentials) != {"app_id", "access_token"}:
                raise ValueError("Doubao speech credentials require app_id and access_token")
            auth = {"X-Api-App-Id": credentials["app_id"], "X-Api-Access-Key": credentials["access_token"]}
        else:
            auth = {"X-Api-Key": secret}
        if any(not isinstance(value, str) or not re.fullmatch(r"[!-~]+", value) for value in auth.values()):
            raise ValueError("Doubao speech credentials contain invalid header values")
        if not re.fullmatch(r"[!-~]+", route.model_name):
            raise ValueError("Doubao speech resource ID contains invalid header values")
        return auth

    async def synthesize(self, request: SpeechRequest, *, max_bytes: int) -> SpeechResult:
        self.route.validate_request(request)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("speech audio byte limit must be a positive integer")
        parameters = request.parameters
        audio_format = parameters.get("format", "mp3")
        additions = {}
        if "language" in parameters:
            additions["explicit_language"] = parameters["language"]
        if "instructions" in parameters:
            additions["context_texts"] = [parameters["instructions"]]
        req_params = {
            "text": request.text,
            "speaker": parameters.get("voice", self.route.speech_capabilities.voices[0]),
            "audio_params": {
                "format": "ogg_opus" if audio_format == "opus" else audio_format,
                "sample_rate": 24000,
                "speech_rate": (parameters.get("speed", 1.0) - 1) * 100,
            },
        }
        if additions:
            req_params["additions"] = json.dumps(additions, ensure_ascii=False)
        headers = {
            **self._auth,
            "X-Api-Resource-Id": self.route.model_name,
            "X-Api-Request-Id": str(uuid4()),
            "X-Control-Require-Usage-Tokens-Return": "text_words",
        }
        try:
            async with self._client.stream(
                "POST",
                self._url,
                json={"user": {"uid": "agent-runtime"}, "req_params": req_params},
                headers=headers,
                follow_redirects=False,
            ) as response:
                if not response.is_success:
                    error = (
                        SubmissionUnknown
                        if response.status_code >= 500 or response.status_code == 408
                        else VideoProviderError
                    )
                    raise error(f"speech provider returned HTTP {response.status_code}")
                stream = _DoubaoAudioStream(max_bytes)
                async for chunk in response.aiter_bytes():
                    stream.feed(chunk)
                audio = stream.finish()
                if not _valid_audio(audio, audio_format):
                    raise SubmissionUnknown("speech provider returned invalid audio")
                identity = response.headers.get("x-tt-logid")
                request_id = _safe_request_id(identity, self.route.api_key.get_secret_value(), *self._auth.values())
                return SpeechResult(
                    data=audio,
                    content_type=_CONTENT_TYPES[audio_format],
                    extension=audio_format,
                    usage=stream.usage,
                    request_id=request_id,
                )
        except httpx.HTTPError:
            raise SubmissionUnknown("speech provider transport failed") from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


_PROVIDERS: dict[str, Callable[..., SpeechProvider]] = {
    "openai": OpenAISpeechProvider,
    "openai_compatible": OpenAISpeechProvider,
    "openai-compatible": OpenAISpeechProvider,
    "volcengine_tts": VolcengineSpeechProvider,
}


def speech_provider_factory(provider: str) -> Callable[..., SpeechProvider]:
    """Check protocol availability before planning or charging external calls."""
    try:
        return _PROVIDERS[provider]
    except KeyError:
        raise ValueError("speech provider protocol is not implemented") from None


def create_speech_provider(route: SpeechRoute, *, client: httpx.AsyncClient | None = None) -> SpeechProvider:
    return speech_provider_factory(route.provider)(route, client=client)
