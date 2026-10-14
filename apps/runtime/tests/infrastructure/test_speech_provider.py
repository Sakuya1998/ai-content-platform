import base64
import json
from uuid import UUID

import httpx
import pytest

from agent_runtime.domain.speech import SpeechCapabilities, SpeechRequest, SpeechRoute
from agent_runtime.infrastructure.media.errors import SubmissionUnknown, VideoProviderError
from agent_runtime.infrastructure.media.speech import create_speech_provider, speech_provider_factory


def route(**changes):
    return SpeechRoute(
        provider="openai",
        model_name="configured-tts",
        base_url="https://speech.example/v1/",
        api_key="private-key",
        profile_version_id="frozen-1",
        speech_capabilities=SpeechCapabilities(
            voices=["nova"],
            formats=["mp3", "wav", "opus", "aac", "flac", "pcm"],
            languages=["zh"],
            instructions=True,
        ),
        **changes,
    )


async def test_speech_payload_omits_metadata_and_unsupported_language():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, content=b"ID3audio", headers={"content-type": "audio/mpeg", "x-request-id": "req-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = create_speech_provider(route(), client=client)
        result = await provider.synthesize(
            SpeechRequest(
                text="旁白",
                parameters={
                    "format": "mp3",
                    "speed": 1.5,
                    "language": "zh",
                    "instructions": "轻声",
                    "character_id": "character-1",
                    "scene_id": "scene-1",
                },
            ),
            max_bytes=100,
        )
        await provider.aclose()
        assert not client.is_closed
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://speech.example/v1/audio/speech"
    assert requests[0].headers["authorization"] == "Bearer private-key"
    assert json.loads(requests[0].content) == {
        "model": "configured-tts",
        "input": "旁白",
        "voice": "nova",
        "response_format": "mp3",
        "speed": 1.5,
        "instructions": "轻声",
    }
    assert result.data == b"ID3audio"
    assert result.content_type == "audio/mpeg"
    assert result.extension == "mp3"
    assert result.request_id == "req-1"
    assert result.usage == {}


@pytest.mark.parametrize(
    "status,error",
    [
        (400, VideoProviderError),
        (401, VideoProviderError),
        (429, VideoProviderError),
        (302, VideoProviderError),
        (408, SubmissionUnknown),
        (500, SubmissionUnknown),
    ],
)
async def test_speech_http_failure_is_safe_and_never_retried(status, error):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "private-key private manuscript https://secret.example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(error) as caught:
            await create_speech_provider(route(), client=client).synthesize(SpeechRequest(text="旁白"), max_bytes=100)
    assert len(calls) == 1
    assert "private" not in str(caught.value)
    assert "https://" not in str(caught.value)


async def test_speech_timeout_is_unknown_without_retry_or_private_error_details():
    calls = []

    def transport(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-key private manuscript", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(SubmissionUnknown) as caught:
            await create_speech_provider(route(), client=client).synthesize(SpeechRequest(text="旁白"), max_bytes=100)
    assert len(calls) == 1
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"", {"content-type": "audio/mpeg"}),
        (b'{"error":"private"}', {"content-type": "application/json"}),
        (b"private manuscript", {"content-type": "audio/mpeg"}),
        (b"ID3audio", {"content-type": "audio/wav"}),
        (b"ID3audio", {"content-type": "audio/mpeg", "content-length": "100"}),
    ],
)
async def test_speech_corrupt_success_is_unknown(body, headers):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body, headers=headers))
    ) as client:
        with pytest.raises(SubmissionUnknown):
            await create_speech_provider(route(), client=client).synthesize(SpeechRequest(text="旁白"), max_bytes=200)


class AudioStream(httpx.AsyncByteStream):
    def __init__(self, *, fail=False):
        self.read = 0
        self.closed = False
        self.fail = fail

    async def __aiter__(self):
        self.read += 1
        yield b"ID3abc"
        if self.fail:
            raise httpx.ReadError("private content")
        self.read += 1
        yield b"x" * 20
        self.read += 1
        yield b"not reached"

    async def aclose(self):
        self.closed = True


