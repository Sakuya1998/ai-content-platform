import pytest
from pydantic import ValidationError

from agent_runtime.domain.media import VideoCapabilities, VideoRequest, VideoRoute


def route(**capabilities):
    return VideoRoute(
        provider="volcengine",
        model_name="seedance-version",
        base_url="https://ark.example/api/v3",
        api_key="private-key",
        profile_version_id="frozen-1",
        video_capabilities=VideoCapabilities(
            text_to_video=True, durations=[5, 10], ratios=["16:9"], resolutions=["720p"], **capabilities
        ),
    )


def test_frozen_video_capabilities_validate_request_and_do_not_persist_credentials():
    configured = route()
    configured.validate_request(VideoRequest(prompt="开场", parameters={"duration": 5, "ratio": "16:9"}))
    assert configured.identity()["profile_version_id"] == "frozen-1"
    assert "private-key" not in str(configured.identity())
    assert "private-key" not in repr(configured)
    with pytest.raises(ValueError, match="duration"):
        configured.validate_request(VideoRequest(prompt="开场", parameters={"duration": 7}))
    with pytest.raises(ValueError, match="generate_audio"):
        configured.validate_request(VideoRequest(prompt="开场", parameters={"generate_audio": True}))
    with pytest.raises(ValueError, match="image_to_video"):
        configured.validate_request(VideoRequest(prompt="开场", parameters={"image_url": "https://img.example/a"}))


@pytest.mark.parametrize(
    "parameters",
    [
        {"duration": True},
        {"duration": 0},
        {"seed": -2},
        {"seed": 2**32},
        {"generate_audio": "true"},
        {"image_url": "file:///etc/passwd"},
        {"unknown": 1},
        {"ratio": 16},
    ],
)
def test_video_request_rejects_untyped_or_unsupported_parameters(parameters):
    with pytest.raises(ValidationError):
        VideoRequest(prompt="开场", parameters=parameters)


def test_video_request_rejects_blank_prompt():
    with pytest.raises(ValidationError):
        VideoRequest(prompt="  ")


def test_video_route_rejects_missing_capabilities():
    with pytest.raises(ValidationError):
        VideoRoute(provider="volcengine", model_name="seedance-version", api_key="key", base_url="https://ark.example")
