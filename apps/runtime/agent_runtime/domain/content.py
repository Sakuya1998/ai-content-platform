"""Versioned content intermediate representation (Content IR)."""

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContentBlock(BaseModel):
    """A portable, prompt-safe unit of content."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=64)
    text: str = ""
    position: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentDocument(BaseModel):
    """A versioned document composed of Content IR blocks."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=200)
    project_id: str = ""
    document_type: str = "generic"
    version: int = Field(default=1, ge=1)
    schema_version: str = Field(min_length=1, max_length=32)
    blocks: list[ContentBlock] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""

    @field_validator("schema_version")
    @classmethod
    def require_semver_like_version(cls, value: str) -> str:
        if "." not in value or not value[0].isdigit():
            raise ValueError("schema_version must be a dotted version")
        return value

    @model_validator(mode="after")
    def set_content_hash(self) -> ContentDocument:
        payload = self.model_dump(exclude={"content_hash"}, mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.content_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return self
