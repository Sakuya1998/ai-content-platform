"""Generate the MQ JSON Schemas from the Runtime's canonical Pydantic models."""

import json
from pathlib import Path

from agent_runtime.domain.assembly import AssemblyRequest
from agent_runtime.domain.audio_generation import (
    MusicCapabilities,
    MusicRequest,
    SoundEffectCapabilities,
    SoundEffectRequest,
)
from agent_runtime.domain.capabilities import CapabilitySnapshot
from agent_runtime.domain.execution_snapshot import ExecutionSnapshot
from agent_runtime.domain.images import ImageCapabilities, ImageRequest
from agent_runtime.domain.media import VideoCapabilities, VideoRequest
from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent
from agent_runtime.domain.speech import SpeechCapabilities, SpeechRequest
from agent_runtime.domain.storyboard import Storyboard
from agent_runtime.domain.timeline import Timeline


def main():
    root = Path(__file__).resolve().parents[1] / "contracts"
    for name, model in [
        ("command", RuntimeCommand),
        ("event", RuntimeEvent),
        ("capability-snapshot", CapabilitySnapshot),
        ("execution-snapshot", ExecutionSnapshot),
        ("video-request", VideoRequest),
        ("video-capabilities", VideoCapabilities),
        ("image-request", ImageRequest),
        ("image-capabilities", ImageCapabilities),
        ("speech-request", SpeechRequest),
        ("speech-capabilities", SpeechCapabilities),
        ("music-request", MusicRequest),
        ("music-capabilities", MusicCapabilities),
        ("sound-effect-request", SoundEffectRequest),
        ("sound-effect-capabilities", SoundEffectCapabilities),
        ("storyboard", Storyboard),
        ("timeline", Timeline),
        ("assembly-request", AssemblyRequest),
    ]:
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://agent-platform.dev/contracts/{name}.schema.json"
        (root / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
