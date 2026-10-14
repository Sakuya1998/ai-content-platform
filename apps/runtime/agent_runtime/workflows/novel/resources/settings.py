"""Typed defaults for the builtin novel workflow, independent of service secrets."""

from pydantic import BaseModel, ConfigDict, Field


class GenerationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_chapter_words: int = Field(default=6000, gt=0)
    max_revision_attempts: int = Field(default=3, ge=0)
    quality_gate_threshold: float = Field(default=70.0, ge=0, le=100)
