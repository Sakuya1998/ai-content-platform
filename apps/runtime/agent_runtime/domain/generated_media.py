"""Durable request and archived result contracts for synchronous media providers."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StoredMediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prompt: str = Field(min_length=1, max_length=16000, repr=False)
    parameters: dict[str, Any] = Field(default_factory=dict, repr=False)
    source_version_id: str = ""
    source_ir_hash: str = ""


class MediaArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    bucket: str = Field(min_length=1)
    object_key: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    content_type: str = Field(min_length=1)
    etag: str = ""


class GeneratedMediaTask(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    workspace_id: str
    project_id: str
    job_id: str
    modality: Literal["image", "audio", "music", "effect"]
    request: StoredMediaRequest = Field(repr=False)
    request_hash: str
    route_identity: dict[str, Any]
    review: dict[str, Any] = Field(default_factory=dict)
    state: Literal["prepared", "submitting", "submission_unknown", "outputs_ready", "failed", "cancelled"]
    fencing_token: int
    outputs: list[MediaArtifact] = Field(default_factory=list)
    vendor_request_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
