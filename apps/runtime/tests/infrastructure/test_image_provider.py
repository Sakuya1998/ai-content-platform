import base64
import json

import httpx
import pytest

from agent_runtime.domain.images import ImageCapabilities, ImageRequest, ImageRoute
from agent_runtime.infrastructure.media.errors import SubmissionUnknown, VideoProviderError
from agent_runtime.infrastructure.media.images import create_image_provider, image_provider_factory

PNG_IMAGE = b"\x89PNG\r\n\x1a\nfixture"
JPEG_IMAGE = b"\xff\xd8\xff\xe0fixture"
WEBP_IMAGE = b"RIFF\x04\x00\x00\x00WEBP"


def route(provider="openai", model_name="gpt-image-configured"):
    return ImageRoute(
        provider=provider,
        model_name=model_name,
        base_url="https://images.example/api/v3/" if provider == "volcengine" else "https://images.example/v1/",
        api_key="private-key",
        profile_version_id="frozen-1",
        image_capabilities=ImageCapabilities(
            generation=True,
            editing=True,
            sizes=["1024x1024", "2K"],
            formats=["png", "jpeg", "webp"],
            max_images=4,
        ),
    )


def body(data=PNG_IMAGE, **extra):
    return {"data": [{"b64_json": base64.b64encode(data).decode()}], **extra}


async def test_openai_generation_uses_modern_parameters_and_decodes_bounded_images():
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(
            200,
            json=body(JPEG_IMAGE, usage={"total_tokens": 12, "input_tokens_details": {"text_tokens": 4}}),
            headers={"x-request-id": "req-123"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await create_image_provider(route(), client=client).generate(
            ImageRequest(prompt="封面", parameters={"size": "1024x1024", "format": "jpeg"}),
            max_bytes=100,
        )
    assert calls[0].url.path == "/v1/images/generations"
    assert calls[0].headers["authorization"] == "Bearer private-key"
    assert json.loads(calls[0].content) == {
        "model": "gpt-image-configured",
        "prompt": "封面",
        "n": 1,
        "size": "1024x1024",
        "output_format": "jpeg",
    }
    assert result.images[0].data == JPEG_IMAGE
    assert result.images[0].content_type == "image/jpeg"
    assert result.images[0].extension == "jpeg"
    assert result.usage == {"total_tokens": 12, "input_tokens_details": {"text_tokens": 4}}
    assert result.request_id == "req-123"


async def test_openai_edit_uploads_source_image_as_multipart():
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await create_image_provider(route(), client=client).generate(
            ImageRequest(prompt="编辑", parameters={"operation": "edit"}),
            source_image=WEBP_IMAGE,
            source_content_type="image/webp",
            max_bytes=100,
        )
    assert calls[0].url.path == "/v1/images/edits"
    assert calls[0].headers["content-type"].startswith("multipart/form-data;")
    assert b'filename="source.webp"' in calls[0].content
    assert b"Content-Type: image/webp" in calls[0].content
    assert WEBP_IMAGE in calls[0].content
    assert b"response_format" not in calls[0].content


async def test_legacy_openai_returns_url_and_rejects_unsupported_output_format_before_post():
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image.png?token=signed"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = create_image_provider(route(model_name="dall-e-3"), client=client)
        result = await provider.generate(ImageRequest(prompt="cover"), max_bytes=100)
        with pytest.raises(ValueError, match="format"):
            await provider.generate(ImageRequest(prompt="cover", parameters={"format": "jpeg"}), max_bytes=100)
    assert len(calls) == 1
    assert "output_format" not in json.loads(calls[0].content)
    assert json.loads(calls[0].content)["response_format"] == "b64_json"
    assert result.images[0].url == "https://cdn.example/image.png?token=signed"
    assert result.images[0].data is None
    assert "token=signed" not in repr(result)


@pytest.mark.parametrize("operation,n", [("generate", 1), ("edit", 1), ("generate", 2)])
async def test_volcengine_uses_generations_and_data_url_for_edit(operation, n):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"data": body()["data"] * n})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await create_image_provider(route("volcengine", "seedream-configured"), client=client).generate(
            ImageRequest(prompt="cover", parameters={"operation": operation, "size": "2K", "n": n}),
            source_image=PNG_IMAGE if operation == "edit" else None,
            max_bytes=100,
        )
    payload = json.loads(calls[0].content)
    assert calls[0].url.path == "/api/v3/images/generations"
    assert payload["model"] == "seedream-configured"
    assert payload["size"] == "2K"
    assert payload["response_format"] == "b64_json"
    assert "n" not in payload
    assert payload["sequential_image_generation"] == ("auto" if n > 1 else "disabled")
    if n > 1:
        assert payload["sequential_image_generation_options"] == {"max_images": 2}
    if operation == "edit":
        assert payload["image"] == "data:image/png;base64,iVBORw0KGgpmaXh0dXJl"
    else:
        assert "image" not in payload
    assert len(result.images) == n


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"data": []},
        {"data": [{"b64_json": "%%%"}]},
        {"data": [{"b64_json": ""}]},
        {"data": [{"url": "file:///etc/passwd"}]},
        {"data": [{"b64_json": "YQ==", "url": "https://host/a"}]},
        {"data": [{"b64_json": "YQ=="}] * 2},
        {"data": [{"error": {"message": "secret"}}]},
        body(usage={"token": "private-key"}),
        body(usage={"total_tokens": 10**500}),
    ],
)
async def test_invalid_success_is_ambiguous_and_does_not_leak_provider_data(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
        with pytest.raises(SubmissionUnknown) as caught:
            await create_image_provider(route(), client=client).generate(ImageRequest(prompt="cover"), max_bytes=100)
    assert "private-key" not in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "status,error",
    [(400, VideoProviderError), (401, VideoProviderError), (408, SubmissionUnknown), (500, SubmissionUnknown)],
)
async def test_post_errors_are_classified_without_retry_or_sensitive_messages(status, error):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(status, text="private-key manuscript")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(error) as caught:
            await create_image_provider(route(), client=client).generate(ImageRequest(prompt="cover"), max_bytes=100)
    assert len(calls) == 1
    assert "private-key" not in str(caught.value)


