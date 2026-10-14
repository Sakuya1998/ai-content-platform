import hashlib
import json
import sqlite3

import pytest

from scripts.platform_legacy_export import export_legacy, stable_id


def test_export_reads_sqlite_without_mutation_and_keeps_stable_id_mapping(tmp_path):
    source = tmp_path / "legacy.db"
    with sqlite3.connect(source) as connection:
        connection.executescript("""CREATE TABLE tenants(id text,name text);
            CREATE TABLE users(id text,tenant_id text,password_hash text);
            CREATE TABLE novels(id text,tenant_id text,title text,creative_brief_json text);
            CREATE TABLE chapters(novel_id text,chapter_number integer,content text);
            CREATE TABLE sessions(id text,token_hash text);
            INSERT INTO tenants VALUES('tenant-1','Workspace');
            INSERT INTO users VALUES('user-1','tenant-1','scrypt$legacy');
            INSERT INTO novels VALUES('novel-1','tenant-1','Book','{}');
            INSERT INTO chapters VALUES('novel-1',1,'First chapter');
            INSERT INTO sessions VALUES('session-1','secret-session');""")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "migration.json"
    report = export_legacy(source, output)
    bundle = json.loads(output.read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert bundle["tables"]["chapters"][0]["content"] == "First chapter"
    assert "sessions" not in bundle["tables"]
    assert bundle["id_map"]["workspace"]["tenant-1"] == stable_id("workspace", "tenant-1")
    assert report["counts"]["novels"] == 1
    assert "secret-session" not in output.read_text()
    assert output.stat().st_mode & 0o777 == 0o600


def test_export_refuses_existing_destination_and_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_legacy(tmp_path / "missing.db", tmp_path / "out.json")
    source = tmp_path / "legacy.db"
    sqlite3.connect(source).close()
    output = tmp_path / "existing.json"
    output.write_text("keep")
    with pytest.raises(FileExistsError):
        export_legacy(source, output)
    assert output.read_text() == "keep"
