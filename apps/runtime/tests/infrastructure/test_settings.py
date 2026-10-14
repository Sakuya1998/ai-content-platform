from agent_runtime.settings import RuntimeSettings


def test_runtime_settings_use_runtime_prefix_and_typed_values(monkeypatch):
    monkeypatch.setenv("RUNTIME_DATABASE_URL", "postgresql://runtime")
    monkeypatch.setenv("RUNTIME_PORT", "8123")
    monkeypatch.setenv("RUNTIME_JOB_LEASE_SECONDS", "90")
    settings = RuntimeSettings(_env_file=None)

    assert settings.database_url == "postgresql://runtime"
    assert settings.port == 8123
    assert settings.job_lease_seconds == 90
    assert settings.configured_database is True


def test_runtime_proxy_and_shared_infrastructure_settings(monkeypatch):
    monkeypatch.setenv("RUNTIME_ROCKETMQ_ENDPOINT", "proxy.example.com:8081")
    monkeypatch.setenv("RUNTIME_ROCKETMQ_TLS_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_ROCKETMQ_ACCESS_KEY", "test-ak")
    monkeypatch.setenv("RUNTIME_ROCKETMQ_SECRET_KEY", "test-sk")
    settings = RuntimeSettings(_env_file=None)
    assert settings.rocketmq_endpoint == "proxy.example.com:8081"
    assert settings.rocketmq_tls_enabled
    assert settings.rocketmq_secret_key.get_secret_value() == "test-sk"
    assert settings.redis_key_prefix == "runtime:"
    assert settings.object_storage_bucket == "agent-artifacts"


def test_execution_encryption_key_is_loaded_as_a_secret(monkeypatch):
    monkeypatch.setenv("RUNTIME_EXECUTION_ENCRYPTION_KEY", "shared-envelope-key")
    settings = RuntimeSettings(_env_file=None)

    assert settings.execution_encryption_key.get_secret_value() == "shared-envelope-key"
