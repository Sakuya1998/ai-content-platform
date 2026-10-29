import pytest
from dotenv import dotenv_values
from tools.migrate.platform_config_upgrade import upgrade


def test_upgrade_preserves_credentials_and_replaces_only_platform_settings(tmp_path):
    control = tmp_path / "control.env"
    runtime = tmp_path / "runtime.env"
    control.write_text(
        "CONTROL_DATABASE_URL=postgresql://control:secret@db/control\n"
        "CONTROL_PLANE_OBJECT_STORAGE_PROVIDERS_0_ACCESS_KEY=private-key\n"
        "CONTROL_PLANE_MQ_BROKERS_0_NAMESERVER=mq:9876\n"
        "CONTROL_PLANE_RUNTIME_URL=http://runtime:8000\n"
    )
    runtime.write_text(
        "RUNTIME_DATABASE_URL=postgresql://runtime:secret@db/runtime\nRUNTIME_ROCKETMQ_NAMESRV_ADDR=mq:9876\n"
        "RUNTIME_CONTROL_API_URL=http://control:8080\nRUNTIME_INTERNAL_SERVICE_TOKEN=retired-token\n"
    )
    result = upgrade(control, runtime, environment="staging", proxy_endpoint="proxy:8081", tls=False)
    c, r = dotenv_values(control), dotenv_values(runtime)
    assert c["CONTROL_DATABASE_URL"] == "postgresql://control:secret@db/control"
    assert c["CONTROL_PLANE_OBJECT_STORAGE_PROVIDERS_0_ACCESS_KEY"] == "private-key"
    assert "CONTROL_PLANE_MQ_BROKERS_0_NAMESERVER" not in c
    assert "RUNTIME_ROCKETMQ_NAMESRV_ADDR" not in r
    assert c["CONTROL_PLANE_MQ_TOPICS_TASKS"] == r["RUNTIME_ROCKETMQ_TASK_TOPIC"] == "agent-staging-runtime-tasks"
    assert c["CONTROL_PLANE_MQ_ENDPOINT"] == r["RUNTIME_ROCKETMQ_ENDPOINT"] == "proxy:8081"
    assert "RUNTIME_INTERNAL_SERVICE_TOKEN" not in r
    assert "RUNTIME_CONTROL_API_URL" not in r and "CONTROL_PLANE_RUNTIME_URL" not in c
    execution_key = c["CONTROL_PLANE_SECURITY_EXECUTION_ENCRYPTION_KEY"]
    assert execution_key == r["RUNTIME_EXECUTION_ENCRYPTION_KEY"]
    assert len(bytes.fromhex(execution_key)) == 32
    assert execution_key != c["CONTROL_PLANE_SECURITY_MODEL_ENCRYPTION_KEY"]
    assert "private-key" not in str(result) and "postgresql" not in str(result)


def test_missing_proxy_does_not_invent_an_endpoint(tmp_path):
    control, runtime = tmp_path / "control.env", tmp_path / "runtime.env"
    control.write_text("CONTROL_PLANE_MQ_BROKERS_0_NAMESERVER=mq:9876\n")
    runtime.write_text("RUNTIME_ROCKETMQ_NAMESRV_ADDR=mq:9876\n")
    result = upgrade(control, runtime, environment="staging")
    assert "proxy_endpoint" in result["missing"]
    assert not dotenv_values(runtime).get("RUNTIME_ROCKETMQ_ENDPOINT")
    assert dotenv_values(control)["CONTROL_PLANE_MQ_ENABLED"] == "false"


@pytest.mark.parametrize("configured_service", ["control", "runtime"])
def test_execution_encryption_key_is_preserved_and_shared(tmp_path, configured_service):
    control, runtime = tmp_path / "control.env", tmp_path / "runtime.env"
    control.write_text("CONTROL_PLANE_SECURITY_INTERNAL_TOKEN=tooling-token\n")
    runtime.write_text("")
    key = "a3" * 32
    target = control if configured_service == "control" else runtime
    setting = (
        "CONTROL_PLANE_SECURITY_EXECUTION_ENCRYPTION_KEY"
        if configured_service == "control"
        else "RUNTIME_EXECUTION_ENCRYPTION_KEY"
    )
    with target.open("a") as stream:
        stream.write(f"{setting}={key}\n")
    upgrade(control, runtime)
    upgrade(control, runtime)
    c, r = dotenv_values(control), dotenv_values(runtime)
    assert c["CONTROL_PLANE_SECURITY_EXECUTION_ENCRYPTION_KEY"] == r["RUNTIME_EXECUTION_ENCRYPTION_KEY"] == key
    assert c["CONTROL_PLANE_SECURITY_INTERNAL_TOKEN"] == "tooling-token"


@pytest.mark.parametrize("runtime_key", ["b4" * 32, "invalid"])
def test_mismatched_or_invalid_execution_keys_leave_configuration_unchanged(tmp_path, runtime_key):
    control, runtime = tmp_path / "control.env", tmp_path / "runtime.env"
    control.write_text("CONTROL_PLANE_SECURITY_EXECUTION_ENCRYPTION_KEY=" + "a3" * 32 + "\n")
    runtime.write_text("RUNTIME_EXECUTION_ENCRYPTION_KEY=" + runtime_key + "\n")
    before = control.read_bytes(), runtime.read_bytes()
    with pytest.raises(ValueError, match="execution encryption key"):
        upgrade(control, runtime)
    assert (control.read_bytes(), runtime.read_bytes()) == before
