"""Guard the retired local runtime boundary and the remaining remote CLI."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_sources_cannot_import_local_storage_or_retired_management():
    forbidden = (
        "agent_runtime.infrastructure.control",
        "sqlite3",
        "chromadb",
        "langchain_chroma",
        "langgraph.checkpoint.sqlite",
        "novel_agent",
        "agent_runtime.platform",
        "novel_agent.memory.sql_store",
        "novel_agent.models.model_settings",
        "novel_agent.models.resolver",
    )
    sources = list((ROOT / "src" / "agent_runtime").rglob("*.py"))
    assert sources
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            assert not any(
                module == prefix or module.startswith(prefix + ".") for module in modules for prefix in forbidden
            ), path


def test_dependencies_without_runtime_scope_fail_before_opening_local_storage(tmp_path, monkeypatch):
    from agent_runtime.execution.models import get_llm
    from agent_runtime.workflows.novel.graph.nodes import _novel_store
    from agent_runtime.workflows.novel.memory.access import NovelMemory

    monkeypatch.chdir(tmp_path)
    for factory in [_novel_store, lambda: NovelMemory("project"), get_llm]:
        with pytest.raises(RuntimeError, match="scope"):
            factory()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("folder", ["domain", "execution/context.py", "execution/errors.py"])
def test_core_contracts_do_not_depend_on_services_or_infrastructure(folder):
    source = ROOT / "src" / "agent_runtime" / folder
    paths = list(source.rglob("*.py")) if source.is_dir() else [source]
    forbidden = (
        "agent_runtime.infrastructure",
        "agent_runtime.workflows",
        "agent_runtime.api",
        "agent_runtime.entrypoints",
        "agent_runtime.execution.worker",
        "agent_runtime.execution.handlers",
    )
    assert paths
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            )
            assert not any(name == prefix or name.startswith(prefix + ".") for name in names for prefix in forbidden), (
                path
            )


def test_root_cli_exposes_remote_platform_without_old_local_options():
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--help"], cwd=ROOT, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert "--platform-url" in result.stdout
    assert "--resume" in result.stdout
