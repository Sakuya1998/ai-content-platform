"""Idempotently seed platform prompt versions and explicit default workflow packs."""

import argparse
import hashlib
import json
from contextlib import contextmanager
from uuid import NAMESPACE_URL, uuid5

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.types.json import Jsonb

from agent_runtime.domain.workflow_contracts import WORKFLOWS
from agent_runtime.prompts.renderer import PromptVersion

try:
    from scripts.platform_seed_catalog import load_prompt_catalog
except ModuleNotFoundError:
    from platform_seed_catalog import load_prompt_catalog

TABLES = (
    "prompt_node_contracts",
    "prompts",
    "prompt_versions",
    "prompt_packs",
    "prompt_pack_versions",
    "prompt_pack_entries",
    "workflow_prompt_bindings",
    "workflow_prompt_binding_history",
)


def identity(key):
    return str(uuid5(NAMESPACE_URL, "agent-platform:builtin-prompt:" + key))


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@contextmanager
def seed_scope(connection):
    # Migration owners can seed platform defaults. Transactional DDL takes
    # exclusive locks; no reader can observe the temporarily relaxed FORCE RLS.
    roles = connection.execute("SELECT oid,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
    if not roles:
        raise ValueError("database role unavailable")
    privileged = roles[1] or roles[2]
    if not privileged:
        rows = connection.execute(
            "SELECT relname,relowner FROM pg_class WHERE relnamespace='public'::regnamespace AND relname=ANY(%s)",
            (list(TABLES),),
        ).fetchall()
        if len(rows) != len(TABLES) or any(owner != roles[0] for name, owner in rows):
            raise ValueError("prompt seed requires the migration owner or a BYPASSRLS administrator")
        for table in TABLES:
            connection.execute(sql.SQL("ALTER TABLE {} NO FORCE ROW LEVEL SECURITY").format(sql.Identifier(table)))
    try:
        yield
    finally:
        if not privileged:
            for table in TABLES:
                connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(sql.Identifier(table)))


def seed(database_url):
    definitions = load_prompt_catalog()
    changes = {"prompts_created": 0, "prompt_versions_created": 0, "pack_versions_created": 0, "bindings_updated": 0}
    with psycopg.connect(database_url) as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('agent-platform:prompt-seed'))")
        with seed_scope(connection):
            versions = {}
            for definition in definitions:
                node = definition["node_key"]
                existing = connection.execute(
                    """SELECT variables_schema,output_schema,renderer_version FROM prompt_node_contracts
                       WHERE node_key=%s""",
                    (node,),
                ).fetchone()
                contract = (definition["variables_schema"], definition["output_schema"], definition["renderer_version"])
                if existing and tuple(existing) != contract:
                    raise ValueError("builtin prompt node contract changed; use a new versioned workflow contract")
                connection.execute(
                    """INSERT INTO prompt_node_contracts(node_key,variables_schema,output_schema,renderer_version)
                                   VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (node, Jsonb(contract[0]), Jsonb(contract[1]), contract[2]),
                )
                prompt_id = identity(definition["key"])
                created = connection.execute(
                    """INSERT INTO prompts(id,key,node_key,name) VALUES(%s,%s,%s,%s)
                                          ON CONFLICT DO NOTHING RETURNING id""",
                    (prompt_id, definition["key"], node, definition["name"]),
                ).fetchone()
                changes["prompts_created"] += bool(created)
                existing = connection.execute(
                    "SELECT id FROM prompt_versions WHERE prompt_id=%s AND content_hash=%s AND status='published'",
                    (prompt_id, definition["content_hash"]),
                ).fetchone()
                if existing:
                    versions[node] = str(existing[0])
                    continue
                version = connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM prompt_versions WHERE prompt_id=%s", (prompt_id,)
                ).fetchone()[0]
                version_id = identity(definition["key"] + ":" + definition["content_hash"])
                PromptVersion(version_id=version_id, status="published", **definition)
                connection.execute(
                    """INSERT INTO prompt_versions(id,prompt_id,version,messages,variables_schema,
                                   output_schema,renderer_version,status,content_hash)
                                   VALUES(%s,%s,%s,%s,%s,%s,%s,'published',%s)""",
                    (
                        version_id,
                        prompt_id,
                        version,
                        Jsonb(definition["messages"]),
                        Jsonb(contract[0]),
                        Jsonb(contract[1]),
                        contract[2],
                        definition["content_hash"],
                    ),
                )
                connection.execute("UPDATE prompts SET current_version=%s WHERE id=%s", (version, prompt_id))
                versions[node] = version_id
                changes["prompt_versions_created"] += 1
            for workflow, nodes in WORKFLOWS.items():
                pack_key = "builtin." + workflow
                pack_id = identity(pack_key)
                connection.execute(
                    "INSERT INTO prompt_packs(id,key,name) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                    (pack_id, pack_key, workflow),
                )
                entries = {node: versions[node] for node in nodes}
                pack_hash = digest(entries)
                existing = connection.execute(
                    "SELECT id FROM prompt_pack_versions WHERE pack_id=%s AND content_hash=%s AND status='published'",
                    (pack_id, pack_hash),
                ).fetchone()
                if existing:
                    pack_version = str(existing[0])
                else:
                    version = connection.execute(
                        "SELECT COALESCE(MAX(version),0)+1 FROM prompt_pack_versions WHERE pack_id=%s", (pack_id,)
                    ).fetchone()[0]
                    pack_version = identity(pack_key + ":" + pack_hash)
                    connection.execute(
                        """INSERT INTO prompt_pack_versions(id,pack_id,version,status,content_hash)
                           VALUES(%s,%s,%s,'published',%s)""",
                        (pack_version, pack_id, version, pack_hash),
                    )
                    for node, prompt_version in entries.items():
                        connection.execute(
                            "INSERT INTO prompt_pack_entries VALUES(%s,%s,%s)", (pack_version, node, prompt_version)
                        )
                    connection.execute("UPDATE prompt_packs SET current_version=%s WHERE id=%s", (version, pack_id))
                    changes["pack_versions_created"] += 1
                current = connection.execute(
                    "SELECT pack_version_id FROM workflow_prompt_bindings WHERE workspace_id IS NULL AND workflow=%s",
                    (workflow,),
                ).fetchone()
                if current and str(current[0]) == pack_version:
                    continue
                connection.execute(
                    """INSERT INTO workflow_prompt_bindings(id,workflow,pack_version_id) VALUES(%s,%s,%s)
                                   ON CONFLICT(workspace_id,workflow) DO UPDATE
                                   SET pack_version_id=excluded.pack_version_id,
                                   created_at=now()""",
                    (identity("binding:" + workflow), workflow, pack_version),
                )
                changes["bindings_updated"] += 1
    return {**changes, "prompt_count": len(definitions), "workflow_count": len(WORKFLOWS)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-env", default=".env.control.external")
    parser.add_argument("--admin-url-env", default="PLATFORM_PROMPT_SEED_DATABASE_URL")
    args = parser.parse_args()
    import os

    url = os.getenv(args.admin_url_env) or dotenv_values(args.control_env).get("CONTROL_DATABASE_URL")
    if not url:
        parser.error("Control database URL is required")
    try:
        print(json.dumps(seed(url)))
    except Exception as exc:
        print("prompt seed failed: " + type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
