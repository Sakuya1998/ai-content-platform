"""Validate Control backfill and retirement against populated source tables."""

import hashlib
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from scripts.platform_migrate import apply_database, migrations


def test_control_backfill_preserves_versions_keys_transfers_and_archive():
    admin_url = os.getenv("MQ_TEST_ADMIN_DATABASE_URL")
    control_url = os.getenv("MQ_TEST_CONTROL_DATABASE_URL")
    if not admin_url or not control_url:
        pytest.skip("disposable PostgreSQL admin and Control URLs required")
    options = conninfo_to_dict(control_url)
    name = "test_control_migration_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(options["user"]))
        )
    try:
        options["dbname"] = name
        url = make_conninfo(**options)
        admin_options = conninfo_to_dict(admin_url)
        admin_options["dbname"] = name
        migration_url = make_conninfo(**admin_options)
        selected = tuple(item for item in migrations() if item.component == "control" and item.name < "017")
        apply_database(url, "control", selected)
        scope, project, document, record = (str(uuid4()) for _ in range(4))
        payload = {"id": "chapter-1", "chapter_number": 1, "version_number": 1, "content": "Preserved manuscript"}
        body_hash = hashlib.sha256(b"preserved").hexdigest()
        transfer_body = b"encrypted-or-binary\x00\xff"
        with psycopg.connect(url) as connection:
            connection.execute("INSERT INTO workspaces(id,name) VALUES(%s,'Migration test')", (scope,))
            connection.execute("SELECT set_config('app.workspace_id',%s,true)", (scope,))
            connection.execute(
                "INSERT INTO projects(id,workspace_id,project_type,name) VALUES(%s,%s,'novel','Book')", (project, scope)
            )
            connection.execute(
                "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES(%s,%s,%s,'novel','Book')",
                (document, scope, project),
            )
            connection.execute(
                "INSERT INTO novels(id,workspace_id,document_id,input,state,state_hash) VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    project,
                    scope,
                    document,
                    Jsonb({"title": "Book", "creative_brief": {"notes": "Brief"}}),
                    Jsonb({"chapters": [{"content": "Preserved manuscript"}]}),
                    body_hash,
                ),
            )
            connection.execute(
                "INSERT INTO novel_brief_versions(workspace_id,novel_id,version,payload) VALUES(%s,%s,1,%s)",
                (scope, project, Jsonb({"notes": "Brief"})),
            )
            connection.execute(
                "INSERT INTO novel_records(id,workspace_id,novel_id,kind,record_key,chapter_number,"
                "version_number,payload,content_hash) VALUES(%s,%s,%s,'chapter','chapter-1',1,1,%s,%s)",
                (record, scope, project, Jsonb(payload), body_hash),
            )
            connection.execute(
                "INSERT INTO novel_request_keys(workspace_id,key,fingerprint,novel_id) VALUES(%s,'create',%s,%s)",
                (scope, body_hash, project),
            )
            connection.execute(
                "INSERT INTO novel_transfers(workspace_id,novel_id,status,format,result,content_hash) "
                "VALUES(%s,%s,'completed','backup',%s,%s)",
                (scope, project, transfer_body, body_hash),
            )
        forward = tuple(item for item in migrations() if item.component == "control" and item.name >= "017")
        apply_database(migration_url, "control", forward, application_role=options["user"])
        with psycopg.connect(url) as connection:
            connection.execute("SELECT set_config('app.workspace_id',%s,true)", (scope,))
            assert (
                connection.execute(
                    "SELECT count(*) FROM resource_migration_validation "
                    "WHERE source_count=target_count AND source_count=1"
                ).fetchone()[0]
                == 5
            )
            assert (
                connection.execute("SELECT payload FROM project_content_versions WHERE id=%s", (record,)).fetchone()[0]
                == payload
            )
            assert connection.execute(
                "SELECT payload FROM project_brief_versions WHERE project_id=%s", (project,)
            ).fetchone()[0] == {"notes": "Brief"}
            assert (
                connection.execute("SELECT result FROM project_transfers WHERE project_id=%s", (project,)).fetchone()[0]
                == transfer_body
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM legacy_resource_archive WHERE workspace_id=%s", (scope,)
                ).fetchone()[0]
                == 5
            )
            assert connection.execute("SELECT to_regclass('novels'),to_regclass('novel_records')").fetchone() == (
                None,
                None,
            )
            connection.execute("SELECT set_config('app.workspace_id',%s,true)", (str(uuid4()),))
            assert connection.execute("SELECT count(*) FROM project_projections").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM legacy_resource_archive").fetchone()[0] == 0
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
