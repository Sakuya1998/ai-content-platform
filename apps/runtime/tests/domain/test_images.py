import pytest
from pydantic import ValidationError

from agent_runtime.domain.images import ImageCapabilities, ImageOutput, ImageRequest, ImageRoute, validate_image_bytes


def route(**capabilities):
    return ImageRoute(
        provider="openai",
        model_name="gpt-image-configured",
        base_url="https://images.example/v1",
        api_key="private-key",
        profile_version_id="frozen-1",
        image_capabilities=ImageCapabilities(
            generation=True,
            editing=True,
            sizes=["1024x1024"],
            formats=["png", "jpeg"],
            max_images=2,
            **capabilities,
        ),
    )


def test_image_route_validates_frozen_capabilities_and_excludes_credentials():
    configured = route()
    configured.validate_request(ImageRequest(prompt="封面", parameters={"size": "1024x1024", "n": 2}))
    configured.validate_request(ImageRequest(prompt="编辑", parameters={"operation": "edit", "format": "jpeg"}))
    assert configured.identity()["profile_version_id"] == "frozen-1"
    assert "api_key" not in configured.identity()
    assert "private-key" not in repr(configured)
    for parameters in [{"size": "2048x2048"}, {"n": 3}, {"format": "webp"}]:
        with pytest.raises(ValueError):
            configured.validate_request(ImageRequest(prompt="封面", parameters=parameters))
    with pytest.raises(ValueError, match="editing"):
        configured.model_copy(update={"image_capabilities": ImageCapabilities(generation=True)}).validate_request(
            ImageRequest(prompt="编辑", parameters={"operation": "edit"})
        )


@pytest.mark.parametrize(
    "parameters",
    [
        {"n": True},
        {"n": 0},
        {"n": 5},
        {"n": "2"},
        {"operation": "unknown"},
        {"size": 1024},
        {"size": " "},
        {"format": "gif"},
        {"response_format": "url"},
        {"unknown": 1},
        {"operation": []},
        {"format": {}},
    ],
)
def test_image_request_rejects_invalid_parameters(parameters):
    with pytest.raises(ValidationError):
        ImageRequest(prompt="封面", parameters=parameters)


def test_image_request_rejects_blank_prompt_and_hides_manuscript():
    with pytest.raises(ValidationError):
        ImageRequest(prompt=" ")
    assert "private manuscript" not in repr(ImageRequest(prompt="private manuscript"))


@pytest.mark.parametrize("url", ["file:///tmp/x", "https://user:secret@host/v1", "https://host/v1?key=secret"])
def test_image_route_rejects_unsafe_base_url(url):
    with pytest.raises(ValidationError):
        ImageRoute.model_validate({**route().identity(), "base_url": url, "api_key": "key"})


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"data": b""},
        {"data": b"a", "url": "https://images.example/a"},
        {"url": "file:///tmp/x"},
        {"url": "https://user:password@images.example/a"},
    ],
)
def test_image_output_requires_one_valid_content_source(fields):
    with pytest.raises(ValidationError):
        ImageOutput(content_type="image/png", extension="png", **fields)


def test_image_output_repr_hides_data_and_signed_url():
    assert "signed-secret" not in repr(
        ImageOutput(
            url="https://images.example/a?token=signed-secret",
            content_type="image/png",
            extension="png",
        )
    )


@pytest.mark.parametrize(
    "data,content_type",
    [
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"RIFF\x04\x00\x00\x00WEBP", "image/webp"),
    ],
)
def test_image_byte_validator_accepts_matching_format_signatures(data, content_type):
    assert validate_image_bytes(data, content_type) is None


@pytest.mark.parametrize(
    "data,content_type",
    [
        (b"", "image/png"),
        (b"<html>private manuscript</html>", "image/png"),
        (b"\x89PNG\r\n\x1a", "image/png"),
        (b"\xff\xd8\xff", "image/png"),
        (b"RIFF\x04\x00\x00\x00WAVE", "image/webp"),
        (b"RIFFWEBP", "image/webp"),
        (b"\x89PNG\r\n\x1a\n", "image/gif"),
    ],
)
def test_image_byte_validator_rejects_invalid_or_mismatched_format_without_exposing_content(data, content_type):
    with pytest.raises(ValueError) as caught:
        validate_image_bytes(data, content_type)
    assert "private manuscript" not in str(caught.value)
