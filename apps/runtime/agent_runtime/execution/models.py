"""Resolve models through the current task's frozen Runtime dependencies."""

from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel


def get_llm(
    temperature: float | None = None,
    model_name: str | None = None,
    streaming: bool = True,
    purpose: Literal["creative", "analysis"] = "creative",
) -> BaseChatModel:
    """按工作台全局路由获取聊天模型。

    Args:
        temperature: 覆盖配置温度(创作类节点用高值,分析类用低值)
        model_name: 覆盖配置模型名
        streaming: 是否启用流式输出(默认 True,配合 LangGraph astream_events)

    Returns:
        LangChain BaseChatModel 实例
    """
    from agent_runtime.execution.context import current_execution
    from agent_runtime.infrastructure.models.client import RuntimeModels
    from agent_runtime.workflows.novel.context import current_novel

    if dependencies := current_novel():
        return dependencies.models.chat(purpose)
    if execution := current_execution(required=False):
        return RuntimeModels(execution.settings).chat(purpose)
    raise RuntimeError("model execution scope is missing; use a claimed Runtime task")


def get_analyzer_llm() -> BaseChatModel:
    """分析类节点专用:低温度保证输出稳定(一致性检查/情节规划)。"""
    return get_llm(temperature=0.3, purpose="analysis")
