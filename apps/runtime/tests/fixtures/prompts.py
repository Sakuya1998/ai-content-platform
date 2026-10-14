"""Explicit catalog versions for isolated Runtime tests without Control API."""

from uuid import NAMESPACE_URL, uuid5

from agent_runtime.prompts.renderer import PromptVersion
import sys
from pathlib import Path

# Add tools directory to path for importing seed scripts
tools_dir = Path(__file__).resolve().parents[4] / "tools" / "seed"
sys.path.insert(0, str(tools_dir))

from platform_seed_catalog import load_prompt_catalog


def prompt_versions():
    return {
        value["node_key"]: PromptVersion(
            version_id=str(uuid5(NAMESPACE_URL, value["key"] + value["content_hash"])), status="published", **value
        )
        for value in load_prompt_catalog()
    }


def load_template(node):
    return "\n\n".join(message.content for message in prompt_versions()[node].messages)


def get_variables(node):
    return list(prompt_versions()[node].variables_schema["properties"])
