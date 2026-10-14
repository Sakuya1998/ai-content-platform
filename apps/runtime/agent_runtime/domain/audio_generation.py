"""Independent model routes for generated music and sound effects."""

import math
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from agent_runtime.domain.media import _http_url

MUSIC_OUTPUT_FORMATS = {"mp3_44100_128", "mp3_48000_192"}
SOUND_EFFECT_OUTPUT_FORMATS = {
    "mp3_22050_32",
    "mp3_24000_48",
    "mp3_44100_32",
    "mp3_44100_64",
    "mp3_44100_96",
    "mp3_44100_128",
    "mp3_44100_192",
}
MUSIC_MODELS = {"music_v1", "music_v2", "music_v2_5"}
SOUND_EFFECT_MODELS = {"eleven_text_to_sound_v2"}


class MusicRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1, max_length=16000, repr=False)
    parameters: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def validate_parameters(self):
        if not self.prompt.strip():
            raise ValueError("music prompt cannot be blank")
        if self.parameters.keys() - {"output_format", "music_length_ms", "force_instrumental"}:
            raise ValueError("unsupported music parameters")
        output_format = self.parameters.get("output_format")
        if output_format is not None and output_format not in MUSIC_OUTPUT_FORMATS:
            raise ValueError("unsupported music output format")
        duration = self.parameters.get("music_length_ms")
        if duration is not None and (type(duration) is not int or not 3000 <= duration <= 600000):
            raise ValueError("music_length_ms must be an integer between 3000 and 600000")
        instrumental = self.parameters.get("force_instrumental")
        if instrumental is not None and type(instrumental) is not bool:
            raise ValueError("force_instrumental must be boolean")
        return self


class MusicCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    output_formats: list[str] = Field(min_length=1, max_length=2)
    min_duration_ms: int = Field(default=3000, ge=3000, le=600000)
    max_duration_ms: int = Field(default=600000, ge=3000, le=600000)
    instrumental: bool = True

    @model_validator(mode="after")
    def validate_limits(self):
        if self.min_duration_ms > self.max_duration_ms:
            raise ValueError("music minimum duration cannot exceed maximum duration")
        if len(self.output_formats) != len(set(self.output_formats)) or set(self.output_formats) - MUSIC_OUTPUT_FORMATS:
            raise ValueError("invalid music output formats")
        return self


class MusicRoute(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(repr=False)
    profile_version_id: str = ""
    music_capabilities: MusicCapabilities

    @model_validator(mode="after")
    def validate_route(self):
        parsed = urlsplit(self.base_url)
        if self.provider != "elevenlabs" or self.model_name not in MUSIC_MODELS:
            raise ValueError("unsupported music provider or model")
        if (
            not self.api_key.get_secret_value().strip()
            or not _http_url(self.base_url)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("music base_url must be an HTTP(S) URL without credentials or query")
        return self

    def identity(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"api_key"})

    def validate_request(self, request: MusicRequest) -> None:
        parameters = request.parameters
        capabilities = self.music_capabilities
        output_format = parameters.get("output_format", capabilities.output_formats[0])
        if output_format not in capabilities.output_formats:
            raise ValueError("model does not support requested music output format")
        duration = parameters.get("music_length_ms")
        if duration is not None and not capabilities.min_duration_ms <= duration <= capabilities.max_duration_ms:
            raise ValueError("model does not support requested music duration")
        if parameters.get("force_instrumental", False) and not capabilities.instrumental:
            raise ValueError("model does not support instrumental generation")


class SoundEffectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=16000, repr=False)
    parameters: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def validate_parameters(self):
        if not self.text.strip():
            raise ValueError("sound effect prompt cannot be blank")
        if self.parameters.keys() - {"output_format", "duration_seconds", "prompt_influence", "loop"}:
            raise ValueError("unsupported sound effect parameters")
        output_format = self.parameters.get("output_format")
        if output_format is not None and output_format not in SOUND_EFFECT_OUTPUT_FORMATS:
            raise ValueError("unsupported sound effect output format")
        duration = self.parameters.get("duration_seconds")
        if duration is not None and (
            type(duration) not in {int, float} or not math.isfinite(duration) or not 0.5 <= duration <= 30
        ):
            raise ValueError("duration_seconds must be between 0.5 and 30")
        influence = self.parameters.get("prompt_influence")
        if influence is not None and (
            type(influence) not in {int, float} or not math.isfinite(influence) or not 0 <= influence <= 1
        ):
            raise ValueError("prompt_influence must be between 0 and 1")
        loop = self.parameters.get("loop")
        if loop is not None and type(loop) is not bool:
            raise ValueError("loop must be boolean")
        return self


class SoundEffectCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    output_formats: list[str] = Field(min_length=1, max_length=7)
    min_duration_seconds: float = Field(default=0.5, ge=0.5, le=30, allow_inf_nan=False)
    max_duration_seconds: float = Field(default=30, ge=0.5, le=30, allow_inf_nan=False)
    prompt_influence: bool = True
    loop: bool = True

    @model_validator(mode="after")
    def validate_limits(self):
        if self.min_duration_seconds > self.max_duration_seconds:
            raise ValueError("sound effect minimum duration cannot exceed maximum duration")
        if (
            len(self.output_formats) != len(set(self.output_formats))
            or set(self.output_formats) - SOUND_EFFECT_OUTPUT_FORMATS
        ):
            raise ValueError("invalid sound effect output formats")
        return self


class SoundEffectRoute(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(repr=False)
    profile_version_id: str = ""
    sound_effect_capabilities: SoundEffectCapabilities

    @model_validator(mode="after")
    def validate_route(self):
        parsed = urlsplit(self.base_url)
        if self.provider != "elevenlabs" or self.model_name not in SOUND_EFFECT_MODELS:
            raise ValueError("unsupported sound effect provider or model")
        if (
            not self.api_key.get_secret_value().strip()
            or not _http_url(self.base_url)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("sound effect base_url must be an HTTP(S) URL without credentials or query")
        return self

    def identity(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"api_key"})

    def validate_request(self, request: SoundEffectRequest) -> None:
        parameters = request.parameters
        capabilities = self.sound_effect_capabilities
        output_format = parameters.get("output_format", capabilities.output_formats[0])
        if output_format not in capabilities.output_formats:
            raise ValueError("model does not support requested sound effect output format")
        duration = parameters.get("duration_seconds")
        if (
            duration is not None
            and not capabilities.min_duration_seconds <= duration <= capabilities.max_duration_seconds
        ):
            raise ValueError("model does not support requested sound effect duration")
        if "prompt_influence" in parameters and not capabilities.prompt_influence:
            raise ValueError("model does not support prompt influence")
        if parameters.get("loop", False) and not capabilities.loop:
            raise ValueError("model does not support looping sound effects")


class AudioGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data: bytes = Field(repr=False)
    content_type: str
    extension: str = "mp3"
    usage: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class MusicProvider(Protocol):
    async def compose(self, request: MusicRequest, *, max_bytes: int) -> AudioGenerationResult: ...

    async def aclose(self) -> None: ...


class SoundEffectProvider(Protocol):
    async def generate(self, request: SoundEffectRequest, *, max_bytes: int) -> AudioGenerationResult: ...

    async def aclose(self) -> None: ...
