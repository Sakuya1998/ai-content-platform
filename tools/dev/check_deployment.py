"""Run offline checks for the split Go Control Plane and Python Runtime deployment."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load((ROOT / "docker-compose.platform.yml").read_text(encoding="utf-8"))
    external = yaml.safe_load((ROOT / "docker-compose.external.yml").read_text(encoding="utf-8"))
    require("uv sync --locked --no-dev" in dockerfile, "Runtime Dockerfile must use the lockfile")
    require(
        "uv export --frozen --no-dev --no-emit-project --format requirements-txt" in dockerfile,
        "mirror build must use uv.lock",
    )
    require("--require-hashes" in dockerfile, "mirror build must retain dependency hashes")
    require("agent_runtime.api.app:app" in dockerfile, "Dockerfile must start Python Runtime")
    require("USER appuser" in dockerfile, "Runtime image must be non-root")
    require("/readyz" in dockerfile, "Runtime image must have a readiness check")
    require("control-plane" in compose.get("services", {}), "platform Compose must include Go Control Plane")
    require("runtime" in compose.get("services", {}), "platform Compose must include Python Runtime")
    require("rocketmq-proxy" in compose.get("services", {}), "platform Compose must include RocketMQ Proxy")
    require("control-plane" in external.get("services", {}), "external Compose must include Go Control Plane")
    require("runtime" in external.get("services", {}), "external Compose must include Python Runtime")
    require(
        (ROOT / "migrations" / "control" / "010_prompt_catalog_identity.sql").is_file(), "Control migrations incomplete"
    )
    require(
        (ROOT / "migrations" / "runtime" / "007_knowledge_operations.sql").is_file(), "Runtime migrations incomplete"
    )
    print("deployment checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
