"""Deterministically assemble bounded timelines from storyboard and probed media."""

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_runtime.domain.storyboard import Storyboard
from agent_runtime.domain.timeline import AudioTrack, StableIdentifier, SubtitleCue, Timeline, TimelineAsset, VisualShot

CanonicalVersionId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


class AssemblyReviewRequired(ValueError):
    """Assembly cannot preserve the complete plan without a human decision."""


def _canonical_version_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("version_id must be a canonical UUID")
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("version_id must be a canonical UUID") from exc
    if canonical != value:
        raise ValueError("version_id must be a canonical UUID")
    return value


class AssemblyShot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    shot_id: StableIdentifier
    visual_version_id: CanonicalVersionId
    visual_block_position: int = Field(default=0, ge=0)
    audio_version_id: CanonicalVersionId | None = None
    audio_block_position: int = Field(default=0, ge=0)

    @field_validator("visual_version_id", "audio_version_id")
    @classmethod
    def validate_version_id(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_version_id(value)

    @model_validator(mode="after")
    def validate_audio_binding(self) -> AssemblyShot:
        if self.audio_version_id is None and self.audio_block_position != 0:
            raise ValueError("audio_block_position requires audio_version_id")
        return self


class AssemblyEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    shot_id: StableIdentifier
    version_id: CanonicalVersionId
    block_position: int = Field(default=0, ge=0)

    @field_validator("version_id")
    @classmethod
    def validate_version_id(cls, value: str) -> str:
        return _canonical_version_id(value)


class AssemblyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    width: int = Field(default=1280, ge=64, le=1920, multiple_of=2)
    height: int = Field(default=720, ge=64, le=1920, multiple_of=2)
    fps: int = Field(default=25, ge=1, le=60)
    subtitles: bool = True
    shots: list[AssemblyShot] = Field(min_length=1, max_length=32)
    music_version_id: CanonicalVersionId | None = None
    music_block_position: int = Field(default=0, ge=0)
    effects: list[AssemblyEffect] = Field(default_factory=list, max_length=32)

    @field_validator("music_version_id")
    @classmethod
    def validate_music_version_id(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_version_id(value)

    @model_validator(mode="after")
    def validate_render_contract(self) -> AssemblyRequest:
        if self.width * self.height > 2_073_600:
            raise ValueError("assembly output exceeds the pixel budget")
        ids = [shot.shot_id for shot in self.shots]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate shot_id")
        if self.music_version_id is None and self.music_block_position != 0:
            raise ValueError("music_block_position requires music_version_id")
        effect_shots = [effect.shot_id for effect in self.effects]
        if len(effect_shots) != len(set(effect_shots)):
            raise ValueError("duplicate sound effect shot_id")
        if set(effect_shots) - set(ids):
            raise ValueError("sound effect references an unknown storyboard shot")
        return self


@dataclass(frozen=True)
class ProbedAsset:
    """Trusted probe facts; stream durations override the container duration."""

    version_id: str
    block_position: int
    kind: Literal["image", "video", "audio"]
    duration_ms: int
    visual_video_duration_ms: int | None = None
    audio_duration_ms: int | None = None

    def __post_init__(self) -> None:
        _canonical_version_id(self.version_id)
        if type(self.block_position) is not int or self.block_position < 0:
            raise ValueError("block_position must be a nonnegative integer")
        if self.kind not in {"image", "video", "audio"}:
            raise ValueError("unsupported probed asset kind")
        for name, duration in (
            ("duration_ms", self.duration_ms),
            ("visual_video_duration_ms", self.visual_video_duration_ms),
            ("audio_duration_ms", self.audio_duration_ms),
        ):
            if duration is None and name != "duration_ms":
                continue
            if type(duration) is not int or duration < 0:
                raise ValueError(f"{name} must be a nonnegative integer")


def _probe(media: dict[tuple[str, int], ProbedAsset], reference: tuple[str, int]) -> ProbedAsset:
    asset = media.get(reference)
    if asset is None:
        raise ValueError("assembly references a missing media block")
    if not isinstance(asset, ProbedAsset) or (asset.version_id, asset.block_position) != reference:
        raise ValueError("probed media identity does not match the requested reference")
    return asset


def _audio_duration(asset: ProbedAsset) -> int:
    if asset.kind == "audio":
        duration = asset.audio_duration_ms if asset.audio_duration_ms is not None else asset.duration_ms
    elif asset.kind == "video" and asset.audio_duration_ms is not None:
        duration = asset.audio_duration_ms
    else:
        raise ValueError("narration requires an audio stream")
    if duration <= 0:
        raise ValueError("narration audio duration must be positive")
    return duration


def _subtitle_text(dialogue: str) -> str:
    text = " ".join(dialogue.split())
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in text):
        raise AssemblyReviewRequired("dialogue contains unsafe subtitle control characters")
    if len(text) > 2000:
        raise AssemblyReviewRequired("complete dialogue exceeds the subtitle cue text limit")
    return text


def assemble_timeline(
    storyboard: Storyboard, request: AssemblyRequest, media: dict[tuple[str, int], ProbedAsset]
) -> Timeline:
    """Keep all narration and require review when a video or timeline is too short."""
    storyboard = Storyboard.model_validate(storyboard.model_dump())
    request = AssemblyRequest.model_validate(request.model_dump())
    if [shot.shot_id for shot in request.shots] != [shot.shot_id for shot in storyboard.shots]:
        raise ValueError("assembly shots must exactly match storyboard shot order")

    assets: dict[tuple[str, int], TimelineAsset] = {}
    shots: list[VisualShot] = []
    audio_tracks: list[AudioTrack] = []
    subtitles: list[SubtitleCue] = []
    cursor = 0
    shot_spans: dict[str, tuple[int, int]] = {}

    def asset_id(reference: tuple[str, int]) -> str:
        if reference not in assets:
            digest = hashlib.sha256(f"{reference[0]}:{reference[1]}".encode()).hexdigest()
            assets[reference] = TimelineAsset(
                asset_id=f"asset-{digest}", document_version_id=reference[0], block_position=reference[1]
            )
        return assets[reference].asset_id

    for index, (planned, binding) in enumerate(zip(storyboard.shots, request.shots, strict=True)):
        visual_ref = (binding.visual_version_id, binding.visual_block_position)
        visual = _probe(media, visual_ref)
        if visual.kind not in {"image", "video"}:
            raise ValueError("visual binding requires an image or video block")

        audio_ref = None
        narration_duration = 0
        if binding.audio_version_id is not None:
            audio_ref = (binding.audio_version_id, binding.audio_block_position)
            narration_duration = _audio_duration(_probe(media, audio_ref))
        duration = max(planned.duration_ms, narration_duration)
        if visual.kind == "video":
            visual_duration = (
                visual.visual_video_duration_ms if visual.visual_video_duration_ms is not None else visual.duration_ms
            )
            if visual_duration < duration:
                raise AssemblyReviewRequired(
                    f"video for shot {planned.shot_id} cannot contain the complete planned shot"
                )
        if cursor + duration > 600_000:
            raise AssemblyReviewRequired("complete assembly exceeds the ten-minute duration limit")

        shots.append(
            VisualShot(shot_id=planned.shot_id, asset_id=asset_id(visual_ref), start_ms=cursor, duration_ms=duration)
        )
        shot_spans[planned.shot_id] = (cursor, duration)
        if audio_ref is not None:
            audio_tracks.append(
                AudioTrack(
                    track_id=f"narration-{index}",
                    asset_id=asset_id(audio_ref),
                    start_ms=cursor,
                    duration_ms=narration_duration,
                )
            )
            if request.subtitles:
                text = _subtitle_text(planned.dialogue)
                if text:
                    subtitles.append(
                        SubtitleCue(
                            cue_id=f"dialogue-{index}", start_ms=cursor, end_ms=cursor + narration_duration, text=text
                        )
                    )
        cursor += duration

    if request.music_version_id is not None:
        music_ref = (request.music_version_id, request.music_block_position)
        music_duration = min(_audio_duration(_probe(media, music_ref)), cursor)
        audio_tracks.append(
            AudioTrack(
                track_id="music",
                asset_id=asset_id(music_ref),
                start_ms=0,
                duration_ms=music_duration,
                gain=0.25,
                role="music",
                fade_in_ms=min(250, music_duration // 4),
                fade_out_ms=min(500, music_duration // 3),
            )
        )

    for effect in request.effects:
        effect_ref = (effect.version_id, effect.block_position)
        start, shot_duration = shot_spans[effect.shot_id]
        effect_duration = min(_audio_duration(_probe(media, effect_ref)), shot_duration)
        audio_tracks.append(
            AudioTrack(
                track_id=f"effect-{effect.shot_id}",
                asset_id=asset_id(effect_ref),
                start_ms=start,
                duration_ms=effect_duration,
                gain=0.85,
                role="effect",
            )
        )

    return Timeline(
        width=request.width,
        height=request.height,
        fps=request.fps,
        duration_ms=cursor,
        assets=list(assets.values()),
        shots=shots,
        audio_tracks=audio_tracks,
        subtitles=subtitles,
    )
