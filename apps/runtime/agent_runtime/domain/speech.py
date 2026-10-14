"""Typed speech requests, frozen capabilities and synchronous audio results."""

import math
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from agent_runtime.domain.media import _http_url

SpeechFormat = Literal["mp3", "wav", "opus", "aac", "flac", "pcm"]
SPEECH_FORMATS = {"mp3", "wav", "opus", "aac", "flac", "pcm"}


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, repr=False)
    parameters: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def validate_parameters(self):
        if not self.text.strip():
            raise ValueError("speech text cannot be blank")
        allowed = {"voice", "format", "speed", "language", "instructions", "character_id", "scene_id"}
        if self.parameters.keys() - allowed:
            raise ValueError("unsupported speech parameters")
        for key, value in self.parameters.items():
            if key == "speed":
                if type(value) not in {int, float} or not math.isfinite(value) or not 0.25 <= value <= 4:
                    raise ValueError("speech speed must be a number between 0.25 and 4")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"speech {key} must be a nonempty string")
            if key == "format" and value not in SPEECH_FORMATS:
                raise ValueError("unsupported speech format")
        return self


class SpeechCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    voices: list[str] = Field(default_factory=list)
    formats: list[SpeechFormat] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    min_speed: float = Field(default=0.25, ge=0.25, le=4, allow_inf_nan=False)
    max_speed: float = Field(default=4, ge=0.25, le=4, allow_inf_nan=False)
    max_input_chars: int = Field(default=4096, ge=1)
    instructions: bool = False

    @model_validator(mode="after")
    def validate_limits(self):
        if self.min_speed > self.max_speed:
            raise ValueError("speech minimum speed cannot exceed maximum speed")
        for values in (self.voices, self.languages):
            if any(not value.strip() for value in values):
                raise ValueError("speech capability entries cannot be blank")
        return self


class SpeechRoute(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(repr=False)
    profile_version_id: str = ""
    speech_capabilities: SpeechCapabilities

    @model_validator(mode="after")
    def validate_base_url(self):
        if not _http_url(self.base_url):
            raise ValueError("speech base_url must be an HTTP(S) URL without credentials")
        parsed = urlsplit(self.base_url)
        if parsed.query or parsed.fragment:
            raise ValueError("speech base_url cannot contain a query or fragment")
        return self

    def identity(self) -> dict[str, Any]:
        """Persist the frozen route without credentials."""
        return self.model_dump(mode="json", exclude={"api_key"})

    def validate_request(self, request: SpeechRequest) -> None:
        capabilities = self.speech_capabilities
        if len(request.text) > capabilities.max_input_chars:
            raise ValueError("speech input exceeds model character limit")
        voice = request.parameters.get("voice", capabilities.voices[0] if capabilities.voices else None)
        if voice not in capabilities.voices:
            raise ValueError("model does not support requested speech voice")
        if request.parameters.get("format", "mp3") not in capabilities.formats:
            raise ValueError("model does not support requested speech format")
        language = request.parameters.get("language")
        if language is not None and language not in capabilities.languages:
            raise ValueError("model does not support requested speech language")
        if not capabilities.min_speed <= request.parameters.get("speed", 1.0) <= capabilities.max_speed:
            raise ValueError("model does not support requested speech speed")
        if "instructions" in request.parameters and not capabilities.instructions:
            raise ValueError("model does not support speech instructions")


class SpeechResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data: bytes = Field(repr=False)
    content_type: str
    extension: SpeechFormat
    usage: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class SpeechProvider(Protocol):
    async def synthesize(self, request: SpeechRequest, *, max_bytes: int) -> SpeechResult: ...

    async def aclose(self) -> None: ...
