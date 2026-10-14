import pytest
from pydantic import ValidationError

from agent_runtime.domain.speech import SpeechCapabilities, SpeechRequest, SpeechResult, SpeechRoute


def route(**capabilities):
    configured = {
        "voices": ["alloy", "nova"],
        "formats": ["mp3", "wav"],
        "languages": ["zh", "en"],
        "min_speed": 0.5,
        "max_speed": 2.0,
        "max_input_chars": 20,
        "instructions": False,
        **capabilities,
    }
    return SpeechRoute(
        provider="openai",
        model_name="configured-tts",
        base_url="https://speech.example/v1/",
        api_key="private-key",
        profile_version_id="frozen-1",
        speech_capabilities=SpeechCapabilities(**configured),
    )


def test_speech_route_validates_frozen_capabilities_and_hides_secrets():
    configured = route()
    configured.validate_request(SpeechRequest(text="开场", parameters={"voice": "nova", "language": "zh"}))
    assert configured.identity()["profile_version_id"] == "frozen-1"
    assert "api_key" not in configured.identity()
    assert "private-key" not in repr(configured)
    assert "private manuscript" not in repr(SpeechRequest(text="private manuscript"))
    assert "binary secret" not in repr(SpeechResult(data=b"binary secret", content_type="audio/mpeg", extension="mp3"))


@pytest.mark.parametrize(
    "parameters,match",
    [
        ({"voice": "unknown"}, "voice"),
        ({"format": "flac"}, "format"),
        ({"language": "fr"}, "language"),
        ({"speed": 0.25}, "speed"),
        ({"speed": 4.0}, "speed"),
        ({"instructions": "whisper"}, "instructions"),
    ],
)
def test_speech_route_rejects_unsupported_capability(parameters, match):
    with pytest.raises(ValueError, match=match):
        route().validate_request(SpeechRequest(text="开场", parameters=parameters))


def test_speech_route_rejects_oversized_input():
    with pytest.raises(ValueError, match="input"):
        route().validate_request(SpeechRequest(text="a" * 21))


@pytest.mark.parametrize(
    "parameters",
    [
        {"text": "nested"},
        {"voice": 1},
        {"voice": " "},
        {"format": "ogg"},
        {"speed": True},
        {"speed": "1"},
        {"speed": float("nan")},
        {"speed": float("inf")},
        {"speed": 0.24},
        {"speed": 4.01},
        {"instructions": " "},
        {"language": False},
        {"character_id": 1},
        {"scene_id": " "},
        {"unknown": "private"},
    ],
)
def test_speech_request_rejects_untyped_or_unsupported_parameters(parameters):
    with pytest.raises(ValidationError):
        SpeechRequest(text="开场", parameters=parameters)


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_speech_request_rejects_blank_text(text):
    with pytest.raises(ValidationError):
        SpeechRequest(text=text)


@pytest.mark.parametrize(
    "capabilities",
    [
        {"min_speed": 2, "max_speed": 1},
        {"min_speed": 0.1},
        {"max_speed": 5},
        {"max_input_chars": 0},
        {"formats": ["ogg"]},
        {"voices": [""]},
        {"languages": [""]},
    ],
)
def test_speech_capabilities_reject_invalid_limits(capabilities):
    with pytest.raises(ValidationError):
        SpeechCapabilities(**capabilities)


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///etc/passwd",
        "https://secret:password@example.com",
        "https://example.com?key=private",
        "https://example.com/#fragment",
    ],
)
def test_speech_route_rejects_unsafe_base_url(base_url):
    with pytest.raises(ValidationError):
        SpeechRoute(**{**route().model_dump(), "base_url": base_url})


def test_speech_route_defaults_to_first_configured_voice():
    configured = route(voices=["nova"])
    configured.validate_request(SpeechRequest(text="开场"))


def test_speech_route_requires_declared_default_voice_and_format():
    for capabilities in ({"voices": []}, {"formats": ["wav"]}):
        with pytest.raises(ValueError):
            route(**capabilities).validate_request(SpeechRequest(text="开场"))
