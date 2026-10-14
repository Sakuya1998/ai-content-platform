import json
from pathlib import Path

import pytest

from agent_runtime.infrastructure.storage.keys import object_key

EXAMPLES = json.loads((Path(__file__).resolve().parents[2] / "contracts/object-keys.examples.json").read_text())


@pytest.mark.parametrize("example", EXAMPLES)
def test_shared_object_key_contract(example):
    fields = {key: value for key, value in example.items() if key != "expected"}
    assert object_key(**fields) == example["expected"]


@pytest.mark.parametrize("field", ["workspace_id", "project_id", "resource_id", "version_id", "filename"])
@pytest.mark.parametrize("value", ["", "..", "a/b", "a\\b"])
def test_object_key_rejects_invalid_path_segments(field, value):
    fields = {
        "workspace_id": "ws-1",
        "project_id": "project-1",
        "category": "assets",
        "resource_id": "asset-1",
        "version_id": "v-1",
        "filename": "image.png",
    }
    fields[field] = value
    with pytest.raises(ValueError):
        object_key(**fields)


@pytest.mark.parametrize("category,version", [("assets", None), ("documents", None), ("exports", "v-1")])
def test_object_key_enforces_versioned_categories(category, version):
    with pytest.raises(ValueError):
        object_key("ws-1", "project-1", category, "resource-1", "file.json", version_id=version)
