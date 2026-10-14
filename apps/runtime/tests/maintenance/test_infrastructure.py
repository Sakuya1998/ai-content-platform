from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("suffix", ["example", "external.example"])
def test_service_templates_share_bucket_redis_db_and_mq_topics(suffix):
    control = dotenv_values(ROOT / f".env.control.{suffix}")
    runtime = dotenv_values(ROOT / f".env.runtime.{suffix}")
    assert control["CONTROL_PLANE_OBJECT_STORAGE_PROVIDERS_0_BUCKET"] == runtime["RUNTIME_OBJECT_STORAGE_BUCKET"]
    assert runtime["RUNTIME_OBJECT_STORAGE_BUCKET"] == "agent-artifacts"
    assert "CONTROL_PLANE_OBJECT_STORAGE_PROVIDERS_0_PREFIX" not in control
    assert "RUNTIME_OBJECT_STORAGE_PREFIX" not in runtime
    assert control["CONTROL_PLANE_REDIS_DB"] == urlparse(runtime["RUNTIME_REDIS_URL"]).path.lstrip("/") == "0"
    assert control["CONTROL_PLANE_MQ_ENDPOINT"] == runtime["RUNTIME_ROCKETMQ_ENDPOINT"]
    for name, runtime_name in [("TASKS", "TASK"), ("CONTROL", "CONTROL"), ("EVENTS", "EVENT"), ("MEMORY", "MEMORY")]:
        assert control[f"CONTROL_PLANE_MQ_TOPICS_{name}"] == runtime[f"RUNTIME_ROCKETMQ_{runtime_name}_TOPIC"]
    assert control["CONTROL_PLANE_MQ_GROUPS_PROJECTION"] != runtime["RUNTIME_ROCKETMQ_DISPATCH_GROUP"]
    assert control["CONTROL_PLANE_REDIS_KEY_PREFIX"] != runtime["RUNTIME_REDIS_KEY_PREFIX"]


def test_local_services_depend_on_proxy_and_do_not_expose_nameserver():
    services = yaml.safe_load((ROOT / "docker-compose.platform.yml").read_text())["services"]
    for name in ("control-plane", "runtime"):
        assert "rocketmq-proxy" in services[name]["depends_on"]
        assert "rocketmq-namesrv" not in services[name]["depends_on"]
    assert "ports" not in services["rocketmq-namesrv"]
    assert services["rocketmq-proxy"]["command"][1] == "mqproxy"
