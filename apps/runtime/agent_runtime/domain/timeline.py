"""Closed, portable timeline IR referencing immutable content versions."""

import unicodedata
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_runtime.domain.content import ContentBlock, ContentDocument

StableIdentifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Milliseconds = Annotated[int, Field(ge=0)]
DurationMilliseconds = Annotated[int, Field(gt=0, le=600_000)]


class TimelineAsset(BaseModel):
    """A block in an immutable content version, never a user-supplied path."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    asset_id: StableIdentifier
    document_version_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    block_position: int = Field(ge=0)

    @field_validator("document_version_id")
    @classmethod
    def require_canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("document_version_id must be a canonical UUID")
        return value


class VisualShot(BaseModel):
    """One segment on the ordered, continuous primary visual track."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    shot_id: StableIdentifier
    asset_id: StableIdentifier
    start_ms: Milliseconds
    duration_ms: DurationMilliseconds
    source_start_ms: Milliseconds = 0
    fit: Literal["contain", "cover"] = "contain"


class AudioTrack(BaseModel):
    """An independently positioned audio segment."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    track_id: StableIdentifier
    asset_id: StableIdentifier
    start_ms: Milliseconds
    duration_ms: DurationMilliseconds
    source_start_ms: Milliseconds = 0
    gain: float = Field(default=1.0, ge=0, le=2, allow_inf_nan=False)
    role: Literal["narration", "music", "effect"] = "narration"
    fade_in_ms: Milliseconds = 0
    fade_out_ms: Milliseconds = 0

    @field_validator("gain", mode="before")
    @classmethod
    def reject_boolean_gain(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("gain must be a number")
        return value

    @model_validator(mode="after")
    def validate_fades(self) -> AudioTrack:
        if self.fade_in_ms + self.fade_out_ms > self.duration_ms:
            raise ValueError("audio fades must fit within the track duration")
        return self


class SubtitleCue(BaseModel):
    """A text cue; text is data and cannot carry control characters."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    cue_id: StableIdentifier
    start_ms: Milliseconds
    end_ms: Milliseconds
    text: str = Field(min_length=1, max_length=2000, repr=False)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip() or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
            raise ValueError("subtitle text must be nonblank and contain no control characters")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> SubtitleCue:
        if self.end_ms <= self.start_ms:
            raise ValueError("subtitle end_ms must be greater than start_ms")
        return self


class Timeline(BaseModel):
    """Deterministic render instructions with no provider URLs or executable expressions."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["timeline@1"] = "timeline@1"
    width: int = Field(ge=64, le=1920, multiple_of=2)
    height: int = Field(ge=64, le=1920, multiple_of=2)
    fps: int = Field(ge=1, le=60)
    duration_ms: DurationMilliseconds
    assets: list[TimelineAsset] = Field(min_length=1, max_length=256)
    shots: list[VisualShot] = Field(min_length=1, max_length=128)
    audio_tracks: list[AudioTrack] = Field(default_factory=list, max_length=128)
    subtitles: list[SubtitleCue] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_render_contract(self) -> Timeline:
        if self.width * self.height > 2_073_600:
            raise ValueError("timeline output exceeds the pixel budget")
        for values, identifier in (
            (self.assets, "asset_id"),
            (self.shots, "shot_id"),
            (self.audio_tracks, "track_id"),
            (self.subtitles, "cue_id"),
        ):
            ids = [getattr(value, identifier) for value in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {identifier}")
        asset_ids = {asset.asset_id for asset in self.assets}
        cursor = 0
        for shot in self.shots:
            if shot.asset_id not in asset_ids:
                raise ValueError("shot references an unknown asset_id")
            if shot.start_ms != cursor:
                raise ValueError("visual shots must be ordered and continuous without overlap or gaps")
            cursor += shot.duration_ms
        if cursor != self.duration_ms:
            raise ValueError("visual shots must cover exactly duration_ms")
        for track in self.audio_tracks:
            if track.asset_id not in asset_ids:
                raise ValueError("audio track references an unknown asset_id")
            if track.start_ms + track.duration_ms > self.duration_ms:
                raise ValueError("audio track exceeds timeline duration_ms")
        if any(cue.end_ms > self.duration_ms for cue in self.subtitles):
            raise ValueError("subtitle cue exceeds timeline duration_ms")
        return self


def timeline_document(
    timeline: Timeline,
    *,
    document_id: str,
    project_id: str = "",
    version: int = 1,
    metadata: dict[str, Any] | None = None,
) -> ContentDocument:
    """Embed validated Timeline IR in Content IR 1.0."""
    timeline = Timeline.model_validate(timeline.model_dump(mode="json"))
    return ContentDocument(
        document_id=document_id,
        project_id=project_id,
        document_type="timeline",
        version=version,
        schema_version="1.0",
        blocks=[ContentBlock(type="timeline", text=f"{len(timeline.shots)} shots; {timeline.duration_ms} ms")],
        metadata={**(metadata or {}), "timeline": timeline.model_dump(mode="json")},
    )


def timeline_from_document(document: ContentDocument) -> Timeline:
    """Read the authoritative typed timeline payload from Content IR."""
    if document.schema_version != "1.0" or document.document_type != "timeline":
        raise ValueError("timeline requires a timeline Content IR 1.0 document")
    if "timeline" not in document.metadata:
        raise ValueError("Content IR document is missing metadata.timeline")
    return Timeline.model_validate(document.metadata["timeline"])
