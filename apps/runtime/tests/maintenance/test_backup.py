import os
from uuid import uuid4

import pytest
from tools.migrate.platform_backup import database_environment, make_archive, open_archive


def test_backup_requires_encryption_and_detects_modified_content():
    with pytest.raises(ValueError):
        make_archive({"control.dump": b"database"}, "")
    encrypted = make_archive({"control.dump": b"database", "runtime.dump": b"checkpoint"}, "test-passphrase")
    assert b"database" not in encrypted
    result = open_archive(encrypted, "test-passphrase")
    assert result["control.dump"] == b"database"
    with pytest.raises(ValueError):
        open_archive(encrypted[:-1] + bytes([encrypted[-1] ^ 1]), "test-passphrase")


def test_database_command_environment_keeps_password_out_of_process_arguments():
    values = database_environment("postgresql://control:private%40secret@database:5433/control?sslmode=require")
    assert values["PGPASSWORD"] == "private@secret"
    assert values["PGDATABASE"] == "control"
    assert values["PGSSLMODE"] == "require"


def test_real_pg_backup_restore_recovers_after_second_database_failure(tmp_path, monkeypatch):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from tools.migrate import platform_backup

    from agent_runtime.settings import RuntimeSettings

    admin = os.getenv("BACKUP_TEST_ADMIN_URL")
    if not admin:
        pytest.skip("BACKUP_TEST_ADMIN_URL is not configured")
    root = conninfo_to_dict(admin)
    prefix = "restore_" + uuid4().hex[:10]
    source_c = make_conninfo(**{**root, "dbname": "control"})
    source_r = make_conninfo(**{**root, "dbname": "runtime"})
    target_c = make_conninfo(**{**root, "dbname": prefix + "_c"})
    target_r = make_conninfo(**{**root, "dbname": prefix + "_r"})
    for name in [prefix + "_c", prefix + "_r"]:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = RuntimeSettings(_env_file=None, database_url=source_r)
    output = tmp_path / "backup.enc"
    platform_backup.create(source_c, settings, output, "test-backup-password", artifacts=False)
    files = platform_backup.open_archive(output.read_bytes(), "test-backup-password")
    real = platform_backup.run_database
    failed = False

    def run(tool, url, args, data=None):
        nonlocal failed
        if tool == "pg_restore" and url == target_r and not failed:
            failed = True
            raise ConnectionError("test restore interruption")
        return real(tool, url, args, data)

    monkeypatch.setattr(platform_backup, "run_database", run)
    restored_settings = settings.model_copy(update={"database_url": target_r})
    with pytest.raises(ConnectionError):
        platform_backup.restore(files, target_c, restored_settings)
    platform_backup.restore(files, target_c, restored_settings)
    platform_backup.restore(files, target_c, restored_settings)
    with psycopg.connect(target_c) as connection:
        assert connection.execute("SELECT count(*) FROM platform_migrations").fetchone()[0] >= 8
        assert connection.execute("SELECT phase FROM platform_restore_progress").fetchone()[0] == "complete"
    with psycopg.connect(target_r) as connection:
        assert connection.execute("SELECT count(*) FROM platform_migrations").fetchone()[0] >= 7
