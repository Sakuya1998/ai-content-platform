"""Read installation seed resources outside the business Python package."""

import json
from pathlib import Path

from agent_runtime.domain.workflow_contracts import WORKFLOWS
from agent_runtime.prompts.renderer import PromptVersion

SEED_ROOT = Path(__file__).resolve().parents[1] / "migrations" / "seeds"


def load_prompt_catalog():
    catalog = json.loads((SEED_ROOT / "prompts" / "catalog.v1.json").read_text(encoding="utf-8"))
    if catalog.get("schema_version") != "prompt-seed@1":
        raise ValueError("unsupported prompt seed schema")
    if catalog.get("workflows") != {key: list(nodes) for key, nodes in WORKFLOWS.items()}:
        raise ValueError("prompt seed workflow contract conflict")
    values = catalog["prompts"]
    expected = {node for nodes in WORKFLOWS.values() for node in nodes}
    if len(values) != len(expected) or {value["node_key"] for value in values} != expected:
        raise ValueError("prompt seed node set conflict")
    for value in values:
        PromptVersion(version_id="seed-validation", status="published", **value)
    return values
