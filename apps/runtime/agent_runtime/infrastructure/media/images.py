"""OpenAI image and Ark Seedream protocols, without retries or URL downloads.

Protocol references:
https://developers.openai.com/api/reference/resources/images/methods/generate
https://developers.openai.com/api/docs/guides/image-generation
https://github.com/volcengine/volcengine-python-sdk/blob/master/
volcenginesdkarkruntime/resources/images/images.py
"""

import base64
import binascii
import json
import re
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from agent_runtime.domain.images import (
    ImageOutput,
    ImageProvider,
    ImageRequest,
    ImageResult,
    ImageRoute,
    validate_image_bytes,
)
from agent_runtime.infrastructure.media.errors import SubmissionUnknown, VideoProviderError

_FORMATS = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
_USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "image_tokens",
    "text_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "generated_images",
    "images",
    "output_images",
    "input_tokens_details",
    "output_tokens_details",
    "prompt_tokens_details",
    "completion_tokens_details",
}


def _safe_usage(value: object, *, depth: int = 0) -> dict:
    if not isinstance(value, dict) or depth > 2:
        raise SubmissionUnknown("image provider returned invalid usage")
    safe = {}
    for key, item in value.items():
        number = type(item) in {int, float} and 0 <= item <= 2**63 - 1
        if key not in _USAGE_FIELDS:
            if not number:
                raise SubmissionUnknown("image provider returned invalid usage")
            continue
        if isinstance(item, dict):
            safe[key] = _safe_usage(item, depth=depth + 1)
        elif number:
            safe[key] = item
        else:
            raise SubmissionUnknown("image provider returned invalid usage")
    return safe


