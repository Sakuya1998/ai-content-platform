"""Shared structured model output parsing and bounded correction retries."""

import json
import re
from collections.abc import Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel


class StructuredOutputError(ValueError):
    """LLM 连续两次未返回符合约定的结构化结果。"""


def extract_code_block(text: str, lang: str) -> str:
    """提取 ```lang ... ``` 代码块;无代码块则返回原文(去首尾空白)。"""
    pattern = rf"```{lang}\s*\n(.*?)```"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def parse_yaml_block(text: str) -> list[dict]:
    """从 LLM 输出解析 YAML 列表(容忍代码块包裹与前后噪声)。"""
    import yaml

    block = extract_code_block(text, "yaml")
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        # 退化:截取首个列表型代码段重试
        m = re.search(r"^- .*$", block, re.MULTILINE)
        if not m:
            raise
        start = m.start()
        data = yaml.safe_load(block[start:])
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError(f"YAML 解析结果不是列表: {type(data)}")
    return [item for item in data if isinstance(item, dict)]


def parse_json_block(text: str) -> list[dict]:
    """从 LLM 输出解析 JSON 数组(容忍代码块包裹与前后噪声)。"""
    block = extract_code_block(text, "json")
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        start, end = block.find("["), block.rfind("]")
        if start == -1 or end <= start:
            raise ValueError("未找到 JSON 数组") from None
        data = json.loads(block[start : end + 1])
    if not isinstance(data, list):
        data = [data]
    if any(not isinstance(item, dict) for item in data):
        raise ValueError("JSON 数组中的每一项都必须是对象")
    return data


async def invoke_structured(
    llm: BaseChatModel,
    prompt: str | list[dict[str, str]],
    *,
    parser: Callable[[str], Any],
    validator: Callable[[Any], None],
    agent_name: str,
    format_name: str,
) -> tuple[str, Any]:
    """调用 LLM 并解析结构化结果;失败时追加纠正指令重试一次。"""
    current_prompt = prompt
    last_error: Exception | None = None
    for attempt in range(2):
        response = await llm.ainvoke(current_prompt)
        content = response.content if isinstance(response.content, str) else str(response.content)
        try:
            parsed = parser(content)
            from agent_runtime.prompts.renderer import validate_rendered_output

            validate_rendered_output(current_prompt, parsed)
            validator(parsed)
            return content, parsed
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                correction = (
                    "\n\n## 格式纠正\n"
                    f"上一次输出未通过 {format_name} 解析或校验:{exc}。"
                    "请重新生成完整结果,只输出要求的结构化内容,不要解释。"
                )
                current_prompt = (
                    [*prompt, {"role": "user", "content": correction}]
                    if isinstance(prompt, list)
                    else prompt + correction
                )
                from agent_runtime.prompts.renderer import inherit_rendering

                inherit_rendering(prompt, current_prompt)

    raise StructuredOutputError(f"{agent_name} 连续两次未返回有效 {format_name}: {last_error}") from last_error


__all__ = [
    "StructuredOutputError",
    "extract_code_block",
    "invoke_structured",
    "parse_yaml_block",
    "parse_json_block",
]
