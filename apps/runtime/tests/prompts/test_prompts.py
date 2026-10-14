"""Prompt 模板与解析工具测试。"""

import pytest

from agent_runtime.execution.structured import parse_json_block, parse_yaml_block
from agent_runtime.prompts import fill_template
from tests.fixtures.prompts import get_variables, load_template, prompt_versions

ALL_TEMPLATES = [
    "world_builder",
    "character_designer",
    "plot_planner",
    "scene_planner",
    "scene_writer",
    "scene_rewriter",
    "style_editor",
    "consistency_checker",
    "chapter_digest",
    "replanner",
    "book_auditor",
]


def test_all_templates_loadable():
    for name in ALL_TEMPLATES:
        text = load_template(name)
        assert len(text) > 100, f"{name} 模板过短"


def test_variables_extracted():
    assert "style_prompt" in get_variables("scene_writer")
    assert "canon_context" in get_variables("scene_planner")
    assert "chapter_number" in get_variables("consistency_checker")
    assert "chapter_content" in get_variables("chapter_digest")
    assert "future_outline" in get_variables("replanner")
    assert "hierarchical_memory" in get_variables("book_auditor")
    assert "user_input" in get_variables("world_builder")
    for template in (
        "scene_planner",
        "scene_rewriter",
        "style_editor",
        "replanner",
        "book_auditor",
    ):
        assert "creative_brief" in get_variables(template)


def test_fill_template_roundtrip():
    from agent_runtime.prompts.renderer import render_prompt

    out = render_prompt(prompt_versions()["world_builder"], {"user_input": "标题:测试\n类型:武侠"})[0]["content"]
    assert "标题:测试" in out
    assert "{user_input}" not in out


def test_fill_template_missing_var_raises():
    with pytest.raises(RuntimeError, match="claimed Runtime"):
        fill_template("world_builder")


def test_parse_yaml_block_plain_and_wrapped():
    plain = "- name: 甲\n  role: 主角\n- name: 乙\n  role: 反派\n"
    wrapped = f"好的,以下是角色:\n```yaml\n{plain}```\n请查收"
    assert parse_yaml_block(plain) == parse_yaml_block(wrapped)
    assert len(parse_yaml_block(wrapped)) == 2
    assert parse_yaml_block(wrapped)[0]["name"] == "甲"


def test_parse_json_block_with_noise():
    text = '检查完成:\n```json\n[{"type": "时间线", "severity": "high"}]\n```'
    issues = parse_json_block(text)
    assert issues[0]["severity"] == "high"

    # 无代码块的裸数组
    assert parse_json_block("[]") == []
    # 前后噪声中截取数组
    assert parse_json_block('结果如下 [{"type": "x"}] 以上')[0]["type"] == "x"

    with pytest.raises(ValueError, match="对象"):
        parse_json_block("[1, 2]")
