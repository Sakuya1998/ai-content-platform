"""Registered text adapters; structure and lineage remain Content IR 1.0 metadata."""

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime.domain.content import ContentDocument


class SceneMetadata(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)

    number: int = Field(ge=1)
    location: str | None = None
    time_of_day: str | None = None
    duration_seconds: float | None = Field(default=None, gt=0)
    episode_number: int | None = Field(default=None, ge=1)


class EpisodeMetadata(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)

    number: int = Field(ge=1)
    duration_seconds: float | None = Field(default=None, gt=0)


@dataclass(frozen=True)
class AdapterFinding:
    code: str
    message: str
    block_position: int | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "block_position": self.block_position}


@dataclass(frozen=True)
class AdaptationResult:
    document: ContentDocument
    findings: list[AdapterFinding]


@dataclass(frozen=True)
class ContentAdapter:
    contract: str
    check: Callable[[ContentDocument], list[AdapterFinding]]


@dataclass(frozen=True)
class WorkflowAdapter:
    contract: str
    check: Callable[[ContentDocument, ContentDocument | dict | None, str], list[AdapterFinding]]


ADAPTERS: dict[str, ContentAdapter] = {}
WORKFLOW_ADAPTERS: dict[str, WorkflowAdapter] = {}


def register_content_adapter(document_type: str, adapter: ContentAdapter) -> None:
    if document_type in ADAPTERS:
        raise ValueError("content adapter already registered")
    ADAPTERS[document_type] = adapter


def with_block_ids(document: ContentDocument) -> AdaptationResult:
    """Keep supplied IDs; derive missing IDs from document identity and block position.

    IDs deliberately exclude version and text so subsequent edits retain identity.
    Reordered/inserted blocks must carry their existing IDs explicitly.
    """
    document = document.model_copy(deep=True)
    seen = set()
    findings = []
    for index, block in enumerate(document.blocks):
        block.position = index
        if "block_id" not in block.metadata:
            block.metadata["block_id"] = str(uuid5(NAMESPACE_URL, f"content-ir:{document.document_id}:{index}"))
        block_id = block.metadata["block_id"]
        if not isinstance(block_id, str) or not block_id.strip():
            findings.append(AdapterFinding("invalid_block_id", "Block ID must be a non-empty string.", index))
        elif block_id in seen:
            findings.append(AdapterFinding("duplicate_block_id", "Block IDs must be unique within a document.", index))
        else:
            seen.add(block_id)
    return AdaptationResult(ContentDocument.model_validate(document.model_dump()), findings)


