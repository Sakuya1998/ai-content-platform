import os

import psycopg
import pytest
from tools.migrate.platform_seed_catalog import load_prompt_catalog
from tools.seed.platform_prompt_seed import seed

from agent_runtime.domain.workflow_contracts import WORKFLOWS


def test_catalog_covers_all_nodes_with_independent_variable_contracts():
    from agent_runtime.prompts.renderer import PromptVersion, render_prompt

    definitions = load_prompt_catalog()
    assert len(definitions) == 24
    assert set(node for nodes in WORKFLOWS.values() for node in nodes) == {value["node_key"] for value in definitions}
    for value in definitions:
        version = PromptVersion(version_id="test", status="published", **value)
        variables = {
            key: 1
            if property["type"] == "integer"
            else []
            if property["type"] == "array"
            else {}
            if property["type"] == "object"
            else "text"
            for key, property in value["variables_schema"]["properties"].items()
        }
        assert render_prompt(version, variables)[0]["content"]


def test_prompt_seed_is_idempotent_and_platform_rls_is_restored():
    url = os.getenv("MQ_TEST_CONTROL_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
    seed(url)
    result = seed(url)
    assert result["prompt_count"] == 24 and result["workflow_count"] == 13
    assert result["prompt_versions_created"] == result["pack_versions_created"] == result["bindings_updated"] == 0
    with psycopg.connect(url) as connection:
        count = connection.execute("SELECT count(*) FROM prompt_versions WHERE workspace_id IS NULL").fetchone()[0]
        assert count >= 24
        assert connection.execute("UPDATE prompts SET name='forged' WHERE workspace_id IS NULL").rowcount == 0