async def test_speech_stream_limit_stops_download_and_closes_response():
    stream = AudioStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=stream, headers={"content-type": "audio/mpeg"})
        )
    ) as client:
        with pytest.raises(SubmissionUnknown, match="limit"):
            await create_speech_provider(route(), client=client).synthesize(SpeechRequest(text="旁白"), max_bytes=10)
    assert stream.read == 2
    assert stream.closed


async def test_speech_stream_failure_is_unknown_and_closes_response():
    stream = AudioStream(fail=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=stream, headers={"content-type": "audio/mpeg"})
        )
    ) as client:
        with pytest.raises(SubmissionUnknown) as caught:
            await create_speech_provider(route(), client=client).synthesize(SpeechRequest(text="旁白"), max_bytes=100)
    assert stream.closed
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "format,body,mime",
    [
        ("wav", b"RIFF\x10\x00\x00\x00WAVEdata", "audio/wav"),
        ("opus", b"OggSOpusHead", "audio/ogg"),
        ("aac", b"\xff\xf1abc", "audio/aac"),
        ("flac", b"fLaCdata", "audio/flac"),
        ("pcm", b"\x01\x00\x02\x00", "audio/pcm"),
    ],
)
async def test_speech_audio_formats_preserve_binary_and_extension(format, body, mime):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})
        )
    ) as client:
        result = await create_speech_provider(route(), client=client).synthesize(
            SpeechRequest(text="旁白", parameters={"format": format}), max_bytes=100
        )
    assert result.data == body
    assert result.extension == format
    assert result.content_type == mime


async def test_speech_checks_capabilities_and_size_before_http():
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        for request, max_bytes in [
            (SpeechRequest(text="旁白", parameters={"voice": "unknown"}), 100),
            (SpeechRequest(text="旁白"), 0),
            (SpeechRequest(text="旁白"), True),
        ]:
            with pytest.raises(ValueError):
                await create_speech_provider(route(), client=client).synthesize(request, max_bytes=max_bytes)
    assert not calls


def test_speech_registry_is_separate_and_rejects_unimplemented_protocol():
    assert speech_provider_factory("openai") is speech_provider_factory("openai_compatible")
    with pytest.raises(ValueError, match="provider"):
        speech_provider_factory("volcengine")


def doubao_route(**changes):
    values = {
        "provider": "volcengine_tts",
        "model_name": "seed-tts-2.0",
        "base_url": "https://speech.example",
        "api_key": json.dumps({"app_id": "app-1", "access_token": "private-token"}),
        "speech_capabilities": SpeechCapabilities(
            voices=["zh_female_vv_uranus_bigtts"],
            formats=["mp3", "opus", "pcm"],
            languages=["zh"],
            min_speed=0.5,
            max_speed=2,
            instructions=True,
        ),
        **changes,
    }
    return SpeechRoute(**values)


def ndjson(*events):
    return b"\n".join(json.dumps(event).encode() for event in events) + b"\n"


def audio_event(data=b"ID3audio"):
    return {"code": 0, "message": "", "data": base64.b64encode(data).decode()}


