"""Typed storyboard planning IR embedded in portable Content IR."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.domain.timeline import DurationMilliseconds, StableIdentifier


class StoryboardShot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    shot_id: StableIdentifier
    description: str = Field(min_length=1, max_length=8000, repr=False)
    dialogue: str = Field(default="", max_length=8000, repr=False)
    duration_ms: DurationMilliseconds
    source_block_ids: list[StableIdentifier] = Field(default_factory=list, max_length=256)

    @field_validator("description")
    @classmethod
    def reject_blank_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("shot description cannot be blank")
        return value


class Storyboard(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["storyboard@1"] = "storyboard@1"
    shots: list[StoryboardShot] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_shot_ids(self) -> Storyboard:
        ids = [shot.shot_id for shot in self.shots]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate shot_id")
        return self


def storyboard_document(
    storyboard: Storyboard,
    *,
    document_id: str,
    project_id: str = "",
    version: int = 1,
    metadata: dict[str, Any] | None = None,
) -> ContentDocument:
    """Embed typed shots and create readable blocks for indexing or review."""
    storyboard = Storyboard.model_validate(storyboard.model_dump(mode="json"))
    return ContentDocument(
        document_id=document_id,
        project_id=project_id,
        document_type="storyboard",
        version=version,
        schema_version="1.0",
        blocks=[
            ContentBlock(
                type="storyboard_shot",
                text=shot.description + (f"\n{shot.dialogue}" if shot.dialogue else ""),
                position=position,
                metadata={"shot_id": shot.shot_id, "source_block_ids": list(shot.source_block_ids)},
            )
            for position, shot in enumerate(storyboard.shots)
        ],
        metadata={**(metadata or {}), "storyboard": storyboard.model_dump(mode="json")},
    )


def storyboard_from_document(document: ContentDocument) -> Storyboard:
    if document.schema_version != "1.0" or document.document_type != "storyboard":
        raise ValueError("storyboard requires a storyboard Content IR 1.0 document")
    if "storyboard" not in document.metadata:
        raise ValueError("Content IR document is missing metadata.storyboard")
    return Storyboard.model_validate(document.metadata["storyboard"])