def _structure_findings(document: ContentDocument) -> list[AdapterFinding]:
    findings = []
    characters = set()
    scene_ids = {
        block.metadata["block_id"]
        for block in document.blocks
        if block.type == "scene" and isinstance(block.metadata.get("block_id"), str)
    }
    scenes: dict[int | None, list[tuple[int, SceneMetadata]]] = defaultdict(list)
    episodes: list[tuple[int, EpisodeMetadata]] = []
    for block in document.blocks:
        if block.type == "character":
            character_id = block.metadata.get("character_id")
            if not isinstance(character_id, str) or not character_id.strip():
                findings.append(
                    AdapterFinding(
                        "invalid_character_id",
                        "Character declarations require a non-empty character_id.",
                        block.position,
                    )
                )
            elif character_id in characters:
                findings.append(
                    AdapterFinding(
                        "duplicate_character_id",
                        "Character IDs must be unique.",
                        block.position,
                    )
                )
            else:
                characters.add(character_id)
        for key, schema in (("scene", SceneMetadata), ("episode", EpisodeMetadata)):
            if key not in block.metadata:
                continue
            try:
                typed = schema.model_validate(block.metadata[key])
            except ValidationError:
                findings.append(
                    AdapterFinding(
                        f"invalid_{key}_metadata",
                        f"{key.title()} metadata has invalid numbers, duration, or field types.",
                        block.position,
                    )
                )
                continue
            block.metadata[key] = typed.model_dump(exclude_none=True)
            if key == "scene":
                scenes[typed.episode_number].append((block.position, typed))
            else:
                episodes.append((block.position, typed))
    for block in document.blocks:
        if block.type != "dialogue":
            continue
        character_id = block.metadata.get("character_id")
        if not isinstance(character_id, str) or not character_id.strip():
            findings.append(
                AdapterFinding(
                    "missing_character_reference",
                    "Dialogue must reference a declared character_id.",
                    block.position,
                )
            )
        elif character_id not in characters:
            findings.append(
                AdapterFinding(
                    "unknown_character_reference",
                    "Dialogue references an undeclared character_id.",
                    block.position,
                )
            )
        if "scene_id" in block.metadata:
            scene_id = block.metadata["scene_id"]
            if not isinstance(scene_id, str) or scene_id not in scene_ids:
                findings.append(
                    AdapterFinding(
                        "unknown_scene_reference",
                        "Dialogue scene_id must reference a scene block ID.",
                        block.position,
                    )
                )
    for items in scenes.values():
        for expected, (position, scene) in enumerate(items, 1):
            if scene.number != expected:
                findings.append(
                    AdapterFinding(
                        "invalid_scene_numbering",
                        "Scene numbers must be consecutive from one within each episode.",
                        position,
                    )
                )
    episode_numbers = {episode.number for _, episode in episodes}
    for expected, (position, episode) in enumerate(episodes, 1):
        if episode.number != expected:
            findings.append(
                AdapterFinding(
                    "invalid_episode_numbering",
                    "Episode numbers must be consecutive from one.",
                    position,
                )
            )
        duration = sum(scene.duration_seconds or 0 for _, scene in scenes.get(episode.number, []))
        if episode.duration_seconds is not None and duration > episode.duration_seconds:
            findings.append(
                AdapterFinding(
                    "episode_duration_exceeded",
                    "Scene durations exceed the declared episode duration.",
                    position,
                )
            )
    for episode_number, items in scenes.items():
        if episode_number is not None and episode_number not in episode_numbers:
            findings.extend(
                AdapterFinding(
                    "unknown_episode_reference",
                    "Scene episode_number must reference a declared episode.",
                    position,
                )
                for position, _ in items
            )
    return findings


_STRUCTURE_CONTRACT = (
    "Screenplay / short drama structure: retain metadata.block_id when supplied. "
    "Structured scenes use metadata.scene={number: positive integer, location?: string, time_of_day?: string, "
    "duration_seconds?: positive finite number, episode_number?: positive integer}. "
    "Episodes use metadata.episode={number: positive integer, duration_seconds?: positive finite number}. "
    "Scene and episode numbering starts at one and is consecutive; scene numbers reset for each episode. "
    "Character blocks declare metadata.character_id; dialogue blocks reference a declared metadata.character_id "
    "and may reference a scene block via metadata.scene_id. Unstructured legacy scene text remains accepted."
)
register_content_adapter("screenplay", ContentAdapter(_STRUCTURE_CONTRACT, _structure_findings))
register_content_adapter("short_drama", ContentAdapter(_STRUCTURE_CONTRACT, _structure_findings))