async def test_doubao_uses_official_v3_headers_payload_and_terminal_usage():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            content=ndjson(
                audio_event(b"ID3"),
                {"code": 0, "data": None, "sentence": {"text": "private manuscript"}},
                audio_event(b"audio"),
                {"code": 20000000, "data": None, "usage": {"text_words": 2}},
            ),
            headers={"x-tt-logid": "log-1"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await create_speech_provider(doubao_route(), client=client).synthesize(
            SpeechRequest(
                text="旁白",
                parameters={
                    "speed": 1.5,
                    "language": "zh",
                    "instructions": "轻声",
                    "character_id": "character-1",
                    "scene_id": "scene-1",
                },
            ),
            max_bytes=100,
        )
    assert len(requests) == 1
    assert str(requests[0].url) == "https://speech.example/api/v3/tts/unidirectional"
    assert requests[0].headers["x-api-app-id"] == "app-1"
    assert requests[0].headers["x-api-access-key"] == "private-token"
    assert requests[0].headers["x-api-resource-id"] == "seed-tts-2.0"
    assert requests[0].headers["x-control-require-usage-tokens-return"] == "text_words"
    assert UUID(requests[0].headers["x-api-request-id"])
    assert "authorization" not in requests[0].headers
    payload = json.loads(requests[0].content)
    assert payload["req_params"] == {
        "text": "旁白",
        "speaker": "zh_female_vv_uranus_bigtts",
        "audio_params": {"format": "mp3", "sample_rate": 24000, "speech_rate": 50},
        "additions": json.dumps({"explicit_language": "zh", "context_texts": ["轻声"]}, ensure_ascii=False),
    }
    assert result.data == b"ID3audio"
    assert result.usage == {"text_words": 2}
    assert result.request_id == "log-1"


async def test_doubao_new_console_uses_simple_api_key_auth():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, content=ndjson(audio_event(), {"code": 20000000, "data": None}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await create_speech_provider(doubao_route(api_key="private-key"), client=client).synthesize(
            SpeechRequest(text="旁白"), max_bytes=100
        )
    assert requests[0].headers["x-api-key"] == "private-key"
    assert "x-api-app-id" not in requests[0].headers
    assert "x-api-access-key" not in requests[0].headers


@pytest.mark.parametrize(
    "body",
    [
        ndjson(audio_event()),
        ndjson({"code": 20000000, "data": None}),
        ndjson({"code": 0, "data": "%%%"}, {"code": 20000000, "data": None}),
        ndjson({"code": 0, "data": 1}),
        ndjson({"code": 0}),
        ndjson(audio_event(), {"code": 20000000, "data": "invalid"}),
        ndjson(audio_event(), {"code": 20000000, "data": None}, audio_event()),
        ndjson(audio_event(), {"code": 20000000, "data": None, "usage": {"text_words": "private"}}),
        b"{invalid json}\n",
        ndjson(["invalid"]),
        ndjson({"code": "0", "data": "SUQz"}),
        ndjson(audio_event(b"private manuscript"), {"code": 20000000, "data": None}),
    ],
)
async def test_doubao_incomplete_or_corrupt_stream_remains_unknown(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))) as client:
        with pytest.raises(SubmissionUnknown):
            await create_speech_provider(doubao_route(), client=client).synthesize(
                SpeechRequest(text="旁白"), max_bytes=100
            )


@pytest.mark.parametrize(
    "code,error",
    [
        (40402003, VideoProviderError),
        (45000000, VideoProviderError),
        (55000000, SubmissionUnknown),
        (999, SubmissionUnknown),
    ],
)
async def test_doubao_explicit_error_codes_are_sanitized(code, error):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, content=ndjson({"code": code, "message": "private-token private manuscript"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(error) as caught:
            await create_speech_provider(doubao_route(), client=client).synthesize(
                SpeechRequest(text="旁白"), max_bytes=100
            )
    assert len(calls) == 1
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"api_key": '{"app_id":"app-1"}'},
        {"api_key": '{"app_id":1,"access_token":"secret"}'},
        {"api_key": '{"app_id":"app-1","access_token":"bad\\r\\nheader"}'},
        {"speech_capabilities": SpeechCapabilities(voices=["voice"], formats=["wav"], min_speed=0.5, max_speed=2)},
        {"speech_capabilities": SpeechCapabilities(voices=["voice"], formats=["mp3"])},
    ],
)
def test_doubao_rejects_invalid_configuration_when_constructed(changes):
    with pytest.raises(ValueError):
        create_speech_provider(doubao_route(**changes))


