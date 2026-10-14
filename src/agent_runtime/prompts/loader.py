"""Render a node's frozen database prompt within a claimed task."""

from agent_runtime.prompts.renderer import render_node


def fill_template(agent_name: str, **variables) -> list[dict[str, str]]:
    return render_node(agent_name, variables)