class OpenAIImageProvider:
    def __init__(self, route: ImageRoute, *, client: httpx.AsyncClient | None = None):
        self.route = route
        self._client = client or httpx.AsyncClient(timeout=120, follow_redirects=False)
        self._owns_client = client is None
        base = route.base_url.rstrip("/")
        if not urlsplit(base).path:
            base += "/v1"
        self._base = base

    def validate_request(self, request: ImageRequest) -> None:
        """Reject unsupported model parameters before planning or a submission marker."""
        self.route.validate_request(request)
        if self.route.model_name.startswith("dall-e-") and request.parameters.get("format", "png") != "png":
            raise ValueError("legacy image model does not support requested format")
        if self.route.model_name.startswith("dall-e-3"):
            if request.parameters.get("n", 1) != 1:
                raise ValueError("legacy image model does not support requested n")
            if request.parameters.get("operation", "generate") == "edit":
                raise ValueError("legacy image model does not support editing")

    async def generate(
        self,
        request: ImageRequest,
        *,
        source_image: bytes | None = None,
        source_content_type: str = "image/png",
        max_bytes: int,
    ) -> ImageResult:
        self.validate_request(request)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("image max_bytes must be positive")
        editing = request.parameters.get("operation", "generate") == "edit"
        if editing:
            if not isinstance(source_image, bytes) or not source_image:
                raise ValueError("image edit requires source image bytes")
            if len(source_image) > max_bytes:
                raise ValueError("source image exceeds byte limit")
            validate_image_bytes(source_image, source_content_type)
        elif source_image is not None:
            raise ValueError("source image is only supported for edit")
        output_format = request.parameters.get("format", "png")
        url, kwargs = self._submission(request, source_image=source_image, source_content_type=source_content_type)
        payload, request_id = await self._post(url, max_bytes=max_bytes, **kwargs)
        return self._result(
            payload,
            request_id=request_id,
            output_format=output_format,
            count=request.parameters.get("n", 1),
            max_bytes=max_bytes,
        )

    def _submission(self, request: ImageRequest, *, source_image: bytes | None, source_content_type: str) -> tuple:
        parameters = request.parameters
        payload = {"model": self.route.model_name, "prompt": request.prompt, "n": parameters.get("n", 1)}
        if "size" in parameters:
            payload["size"] = parameters["size"]
        output_format = parameters.get("format", "png")
        legacy = self.route.model_name.startswith("dall-e-")
        if legacy:
            payload["response_format"] = "b64_json"
        else:
            payload["output_format"] = output_format
        if parameters.get("operation", "generate") == "edit":
            extension = source_content_type.split("/")[1]
            return self._base + "/images/edits", {
                "data": {key: str(value) for key, value in payload.items()},
                "files": {"image": ("source." + extension, source_image, source_content_type)},
            }
        return self._base + "/images/generations", {"json": payload}

    async def _post(self, url: str, *, max_bytes: int, **kwargs) -> tuple[dict, str | None]:
        wire_limit = 4 * ((max_bytes + 2) // 3) + 65536
        try:
            async with self._client.stream(
                "POST",
                url,
                headers={"Authorization": "Bearer " + self.route.api_key.get_secret_value()},
                follow_redirects=False,
                **kwargs,
            ) as response:
                if not response.is_success:
                    error = (
                        SubmissionUnknown
                        if response.status_code >= 500 or response.status_code == 408
                        else VideoProviderError
                    )
                    raise error(f"image provider returned HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        oversized = int(declared) > wire_limit
                    except ValueError:
                        raise SubmissionUnknown("image provider returned invalid response length") from None
                    if oversized:
                        raise SubmissionUnknown("image provider response exceeds byte limit")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=min(wire_limit + 1, 65536)):
                    if len(data) + len(chunk) > wire_limit:
                        raise SubmissionUnknown("image provider response exceeds byte limit")
                    data.extend(chunk)
                try:
                    payload = json.loads(data)
                except ValueError, UnicodeError, RecursionError:
                    raise SubmissionUnknown("image provider returned invalid JSON") from None
                if not isinstance(payload, dict):
                    raise SubmissionUnknown("image provider returned invalid payload")
                request_id = response.headers.get("x-request-id") or payload.get("request_id")
                if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", request_id):
                    request_id = None
                secret = self.route.api_key.get_secret_value()
                if request_id is not None and secret and secret in request_id:
                    request_id = None
                return payload, request_id
        except httpx.HTTPError:
            raise SubmissionUnknown("image provider transport failed") from None

    def _result(
        self, payload: dict, *, request_id: str | None, output_format: str, count: int, max_bytes: int
    ) -> ImageResult:
        items = payload.get("data")
        if not isinstance(items, list) or not self._valid_image_count(len(items), count):
            raise SubmissionUnknown("image provider returned an unexpected image count")
        images = []
        total = 0
        for item in items:
            if not isinstance(item, dict):
                raise SubmissionUnknown("image provider returned invalid image output")
            encoded, url = item.get("b64_json"), item.get("url")
            if (encoded is None) == (url is None):
                raise SubmissionUnknown("image provider returned invalid image content")
            data = None
            if encoded is not None:
                if not isinstance(encoded, str) or not encoded or len(encoded) > 4 * ((max_bytes - total + 2) // 3):
                    raise SubmissionUnknown("image provider returned invalid or oversized image content")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except ValueError, binascii.Error:
                    raise SubmissionUnknown("image provider returned invalid base64 image") from None
                total += len(data)
                if total > max_bytes:
                    raise SubmissionUnknown("image provider output exceeds byte limit")
            try:
                if data is not None:
                    validate_image_bytes(data, _FORMATS[output_format])
                images.append(
                    ImageOutput(data=data, url=url, content_type=_FORMATS[output_format], extension=output_format)
                )
            except ValueError:
                raise SubmissionUnknown("image provider returned invalid image output") from None
        usage = _safe_usage(payload.get("usage", {}))
        return ImageResult(images=images, usage=usage, request_id=request_id)

    def _valid_image_count(self, returned: int, requested: int) -> bool:
        return returned == requested

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class VolcengineImageProvider(OpenAIImageProvider):
    def __init__(self, route: ImageRoute, *, client: httpx.AsyncClient | None = None):
        super().__init__(route, client=client)
        base = route.base_url.rstrip("/")
        if not base.endswith("/api/v3"):
            base += "/api/v3"
        self._base = base

    def validate_request(self, request: ImageRequest) -> None:
        self.route.validate_request(request)

    def _valid_image_count(self, returned: int, requested: int) -> bool:
        # Ark's max_images limits a sequence; the model decides its actual count.
        return 1 <= returned <= requested

    def _submission(self, request: ImageRequest, *, source_image: bytes | None, source_content_type: str) -> tuple:
        count = request.parameters.get("n", 1)
        payload = {
            "model": self.route.model_name,
            "prompt": request.prompt,
            "response_format": "b64_json",
            "output_format": request.parameters.get("format", "png"),
            "sequential_image_generation": "auto" if count > 1 else "disabled",
        }
        if "size" in request.parameters:
            payload["size"] = request.parameters["size"]
        if count > 1:
            payload["sequential_image_generation_options"] = {"max_images": count}
        if source_image is not None:
            payload["image"] = (
                "data:" + source_content_type + ";base64," + base64.b64encode(source_image).decode("ascii")
            )
        return self._base + "/images/generations", {"json": payload}


_IMAGE_PROVIDERS: dict[str, Callable[..., ImageProvider]] = {
    "openai": OpenAIImageProvider,
    "openai_compatible": OpenAIImageProvider,
    "openai-compatible": OpenAIImageProvider,
    "volcengine": VolcengineImageProvider,
}


def image_provider_factory(provider: str) -> Callable[..., ImageProvider]:
    try:
        return _IMAGE_PROVIDERS[provider]
    except KeyError:
        raise ValueError("image provider protocol is not implemented") from None


def create_image_provider(route: ImageRoute, *, client: httpx.AsyncClient | None = None) -> ImageProvider:
    return image_provider_factory(route.provider)(route, client=client)
