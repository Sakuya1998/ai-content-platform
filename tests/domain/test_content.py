import pytest
from pydantic import ValidationError

from agent_runtime.domain.content import ContentBlock, ContentDocument


def test_content_document_is_versioned_and_round_trips_blocks():
    document = ContentDocument(
        document_id="chapter-1",
        schema_version="1.0",
        blocks=[ContentBlock(type="text", text="开场", metadata={"source": "draft"})],
    )

    restored = ContentDocument.model_validate_json(document.model_dump_json())
    assert restored.schema_version == "1.0"
    assert restored.blocks[0].text == "开场"
    assert len(restored.content_hash) == 64


def test_content_document_rejects_unsupported_schema_version():
    with pytest.raises(ValidationError):
        ContentDocument(document_id="doc", schema_version="", blocks=[])
