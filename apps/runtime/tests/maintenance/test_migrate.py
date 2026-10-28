import sys
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools" / "migrate"))

from tools.migrate.platform_migrate import migration_connection, migrations  # noqa: E402


def test_platform_migrations_are_ordered_and_exist():
    items = migrations()
    assert [(item.component, item.name) for item in items] == [
        ("control", "001_init"),
        ("control", "002_canonical"),
        ("control", "003_messaging"),
        ("control", "004_generation"),
        ("control", "005_identity_governance"),
        ("control", "006_novel_management"),
        ("control", "007_operations"),
        ("control", "008_benchmarks"),
        ("control", "009_prompt_catalog"),
        ("control", "010_prompt_catalog_identity"),
        ("control", "011_creation_policies"),
        ("control", "012_video_models"),
        ("control", "013_image_speech_models"),
        ("control", "014_timeline_workflows"),
        ("control", "015_production_runs"),
        ("control", "016_music_sound_effect_models"),
        ("control", "017_generic_resources"),
        ("control", "018_resource_archive"),
        ("control", "019_retire_novel_state"),
        ("control", "020_project_workflow_concurrency"),
        ("control", "021_workflow_catalog"),
        ("control", "022_document_title_identity"),
        ("control", "023_evaluation_management"),
        ("runtime", "001_init"),
        ("runtime", "002_runtime_defaults"),
        ("runtime", "003_artifacts"),
        ("runtime", "004_messaging"),
        ("runtime", "005_execution"),
        ("runtime", "006_novel_workflows"),
        ("runtime", "007_knowledge_operations"),
        ("runtime", "008_shared_canon"),
        ("runtime", "009_media_tasks"),
        ("runtime", "010_generated_media"),
        ("runtime", "011_composition_runs"),
        ("runtime", "012_music_sound_effect_media"),
        ("runtime", "013_generic_workflow_state"),
        ("runtime", "014_workflow_memory_identity"),
        ("runtime", "015_legacy_resource_archive"),
        ("runtime", "016_retire_novel_state"),
    ]
    assert all(item.path.is_file() for item in items)
    assert all(len(item.checksum) == 64 for item in items)


def test_evaluation_management_migration_adds_workspace_scoped_policy_storage():
    items = [item for item in migrations() if item.component == "control"]
    assert any(item.name == "023_evaluation_management" for item in items)
    migration = next(item for item in items if item.name == "023_evaluation_management")
    migration = migration.path.read_text(encoding="utf-8")
    assert "ADD COLUMN job_id text" in migration
    assert "CREATE TABLE evaluation_policies" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "current_setting('app.workspace_id', true)" in migration
    assert "evaluation_dimensions" in migration


def test_control_migration_is_owned_by_root_migrations():
    item = next(item for item in migrations() if item.name == "002_canonical")
    assert Path("infra/migrations/migrations/control/002_canonical.sql") == item.path.relative_to(REPO_ROOT)
    assert not Path("go-control-plane/migrations/control/002_control_plane.sql").exists()
    assert not Path("go-control-plane/migrations").exists()


def test_bootstrap_sql_is_at_repository_root():
    bootstrap = REPO_ROOT / "infra/migrations/migrations/bootstrap.sql"
    assert bootstrap.is_file()
    text = bootstrap.read_text(encoding="utf-8")
    assert "CREATE ROLE" in text
    assert "CREATE DATABASE" in text
    assert "CREATE EXTENSION IF NOT EXISTS vector" in text


def test_migration_admin_targets_application_database_and_preserves_admin_connection_options():
    connection, role = migration_connection(
        "postgresql://control:app-secret@db.example/control?sslmode=require",
        "postgresql://migration:admin-secret@db.example/postgres?sslmode=verify-full&connect_timeout=5",
    )
    assert conninfo_to_dict(connection) == {
        "user": "migration",
        "password": "admin-secret",
        "dbname": "control",
        "host": "db.example",
        "sslmode": "verify-full",
        "connect_timeout": "5",
    }
    assert role == "control"
