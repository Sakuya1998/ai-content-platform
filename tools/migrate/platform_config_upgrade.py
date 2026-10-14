"""Upgrade split platform dotenv files without printing or replacing infrastructure secrets."""

import argparse
import base64
import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values, set_key, unset_key


def upgrade(control_path, runtime_path, *, environment="staging", proxy_endpoint=None, tls=None):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", environment):
        raise ValueError("invalid environment")
    control_path, runtime_path = Path(control_path), Path(runtime_path)
    if not control_path.is_file() or not runtime_path.is_file():
        raise ValueError("both service configuration files must already exist")
    control, runtime = dotenv_values(control_path), dotenv_values(runtime_path)
    endpoint = (
        proxy_endpoint or control.get("CONTROL_PLANE_MQ_ENDPOINT") or runtime.get("RUNTIME_ROCKETMQ_ENDPOINT") or ""
    )
    if endpoint and ("/" in endpoint or ":" not in endpoint):
        raise ValueError("Proxy endpoint must be host:port")
    if tls is None:
        tls = (
            str(
                control.get("CONTROL_PLANE_MQ_TLS_ENABLED", runtime.get("RUNTIME_ROCKETMQ_TLS_ENABLED", "false"))
            ).lower()
            == "true"
        )
    token = control.get("CONTROL_PLANE_SECURITY_INTERNAL_TOKEN", "")
    if not token or token.startswith("change-"):
        token = secrets.token_urlsafe(32)
    control_key = control.get("CONTROL_PLANE_SECURITY_EXECUTION_ENCRYPTION_KEY") or ""
    runtime_key = runtime.get("RUNTIME_EXECUTION_ENCRYPTION_KEY") or ""
    for key in (control_key, runtime_key):
        if key and not re.fullmatch(r"[a-fA-F0-9]{64}", key):
            raise ValueError("execution encryption key must contain 32 bytes encoded as hex")
    if control_key and runtime_key and not secrets.compare_digest(control_key.lower(), runtime_key.lower()):
        raise ValueError("Control and Runtime execution encryption keys do not match")
    execution_key = control_key or runtime_key or secrets.token_hex(32)
    prefix = f"agent-{environment}-"
    updates_c = {
        "CONTROL_PLANE_SERVER_ENVIRONMENT": environment,
        "CONTROL_PLANE_MQ_ENABLED": "true" if endpoint else "false",
        "CONTROL_PLANE_MQ_ENDPOINT": endpoint,
        "CONTROL_PLANE_MQ_TLS_ENABLED": str(tls).lower(),
        "CONTROL_PLANE_MQ_TOPICS_TASKS": prefix + "runtime-tasks",
        "CONTROL_PLANE_MQ_TOPICS_CONTROL": prefix + "runtime-control",
        "CONTROL_PLANE_MQ_TOPICS_EVENTS": prefix + "runtime-events",
        "CONTROL_PLANE_MQ_TOPICS_MEMORY": prefix + "memory-tasks",
        "CONTROL_PLANE_MQ_GROUPS_PROJECTION": prefix + "control-projection",
        "CONTROL_PLANE_SECURITY_INTERNAL_TOKEN": token,
        "CONTROL_PLANE_SECURITY_EXECUTION_ENCRYPTION_KEY": execution_key,
        "CONTROL_PLANE_OBJECT_STORAGE_PROVIDERS_0_BUCKET": "agent-artifacts",
        "CONTROL_PLANE_REDIS_DB": "0",
    }
    updates_r = {
        "RUNTIME_ENVIRONMENT": environment,
        "RUNTIME_ROCKETMQ_ENDPOINT": endpoint,
        "RUNTIME_ROCKETMQ_TLS_ENABLED": str(tls).lower(),
        "RUNTIME_ROCKETMQ_TASK_TOPIC": prefix + "runtime-tasks",
        "RUNTIME_ROCKETMQ_CONTROL_TOPIC": prefix + "runtime-control",
        "RUNTIME_ROCKETMQ_EVENT_TOPIC": prefix + "runtime-events",
        "RUNTIME_ROCKETMQ_MEMORY_TOPIC": prefix + "memory-tasks",
        "RUNTIME_ROCKETMQ_DISPATCH_GROUP": prefix + "runtime-dispatch",
        "RUNTIME_ROCKETMQ_CONTROL_GROUP": prefix + "runtime-control",
        "RUNTIME_ROCKETMQ_MEMORY_GROUP": prefix + "memory-worker",
        "RUNTIME_ROCKETMQ_MEMORY_SCHEDULER_GROUP": prefix + "memory-scheduler",
        "RUNTIME_EXECUTION_ENCRYPTION_KEY": execution_key,
        "RUNTIME_WORKER_HANDLER": "agent_runtime.execution.handlers:generate_content",
        "RUNTIME_MEMORY_HANDLER": "agent_runtime.execution.handlers:refresh_memory",
        "RUNTIME_OBJECT_STORAGE_BUCKET": "agent-artifacts",
    }
    updates_c.update(
        {
            "CONTROL_PLANE_SECURITY_AUTH_ENABLED": control.get("CONTROL_PLANE_SECURITY_AUTH_ENABLED") or "true",
            "CONTROL_PLANE_SECURITY_COOKIE_SECURE": control.get("CONTROL_PLANE_SECURITY_COOKIE_SECURE")
            or ("true" if environment == "prod" else "false"),
            "CONTROL_PLANE_SECURITY_MODEL_ENCRYPTION_KEY": control.get("CONTROL_PLANE_SECURITY_MODEL_ENCRYPTION_KEY")
            or base64.b64encode(secrets.token_bytes(32)).decode(),
            "CONTROL_PLANE_SECURITY_ALLOWED_ORIGINS": control.get("CONTROL_PLANE_SECURITY_ALLOWED_ORIGINS")
            or "http://localhost:5173,http://127.0.0.1:5173",
        }
    )
    defaults = {
        "RUNTIME_LLM_PROVIDER": "openai_compatible",
        "RUNTIME_LLM_MODEL": "",
        "RUNTIME_LLM_BASE_URL": "",
        "RUNTIME_LLM_API_KEY": "",
        "RUNTIME_ANALYSIS_MODEL": "",
        "RUNTIME_EMBEDDING_MODEL": "",
        "RUNTIME_EMBEDDING_BASE_URL": "",
        "RUNTIME_EMBEDDING_API_KEY": "",
        "RUNTIME_CONTEXT_TOKEN_BUDGET": "12000",
    }
    updates_r.update({key: runtime.get(key) or value for key, value in defaults.items()})
    if runtime.get("RUNTIME_REDIS_URL"):
        url = urlsplit(runtime["RUNTIME_REDIS_URL"])
        updates_r["RUNTIME_REDIS_URL"] = urlunsplit(url._replace(path="/0"))
    missing = []
    if not endpoint:
        missing.append("proxy_endpoint")
    for key in ["RUNTIME_LLM_MODEL", "RUNTIME_LLM_API_KEY", "RUNTIME_EMBEDDING_MODEL", "RUNTIME_EMBEDDING_API_KEY"]:
        if not runtime.get(key):
            missing.append(key)
    # dotenv handles quoting and preserves untouched keys/comments; credentials
    # never appear in the report or shell command arguments.
    for path, updates in [(control_path, updates_c), (runtime_path, updates_r)]:
        for key, value in updates.items():
            set_key(path, key, value, quote_mode="always")
        for key in dotenv_values(path):
            if key in {
                "RUNTIME_ROCKETMQ_NAMESRV_ADDR",
                "RUNTIME_ROCKETMQ_COMMAND_TOPIC",
                "RUNTIME_ROCKETMQ_CONSUMER_GROUP",
                "CONTROL_PLANE_MQ_COMMAND_TOPIC",
                "CONTROL_PLANE_MQ_CONSUMER_GROUP",
                "CONTROL_PLANE_MQ_EVENT_TOPIC",
                "CONTROL_PLANE_RUNTIME_URL",
                "RUNTIME_CONTROL_API_URL",
                "RUNTIME_INTERNAL_SERVICE_TOKEN",
            } or key.endswith("_NAMESERVER"):
                unset_key(path, key)
    control_path.chmod(0o600)
    runtime_path.chmod(0o600)
    return {"updated": [control_path.name, runtime_path.name], "environment": environment, "missing": missing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-env", default=".env.control.external")
    parser.add_argument("--runtime-env", default=".env.runtime.external")
    parser.add_argument("--environment", default="staging")
    parser.add_argument("--proxy-endpoint")
    parser.add_argument("--tls", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            upgrade(
                args.control_env,
                args.runtime_env,
                environment=args.environment,
                proxy_endpoint=args.proxy_endpoint,
                tls=args.tls,
            )
        )
    )


if __name__ == "__main__":
    main()
