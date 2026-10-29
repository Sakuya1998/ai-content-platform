from tools.migrate.platform_policy_seed import load_policy_catalog
from tools.migrate.platform_seed_catalog import load_prompt_catalog

from agent_runtime.prompts.renderer import PromptVersion, render_prompt


def test_video_planning_and_review_are_database_seed_contracts():
    definitions = {value["node_key"]: value for value in load_prompt_catalog()}
    version = PromptVersion(version_id="test", status="published", **definitions["media.plan"])
    messages = render_prompt(version, {"objective": "产品演示", "context": "原稿", "parameters": {"duration": 5}})
    assert "产品演示" in str(messages) and "原稿" in str(messages)
    assert version.output_schema["required"] == ["prompt"]
    assert "media.review" in definitions


def test_video_policy_has_its_own_frozen_binding():
    catalog = load_policy_catalog()
    key = catalog["workflows"]["media.video@1"]
    policy = next(value["payload"] for value in catalog["definitions"] if value["key"] == key)
    assert "video" in policy["constraints"]["schema"]["properties"]["document_type"]["enum"]