@pytest.mark.parametrize(
    "format,body,expected",
    [
        ("opus", b"OggSOpusHead", "ogg_opus"),
        ("pcm", b"\x01\x00", "pcm"),
    ],
)
async def test_doubao_format_mapping(format, body, expected):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, content=ndjson(audio_event(body), {"code": 20000000, "data": None}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await create_speech_provider(doubao_route(), client=client).synthesize(
            SpeechRequest(text="旁白", parameters={"format": format}), max_bytes=100
        )
    assert json.loads(requests[0].content)["req_params"]["audio_params"]["format"] == expected
    assert result.data == body
    assert result.extension == format


async def test_doubao_bounded_stream_closes_before_reading_more_audio():
    class Stream(httpx.AsyncByteStream):
        def __init__(self):
            self.read = 0
            self.closed = False

        async def __aiter__(self):
            self.read += 1
            yield ndjson(audio_event(b"ID3"))
            self.read += 1
            yield ndjson(audio_event(b"x" * 20))
            self.read += 1
            yield ndjson({"code": 20000000, "data": None})

        async def aclose(self):
            self.closed = True

    stream = Stream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(SubmissionUnknown, match="limit"):
            await create_speech_provider(doubao_route(), client=client).synthesize(
                SpeechRequest(text="旁白"), max_bytes=10
            )
    assert stream.closed
    assert stream.read == 2


async def test_doubao_bounds_non_audio_json_without_waiting_for_newline():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 100000))
    ) as client:
        with pytest.raises(SubmissionUnknown, match="limit"):
            await create_speech_provider(doubao_route(), client=client).synthesize(
                SpeechRequest(text="旁白"), max_bytes=10
            )


async def test_doubao_deterministic_error_after_audio_is_unknown():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=ndjson(audio_event(), {"code": 45000000, "message": "private"}))
        )
    ) as client:
        with pytest.raises(SubmissionUnknown):
            await create_speech_provider(doubao_route(), client=client).synthesize(
                SpeechRequest(text="旁白"), max_bytes=100
            )


async def test_doubao_reassembles_split_json_and_final_line_without_newline():
    body = ndjson(audio_event(), {"code": 20000000, "data": None}).rstrip(b"\n")

    class FragmentedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(body), 7):
                yield body[offset : offset + 7]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=FragmentedStream()))
    ) as client:
        result = await create_speech_provider(doubao_route(), client=client).synthesize(
            SpeechRequest(text="旁白"), max_bytes=100
        )
    assert result.data == b"ID3audio"


@pytest.mark.parametrize(
    "status,error", [(400, VideoProviderError), (408, SubmissionUnknown), (500, SubmissionUnknown)]
)
async def test_doubao_http_failures_do_not_retry(status, error):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "private-token https://secret.example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(error) as caught:
            await create_speech_provider(doubao_route(), client=client).synthesize(
                SpeechRequest(text="旁白"), max_bytes=100
            )
    assert len(calls) == 1
    assert "private" not in str(caught.value)
    assert "https://" not in str(caught.value)


async def test_speech_hyphenated_openai_compatible_alias_synthesizes_audio():
    configured = route().model_copy(update={"provider": "openai-compatible"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"ID3audio"))
    ) as client:
        result = await create_speech_provider(configured, client=client).synthesize(
            SpeechRequest(text="旁白"), max_bytes=100
        )
    assert result.data == b"ID3audio"


@pytest.mark.parametrize("identity", ["private-key", "req-private-key", "private-key-suffix"])
async def test_openai_speech_discards_request_id_containing_credentials(identity):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"ID3audio", headers={"x-request-id": identity})
        )
    ) as client:
        result = await create_speech_provider(route(), client=client).synthesize(
            SpeechRequest(text="旁白"), max_bytes=100
        )
    assert result.request_id is None
    assert "private-key" not in repr(result)


@pytest.mark.parametrize("identity", ["private-token", "req-private-token", "app-1"])
async def test_doubao_speech_discards_log_id_containing_legacy_credentials(identity):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=ndjson(audio_event(), {"code": 20000000, "data": None}), headers={"x-tt-logid": identity}
            )
        )
    ) as client:
        result = await create_speech_provider(doubao_route(), client=client).synthesize(
            SpeechRequest(text="旁白"), max_bytes=100
        )
    assert result.request_id is None
    assert "private-token" not in repr(result)


async def test_doubao_speech_discards_log_id_containing_simple_api_key():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                content=ndjson(audio_event(), {"code": 20000000, "data": None}),
                headers={"x-tt-logid": "req-private-key"},
            )
        )
    ) as client:
        result = await create_speech_provider(doubao_route(api_key="private-key"), client=client).synthesize(
            SpeechRequest(text="旁白"), max_bytes=100
        )
    assert result.request_id is None
