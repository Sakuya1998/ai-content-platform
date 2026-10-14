import copy

import jsonschema
import pytest
from pydantic import ValidationError

from agent_runtime.domain.content import ContentDocument
from agent_runtime.domain.storyboard import Storyboard, storyboard_document, storyboard_from_document


def valid_storyboard():
    return {
        "schema_version": "storyboard@1",
        "shots": [
            {
                "shot_id": "opening",
                "description": "月下，一位少女走进庭院。",
                "duration_ms": 3000,
                "source_block_ids": ["chapter-1", "scene.2"],
            }
        ],
    }


def test_storyboard_embeds_typed_ir_and_round_trips_chinese():
    storyboard = Storyboard.model_validate(valid_storyboard())
    assert storyboard.shots[0].dialogue == ""
    document = storyboard_document(storyboard, document_id="storyboard-1", project_id="project-1", version=3)
    assert document.schema_version == "1.0"
    assert document.document_type == "storyboard"
    assert document.blocks[0].text == "月下，一位少女走进庭院。"
    assert document.metadata["storyboard"]["shots"][0]["source_block_ids"] == ["chapter-1", "scene.2"]
    assert storyboard_from_document(ContentDocument.model_validate_json(document.model_dump_json())) == storyboard
    jsonschema.validate(storyboard.model_dump(mode="json"), Storyboard.model_json_schema())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shot_id", "../../bad"),
        ("shot_id", "https://example.com"),
        ("shot_id", "镜头"),
        ("description", " "),
        ("duration_ms", True),
        ("duration_ms", "3000"),
        ("duration_ms", 0),
        ("duration_ms", 600001),
        ("source_block_ids", ["/etc/passwd"]),
        ("source_block_ids", [True]),
        ("source_block_ids", ["x" * 129]),
        ("command", "ffmpeg"),
        ("dialogue", 42),
    ],
)
def test_storyboard_rejects_untyped_unsafe_or_unknown_shot_fields(field, value):
    payload = valid_storyboard()
    payload["shots"][0][field] = value
    with pytest.raises(ValidationError):
        Storyboard.model_validate(payload)


def test_storyboard_rejects_duplicate_shots_and_empty_shot_list():
    payload = valid_storyboard()
    payload["shots"].append(copy.deepcopy(payload["shots"][0]))
    with pytest.raises(ValidationError):
        Storyboard.model_validate(payload)
    payload["shots"] = []
    with pytest.raises(ValidationError):
        Storyboard.model_validate(payload)


def test_storyboard_rejects_unknown_top_level_fields():
    with pytest.raises(ValidationError):
        Storyboard.model_validate({**valid_storyboard(), "url": "https://example.com"})


@pytest.mark.parametrize(
    ("field", "value"), [("document_type", "generic"), ("schema_version", "2.0"), ("metadata", {})]
)
def test_storyboard_from_document_rejects_incompatible_ir(field, value):
    document = storyboard_document(Storyboard.model_validate(valid_storyboard()), document_id="storyboard-1")
    payload = document.model_dump()
    payload[field] = value
    with pytest.raises(ValueError):
        storyboard_from_document(ContentDocument.model_validate(payload))
