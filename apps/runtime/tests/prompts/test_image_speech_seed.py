from agent_runtime.prompts.renderer import PromptVersion, render_prompt
from scripts.platform_policy_seed import load_policy_catalog
from scripts.platform_seed_catalog import load_prompt_catalog


def test_generated_media_planning_are_database_contracts():
    values = {v["node_key"]: v for v in load_prompt_catalog()}
    for node, output in [
        ("media.image.plan", "prompt"),
        ("media.speech.plan", "text"),
        ("media.music.plan", "prompt"),
        ("media.sound_effect.plan", "text"),
    ]:
        version = PromptVersion(version_id="test", status="published", **values[node])
        assert version.output_schema["required"] == [output]
        assert render_prompt(version, {"objective": "车站", "context": "原稿", "parameters": {}})


def test_image_and_audio_have_separate_policy_bindings():
    catalog = load_policy_catalog()
    for workflow, content_type in [
        ("media.image@1", "image"),
        ("media.speech@1", "audio"),
        ("media.music@1", "audio"),
        ("media.sound_effect@1", "audio"),
    ]:
        key = catalog["workflows"][workflow]
        policy = next(v["payload"] for v in catalog["definitions"] if v["key"] == key)
        assert policy["constraints"]["schema"]["properties"]["document_type"]["enum"] == [content_type]
