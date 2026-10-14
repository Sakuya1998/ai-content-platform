from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_migration_matches_external_runtime_identifiers_and_fencing_contract():
    migration = (ROOT / "migrations" / "runtime" / "001_init.sql").read_text(encoding="utf-8")

    assert "id text PRIMARY KEY" in migration
    assert "command_id text PRIMARY KEY" in migration
    assert "fencing_token bigint NOT NULL DEFAULT 0" in migration
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "command_inbox_workspace_policy" in migration
    assert "runtime_event_outbox_workspace_policy" in migration
    assert "tool_calls_workspace_policy" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration


def test_runtime_service_documented_database_mode_is_present():
    source = (ROOT / "src" / "agent_runtime" / "api" / "app.py").read_text(encoding="utf-8")
    settings = (ROOT / "src" / "agent_runtime" / "settings.py").read_text(encoding="utf-8")

    assert "database_url" in settings
    assert "PostgresRuntimeJobStore" in source
    assert "status_code=503" in source
