import json

import httpx
import pytest

from agent_runtime.domain.audio_generation import (
    MusicCapabilities,
    MusicRequest,
    MusicRoute,
    SoundEffectCapabilities,
    SoundEffectRequest,
    SoundEffectRoute,
)
from agent_runtime.infrastructure.media.audio_generation import (
    create_music_provider,
    create_sound_effect_provider,
)
from agent_runtime.infrastructure.media.errors import SubmissionUnknown, VideoProviderError


def music_route(**changes):
    return MusicRoute(
        provider="elevenlabs",
        model_name="music_v2_5",
        base_url="https://api.elevenlabs.io/v1",
        api_key="private-key",
        profile_version_id="frozen-music-1",
        music_capabilities=MusicCapabilities(
            output_formats=["mp3_44100_128", "mp3_48000_192"],
            min_duration_ms=3000,
            max_duration_ms=600000,
            instrumental=True,
        ),
        **changes,
    )


def effect_route(**changes):
    return SoundEffectRoute(
        provider="elevenlabs",
        model_name="eleven_text_to_sound_v2",
        base_url="https://api.elevenlabs.io/v1",
        api_key="private-key",
        profile_version_id="frozen-effect-1",
        sound_effect_capabilities=SoundEffectCapabilities(
            output_formats=["mp3_22050_32", "mp3_44100_128"],
            min_duration_seconds=0.5,
            max_duration_seconds=30,
            prompt_influence=True,
            loop=True,
        ),
        **changes,
    )


async def test_music_request_uses_configured_model_and_bounded_binary_response():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            content=b"ID3generated-music",
            headers={"content-type": "audio/mpeg", "request-id": "music-req-1"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await create_music_provider(music_route(), client=client).compose(
            MusicRequest(
                prompt="Warm cinematic instrumental for a hopeful ending",
                parameters={"music_length_ms": 12000, "force_instrumental": True},
            ),
            max_bytes=128,
        )

    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128"
    assert request.headers["xi-api-key"] == "private-key"
    assert json.loads(request.content) == {
        "model_id": "music_v2_5",
        "prompt": "Warm cinematic instrumental for a hopeful ending",
        "music_length_ms": 12000,
        "force_instrumental": True,
    }
    assert result.data == b"ID3generated-music"
    assert result.extension == "mp3"
    assert result.content_type == "audio/mpeg"
    assert result.request_id == "music-req-1"


async def test_sound_effect_request_uses_its_own_protocol_and_parameters():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, content=b"ID3effect", headers={"content-type": "audio/mpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await create_sound_effect_provider(effect_route(), client=client).generate(
            SoundEffectRequest(
                text="Footsteps on wet pavement, passing from left to right",
                parameters={"duration_seconds": 2.5, "prompt_influence": 0.8, "loop": False},
            ),
            max_bytes=128,
        )

    request = requests[0]
    assert str(request.url) == "https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_22050_32"
    assert request.headers["xi-api-key"] == "private-key"
    assert json.loads(request.content) == {
        "model_id": "eleven_text_to_sound_v2",
        "text": "Footsteps on wet pavement, passing from left to right",
        "duration_seconds": 2.5,
        "prompt_influence": 0.8,
        "loop": False,
    }
    assert result.data == b"ID3effect"
    assert result.request_id is None


@pytest.mark.parametrize(
    "factory,route,make_request",
    [
        (create_music_provider, music_route, lambda: MusicRequest(prompt="A quiet piano cue")),
        (create_sound_effect_provider, effect_route, lambda: SoundEffectRequest(text="A door closes")),
    ],
)
@pytest.mark.parametrize(
    "status,error",
    [
        (400, VideoProviderError),
        (401, VideoProviderError),
        (429, VideoProviderError),
        (408, SubmissionUnknown),
        (500, SubmissionUnknown),
    ],
)
async def test_audio_generation_http_failures_are_sanitized_and_never_retried(
    factory, route, make_request, status, error
):
    calls = []

    def transport(outgoing):
        calls.append(outgoing)
        return httpx.Response(status, json={"detail": "private-key private manuscript"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = factory(route(), client=client)
        operation = provider.compose if factory is create_music_provider else provider.generate
        with pytest.raises(error) as caught:
            await operation(make_request(), max_bytes=128)

    assert len(calls) == 1
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "factory,route,make_request",
    [
        (create_music_provider, music_route, lambda: MusicRequest(prompt="A quiet piano cue")),
        (create_sound_effect_provider, effect_route, lambda: SoundEffectRequest(text="A door closes")),
    ],
)
async def test_audio_generation_rejects_corrupt_or_oversized_results(factory, route, make_request):
    async def run(body, headers, limit=128):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, content=body, headers=headers))
        ) as client:
            provider = factory(route(), client=client)
            operation = provider.compose if factory is create_music_provider else provider.generate
            return await operation(make_request(), max_bytes=limit)

    with pytest.raises(SubmissionUnknown):
        await run(b"not audio", {"content-type": "audio/mpeg"})
    with pytest.raises(SubmissionUnknown, match="byte limit"):
        await run(b"ID3" + b"x" * 200, {"content-type": "audio/mpeg"}, limit=32)


def test_audio_generation_requests_enforce_provider_duration_ranges():
    with pytest.raises(ValueError):
        MusicRequest(prompt="cue", parameters={"music_length_ms": 2000})
    with pytest.raises(ValueError):
        SoundEffectRequest(text="impact", parameters={"duration_seconds": 31})
