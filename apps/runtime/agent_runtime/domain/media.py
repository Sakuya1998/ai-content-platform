"""Typed video requests and immutable provider routing contracts."""

from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


def _http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1, repr=False)
    parameters: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def validate_parameters(self):
        if not self.prompt.strip():
            raise ValueError("video prompt cannot be blank")
        allowed = {
            "duration",
            "ratio",
            "resolution",
            "generate_audio",
            "camera_fixed",
            "watermark",
            "seed",
            "image_url",
        }
        if self.parameters.keys() - allowed:
            raise ValueError("unsupported video parameters")
        for key, value in self.parameters.items():
            if key in {"generate_audio", "camera_fixed", "watermark"} and type(value) is not bool:
                raise ValueError(f"{key} must be boolean")
            if key == "duration" and (type(value) is not int or not 1 <= value <= 120):
                raise ValueError("duration must be an integer between 1 and 120")
            if key == "seed" and (type(value) is not int or not -1 <= value < 2**32):
                raise ValueError("seed must be an integer between -1 and 2^32-1")
            if key in {"ratio", "resolution", "image_url"} and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{key} must be a nonempty string")
            if key == "image_url" and not _http_url(value):
                raise ValueError("image_url must be an HTTP(S) URL without credentials")
        return self


class VideoCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text_to_video: bool = False
    image_to_video: bool = False
    durations: list[int] = Field(default_factory=list)
    ratios: list[str] = Field(default_factory=list)
    resolutions: list[str] = Field(default_factory=list)
    generate_audio: bool = False
    cancellation: bool = False


class VideoRoute(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(repr=False)
    profile_version_id: str = ""
    video_capabilities: VideoCapabilities

    @model_validator(mode="after")
    def validate_base_url(self):
        if not _http_url(self.base_url):
            raise ValueError("video base_url must be an HTTP(S) URL without credentials")
        if urlsplit(self.base_url).query or urlsplit(self.base_url).fragment:
            raise ValueError("video base_url cannot contain a query or fragment")
        return self

    def identity(self) -> dict[str, Any]:
        """The frozen route persisted with a task, excluding credentials."""
        return self.model_dump(mode="json", exclude={"api_key"})

    def validate_request(self, request: VideoRequest) -> None:
        capabilities = self.video_capabilities
        required = "image_to_video" if "image_url" in request.parameters else "text_to_video"
        if not getattr(capabilities, required):
            raise ValueError(f"model does not support {required}")
        for key, supported in (
            ("duration", capabilities.durations),
            ("ratio", capabilities.ratios),
            ("resolution", capabilities.resolutions),
        ):
            if key in request.parameters and request.parameters[key] not in supported:
                raise ValueError(f"model does not support requested {key}")
        if request.parameters.get("generate_audio") and not capabilities.generate_audio:
            raise ValueError("model does not support generate_audio")


class VideoTaskStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["running", "succeeded", "failed", "cancelled"]
    content_url: str | None = Field(default=None, repr=False)
    usage: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)


class VideoProvider(Protocol):
    async def submit(self, request: VideoRequest) -> str: ...

    async def status(self, remote_task_id: str) -> VideoTaskStatus: ...

    async def cancel(self, remote_task_id: str) -> None: ...

    async def aclose(self) -> None: ...


class MediaTask(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    workspace_id: str
    project_id: str
    job_id: str
    request: VideoRequest = Field(repr=False)
    request_hash: str
    route_identity: dict[str, Any]
    review: dict[str, Any] = Field(default_factory=dict, repr=False)
    state: Literal[
        "prepared", "submitting", "submission_unknown", "running", "succeeded", "failed", "cancel_pending", "cancelled"
    ]
    fencing_token: int
    remote_task_id: str | None = None
    content_url: str | None = Field(default=None, repr=False)
    usage: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