async def test_timeout_is_ambiguous_and_does_not_retry():
    calls = []

    def transport(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-key", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(SubmissionUnknown) as caught:
            await create_image_provider(route(), client=client).generate(ImageRequest(prompt="cover"), max_bytes=100)
    assert len(calls) == 1
    assert "private-key" not in str(caught.value)


async def test_image_limit_applies_before_decode_and_across_all_images():
    for payload, parameters in [
        (body(PNG_IMAGE + b"a" * 100), {}),
        ({"data": body(PNG_IMAGE + b"a" * 45)["data"] * 2}, {"n": 2}),
    ]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r, payload=payload: httpx.Response(200, json=payload))
        ) as client:
            with pytest.raises(SubmissionUnknown):
                await create_image_provider(route(), client=client).generate(
                    ImageRequest(prompt="cover", parameters=parameters),
                    max_bytes=100,
                )


async def test_validation_happens_before_http_and_borrowed_client_is_not_closed():
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        provider = create_image_provider(route(), client=client)
        for parameters, source, content_type in [
            ({"operation": "edit"}, None, "image/png"),
            ({}, b"source", "image/png"),
            ({"operation": "edit"}, b"source", "text/plain"),
            ({"size": "unsupported"}, None, "image/png"),
        ]:
            with pytest.raises(ValueError):
                await provider.generate(
                    ImageRequest(prompt="cover", parameters=parameters),
                    source_image=source,
                    source_content_type=content_type,
                    max_bytes=100,
                )
        await provider.aclose()
        assert not client.is_closed
    assert not calls


def test_image_registry_rejects_unimplemented_protocol():
    with pytest.raises(ValueError, match="provider"):
        image_provider_factory("unsupported")


@pytest.mark.parametrize("provider_name", ["openai_compatible", "openai-compatible"])
async def test_image_registry_accepts_existing_openai_compatible_profile_aliases(provider_name):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        configured = create_image_provider(route().model_copy(update={"provider": provider_name}), client=client)
        result = await configured.generate(ImageRequest(prompt="cover"), max_bytes=100)
    assert calls[0].url.path == "/v1/images/generations"
    assert json.loads(calls[0].content)["model"] == "gpt-image-configured"
    assert result.images[0].data == PNG_IMAGE


