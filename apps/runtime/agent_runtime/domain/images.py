"""Typed image requests and immutable, credential-free routing identities."""

from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from agent_runtime.domain.media import _http_url


def validate_image_bytes(data: bytes, content_type: str) -> None:
    """Check a supported image file signature without decoding or exposing content."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("image content must contain bytes")
    valid = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }
    if not valid.get(content_type, False):
        raise ValueError("image content does not match its supported format")


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1, repr=False)
    parameters: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def validate_parameters(self):
        if not self.prompt.strip():
            raise ValueError("image prompt cannot be blank")
        if self.parameters.keys() - {"operation", "size", "format", "n"}:
            raise ValueError("unsupported image parameters")
        operation = self.parameters.get("operation", "generate")
        if not isinstance(operation, str) or operation not in {"generate", "edit"}:
            raise ValueError("image operation must be generate or edit")
        count = self.parameters.get("n", 1)
        if type(count) is not int or not 1 <= count <= 4:
            raise ValueError("n must be an integer between 1 and 4")
        if "size" in self.parameters:
            size = self.parameters["size"]
            if not isinstance(size, str) or not size.strip():
                raise ValueError("size must be a nonempty string")
        image_format = self.parameters.get("format", "png")
        if not isinstance(image_format, str) or image_format not in {"png", "jpeg", "webp"}:
            raise ValueError("image format must be png, jpeg or webp")
        return self


class ImageCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    generation: bool = False
    editing: bool = False
    sizes: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    max_images: int = Field(default=1, ge=1, le=4)

    @model_validator(mode="after")
    def validate_capabilities(self):
        if any(not size.strip() for size in self.sizes):
            raise ValueError("image sizes cannot be blank")
        if any(value not in {"png", "jpeg", "webp"} for value in self.formats):
            raise ValueError("unsupported image capability format")
        return self


class ImageRoute(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(repr=False)
    profile_version_id: str = ""
    image_capabilities: ImageCapabilities

    @model_validator(mode="after")
    def validate_base_url(self):
        if not _http_url(self.base_url):
            raise ValueError("image base_url must be an HTTP(S) URL without credentials")
        parsed = urlsplit(self.base_url)
        if parsed.query or parsed.fragment:
            raise ValueError("image base_url cannot contain a query or fragment")
        return self

    def identity(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"api_key"})

    def validate_request(self, request: ImageRequest) -> None:
        capabilities = self.image_capabilities
        required = "editing" if request.parameters.get("operation", "generate") == "edit" else "generation"
        if not getattr(capabilities, required):
            raise ValueError(f"model does not support {required}")
        if "size" in request.parameters and request.parameters["size"] not in capabilities.sizes:
            raise ValueError("model does not support requested size")
        if request.parameters.get("format", "png") not in capabilities.formats:
            raise ValueError("model does not support requested format")
        if request.parameters.get("n", 1) > capabilities.max_images:
            raise ValueError("model does not support requested n")


class ImageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    data: bytes | None = Field(default=None, repr=False)
    url: str | None = Field(default=None, repr=False)
    content_type: str
    extension: str

    @model_validator(mode="after")
    def validate_content(self):
        if (self.data is None) == (self.url is None):
            raise ValueError("image output requires exactly one content source")
        if self.data is not None and not self.data:
            raise ValueError("image output is empty")
        if self.url is not None and not _http_url(self.url):
            raise ValueError("image output URL must use HTTP(S) without credentials")
        if self.extension not in {"png", "jpeg", "webp"} or self.content_type != "image/" + self.extension:
            raise ValueError("image content type and extension must match a supported format")
        return self


class ImageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    images: list[ImageOutput] = Field(min_length=1, max_length=4)
    usage: dict[str, Any] = Field(default_factory=dict, repr=False)
    request_id: str | None = None


class ImageProvider(Protocol):
    def validate_request(self, request: ImageRequest) -> None: ...

    async def generate(
        self,
        request: ImageRequest,
        *,
        source_image: bytes | None = None,
        source_content_type: str = "image/png",
        max_bytes: int,
    ) -> ImageResult: ...

    async def aclose(self) -> None: ...
