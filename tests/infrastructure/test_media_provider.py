import json

import httpx
import pytest

from agent_runtime.domain.media import VideoCapabilities, VideoRequest, VideoRoute
from agent_runtime.infrastructure.media.registry import create_video_provider
from agent_runtime.infrastructure.media.volcengine import VideoProviderError, VideoSubmissionUnknown


def route(**capabilities):
    return VideoRoute(
        provider="volcengine",
        model_name="seedance-version",
        base_url="https://ark.example/api/v3/",
        api_key="private-key",
        profile_version_id="frozen-1",
        video_capabilities=VideoCapabilities(
            text_to_video=True,
            image_to_video=True,
            durations=[5],
            ratios=["16:9"],
            resolutions=["720p"],
            generate_audio=True,
            **capabilities,
        ),
    )


async def test_ark_uses_tasks_protocol_and_maps_status_and_usage():
    requests = []

    def transport(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "remote-1"})
        return httpx.Response(
            200,
            json={
                "id": "remote-1",
                "model": "seedance-version",
                "status": "succeeded",
                "content": {"video_url": "https://media.example/result.mp4?token=signed"},
                "usage": {"completion_tokens": 120, "total_tokens": 120},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = create_video_provider(route(), client=client)
        remote = await provider.submit(
            VideoRequest(
                prompt="镜头缓缓推进",
                parameters={
                    "duration": 5,
                    "ratio": "16:9",
                    "resolution": "720p",
                    "generate_audio": False,
                    "watermark": False,
                    "camera_fixed": True,
                    "seed": -1,
                    "image_url": "https://image.example/frame.jpg",
                },
            )
        )
        assert remote == "remote-1"
        result = await provider.status(remote)
    assert str(requests[0].url) == "https://ark.example/api/v3/contents/generations/tasks"
    assert requests[0].headers["authorization"] == "Bearer private-key"
    assert "idempotency-key" not in requests[0].headers
    assert json.loads(requests[0].content) == {
        "model": "seedance-version",
        "content": [
            {"type": "text", "text": "镜头缓缓推进"},
            {"type": "image_url", "image_url": {"url": "https://image.example/frame.jpg"}, "role": "first_frame"},
        ],
        "duration": 5,
        "ratio": "16:9",
        "resolution": "720p",
        "generate_audio": False,
        "watermark": False,
        "camera_fixed": True,
        "seed": -1,
    }
    assert requests[1].url.path == "/api/v3/contents/generations/tasks/remote-1"
    assert result.state == "succeeded"
    assert result.content_url == "https://media.example/result.mp4?token=signed"
    assert result.usage["total_tokens"] == 120
    assert "token=signed" not in repr(result)


@pytest.mark.parametrize(
    "state, expected",
    [
        ("queued", "running"),
        ("running", "running"),
        ("cancelled", "cancelled"),
        ("expired", "failed"),
        ("failed", "failed"),
    ],
)
async def test_ark_maps_non_success_status(state, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "id": "remote",
                    "status": state,
                    "error": {"code": "ModelError", "message": "private manuscript"},
                },
            )
        )
    ) as client:
        result = await create_video_provider(route(), client=client).status("remote")
    assert result.state == expected
    assert result.error == {"code": "ModelError"}


async def test_ark_cancel_requires_capability_and_uses_delete():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError, match="cancellation"):
            await create_video_provider(route(), client=client).cancel("remote")
        await create_video_provider(route(cancellation=True), client=client).cancel("remote")
    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/api/v3/contents/generations/tasks/remote"


async def test_ark_timeout_or_missing_id_is_ambiguous_and_never_retried():
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-key", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(VideoSubmissionUnknown) as caught:
            await create_video_provider(route(), client=client).submit(VideoRequest(prompt="shot"))
        assert "private-key" not in str(caught.value)
    assert len(calls) == 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        with pytest.raises(VideoSubmissionUnknown):
            await create_video_provider(route(), client=client).submit(VideoRequest(prompt="shot"))


async def test_ark_http_error_is_sanitized_and_server_error_ambiguous():
    for code, error in [(400, VideoProviderError), (500, VideoSubmissionUnknown)]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r, code=code: httpx.Response(
                    code, json={"error": {"code": "BadRequest", "message": "private-key private manuscript"}}
                )
            )
        ) as client:
            with pytest.raises(error) as caught:
                await create_video_provider(route(), client=client).submit(VideoRequest(prompt="shot"))
            assert "private" not in str(caught.value)


def test_registry_rejects_unimplemented_provider():
    with pytest.raises(ValueError, match="provider"):
        create_video_provider(route().model_copy(update={"provider": "openai"}))


async def test_ark_rejects_status_from_another_frozen_model():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "id": "remote",
                    "model": "different-model",
                    "status": "running",
                },
            )
        )
    ) as client:
        with pytest.raises(VideoProviderError, match="model"):
            await create_video_provider(route(), client=client).status("remote")


async def test_ark_checks_model_capabilities_before_submitting_http():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "must-not-create"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError, match="duration"):
            await create_video_provider(route(), client=client).submit(
                VideoRequest(prompt="shot", parameters={"duration": 10})
            )
    assert not requests