@pytest.mark.parametrize(
    "provider,count,accepted",
    [
        ("volcengine", 0, False),
        ("volcengine", 1, True),
        ("volcengine", 2, True),
        ("volcengine", 3, True),
        ("volcengine", 4, False),
        ("openai", 0, False),
        ("openai", 1, False),
        ("openai", 2, False),
        ("openai", 3, True),
        ("openai", 4, False),
    ],
)
async def test_image_count_is_seedream_maximum_and_openai_exact(provider, count, accepted):
    payload = {"data": body()["data"] * count, "usage": {"generated_images": count}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
        configured = create_image_provider(route(provider), client=client)
        if accepted:
            result = await configured.generate(ImageRequest(prompt="cover", parameters={"n": 3}), max_bytes=100)
            assert len(result.images) == count
            assert result.usage["generated_images"] == count
        else:
            with pytest.raises(SubmissionUnknown, match="count"):
                await configured.generate(ImageRequest(prompt="cover", parameters={"n": 3}), max_bytes=100)


async def test_provider_validates_protocol_specific_request_before_submission():
    async with httpx.AsyncClient() as client:
        configured = create_image_provider(route(model_name="dall-e-3"), client=client)
        configured.validate_request(ImageRequest(prompt="cover"))
        for parameters in [{"format": "jpeg"}, {"n": 2}, {"operation": "edit"}]:
            with pytest.raises(ValueError):
                configured.validate_request(ImageRequest(prompt="cover", parameters=parameters))
        create_image_provider(route("volcengine", "dall-e-3"), client=client).validate_request(
            ImageRequest(prompt="cover", parameters={"format": "jpeg", "n": 2, "operation": "edit"})
        )


@pytest.mark.parametrize("response_body", [b"not-json", b"[" * 2000 + b"]" * 2000])
async def test_invalid_json_is_ambiguous(response_body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=response_body))
    ) as client:
        with pytest.raises(SubmissionUnknown):
            await create_image_provider(route(), client=client).generate(ImageRequest(prompt="cover"), max_bytes=100)


async def test_request_id_does_not_echo_route_credential():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=body(), headers={"x-request-id": "private-key"})
        )
    ) as client:
        result = await create_image_provider(route(), client=client).generate(
            ImageRequest(prompt="cover"), max_bytes=100
        )
    assert result.request_id is None


async def test_stream_response_is_stopped_when_byte_limit_is_exceeded():
    chunks_read = []

    class LargeStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(100):
                chunks_read.append(index)
                yield b"a" * 32768

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=LargeStream()))
    ) as client:
        with pytest.raises(SubmissionUnknown, match="byte limit"):
            await create_image_provider(route(), client=client).generate(ImageRequest(prompt="cover"), max_bytes=100)
    assert len(chunks_read) <= 4


@pytest.mark.parametrize("provider", ["openai", "volcengine"])
@pytest.mark.parametrize(
    "image,output_format",
    [
        (b"<html>private manuscript</html>", "png"),
        (JPEG_IMAGE, "png"),
        (PNG_IMAGE, "jpeg"),
        (b"RIFF\x04\x00\x00\x00WAVE", "webp"),
        (b"\x89PNG\r\n\x1a", "png"),
    ],
)
async def test_wrong_format_success_is_ambiguous_without_exposing_binary_content(provider, image, output_format):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body(image)))
    ) as client:
        with pytest.raises(SubmissionUnknown) as caught:
            await create_image_provider(route(provider), client=client).generate(
                ImageRequest(prompt="cover", parameters={"format": output_format}),
                max_bytes=100,
            )
    assert "private manuscript" not in str(caught.value)


@pytest.mark.parametrize("provider", ["openai", "volcengine"])
async def test_invalid_edit_source_is_rejected_before_http(provider):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError):
            await create_image_provider(route(provider), client=client).generate(
                ImageRequest(prompt="edit", parameters={"operation": "edit"}),
                source_image=b"<html>private manuscript</html>",
                max_bytes=100,
            )
    assert not calls


@pytest.mark.parametrize("data,format", [(PNG_IMAGE, "png"), (JPEG_IMAGE, "jpeg"), (WEBP_IMAGE, "webp")])
async def test_image_signatures_matching_requested_format_are_accepted(data, format):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body(data)))
    ) as client:
        result = await create_image_provider(route(), client=client).generate(
            ImageRequest(prompt="cover", parameters={"format": format}),
            max_bytes=100,
        )
    assert result.images[0].data == data