def _conversion_findings(
    document: ContentDocument,
    source_document: ContentDocument | dict | None,
    source_version_id: str,
) -> list[AdapterFinding]:
    findings = []
    if not isinstance(source_version_id, str) or not source_version_id.strip():
        findings.append(AdapterFinding("missing_source_version", "Conversion requires a frozen source version ID."))
    source = None
    if source_document is None:
        findings.append(
            AdapterFinding("missing_source_document", "Conversion requires the source Content IR document.")
        )
    else:
        try:
            source = with_block_ids(ContentDocument.model_validate(source_document))
        except ValidationError:
            findings.append(AdapterFinding("invalid_source_document", "Source Content IR document is invalid."))
    if source is not None:
        findings.extend(
            AdapterFinding("source_" + item.code, item.message, item.block_position) for item in source.findings
        )
        if not source.document.blocks:
            findings.append(AdapterFinding("empty_source_document", "Conversion source contains no blocks."))
    known = (
        {
            block.metadata["block_id"]
            for block in source.document.blocks
            if isinstance(block.metadata.get("block_id"), str) and block.metadata["block_id"].strip()
        }
        if source is not None
        else set()
    )
    covered = set()
    document.metadata["source_document_id"] = source.document.document_id if source is not None else ""
    document.metadata["source_version_id"] = source_version_id
    for block in document.blocks:
        references = block.metadata.get("source_block_ids")
        if references is None or references == []:
            findings.append(
                AdapterFinding(
                    "missing_source_references",
                    "Every converted block must identify its source_block_ids.",
                    block.position,
                )
            )
        elif not isinstance(references, list) or any(
            not isinstance(item, str) or not item.strip() for item in references
        ):
            findings.append(
                AdapterFinding(
                    "invalid_source_references",
                    "source_block_ids must be a list of non-empty strings.",
                    block.position,
                )
            )
        else:
            if len(references) != len(set(references)):
                findings.append(
                    AdapterFinding(
                        "duplicate_source_reference",
                        "Source references within a block must be unique.",
                        block.position,
                    )
                )
            if set(references) - known:
                findings.append(
                    AdapterFinding(
                        "unknown_source_reference",
                        "Converted block references IDs absent from the source.",
                        block.position,
                    )
                )
            covered.update(set(references) & known)
        supplied_version = block.metadata.get("source_version_id")
        if "source_version_id" in block.metadata and supplied_version != source_version_id:
            findings.append(
                AdapterFinding(
                    "source_version_mismatch",
                    "Converted block references a different source version.",
                    block.position,
                )
            )
        else:
            block.metadata["source_version_id"] = source_version_id
    if known - covered:
        findings.append(
            AdapterFinding(
                "omitted_source_blocks",
                "Some source blocks are omitted; a person must review conversion coverage.",
            )
        )
    return findings


_CONVERSION_CONTRACT = (
    "Conversion lineage: every output block must provide metadata.source_block_ids listing exact source IDs "
    "and metadata.source_version_id matching the frozen version. Preserve source coverage; omitted source "
    "blocks require human review. Source references show lineage, not proof of factual equivalence. "
    "Do not invent source IDs or claim factual verification."
)
WORKFLOW_ADAPTERS["content.adapt@1"] = WorkflowAdapter(_CONVERSION_CONTRACT, _conversion_findings)


def adapt_document(
    document: ContentDocument | dict,
    *,
    workflow: str = "content.generate@1",
    source_document: ContentDocument | dict | None = None,
    source_version_id: str = "",
) -> AdaptationResult:
    """Normalize IDs and return deterministic findings; never claim factual equivalence."""
    result = with_block_ids(ContentDocument.model_validate(document))
    document, findings = result.document, list(result.findings)
    if adapter := ADAPTERS.get(document.document_type):
        findings.extend(adapter.check(document))
    if workflow_adapter := WORKFLOW_ADAPTERS.get(workflow):
        findings.extend(workflow_adapter.check(document, source_document, source_version_id))
    return AdaptationResult(ContentDocument.model_validate(document.model_dump()), findings)


def adapter_context(
    document_type: str,
    *,
    workflow: str,
    source_document: ContentDocument | dict | None = None,
    source_version_id: str = "",
) -> str:
    """Append adapter requirements to existing context without extending prompt schemas."""
    sections = []
    if adapter := ADAPTERS.get(document_type):
        sections.append(adapter.contract)
    if workflow_adapter := WORKFLOW_ADAPTERS.get(workflow):
        sections.append(workflow_adapter.contract + " Frozen source version: " + json.dumps(source_version_id))
        if source_document is not None:
            try:
                source = with_block_ids(ContentDocument.model_validate(source_document)).document
            except ValidationError:
                sections.append("Source IR is invalid and requires human review.")
            else:
                sections.append("Source Content IR (reference data):\n" + source.model_dump_json())
    return "\n".join(sections)
