"""Initialize immutable platform creation policy defaults and explicit workflow bindings."""

import argparse
import json
from contextlib import contextmanager
from uuid import NAMESPACE_URL, uuid5

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.types.json import Jsonb

from agent_runtime.domain.policies import CreationPolicy, policy_hash

try:
    from scripts.platform_seed_catalog import SEED_ROOT
except ModuleNotFoundError:
    from platform_seed_catalog import SEED_ROOT

TABLES = (
    "creation_policy_sets",
    "creation_policy_versions",
    "workflow_creation_policy_bindings",
    "workflow_creation_policy_binding_history",
)


def identity(key):
    return str(uuid5(NAMESPACE_URL, "agent-platform:builtin-policy:" + key))


def load_policy_catalog():
    catalog = json.loads((SEED_ROOT / "creation-policies" / "catalog.v1.json").read_text())
    if catalog.get("schema_version") != "creation-policy-seed@1":
        raise ValueError("invalid policy seed")
    for value in catalog["definitions"]:
        CreationPolicy.model_validate(value["payload"])
    return catalog


@contextmanager
def seed_scope(connection):
    role = connection.execute("SELECT oid,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
    privileged = role[1] or role[2]
    if not privileged:
        owners = connection.execute(
            "SELECT relowner FROM pg_class WHERE relnamespace='public'::regnamespace AND relname=ANY(%s)",
            (list(TABLES),),
        ).fetchall()
        if len(owners) != len(TABLES) or any(row[0] != role[0] for row in owners):
            raise ValueError("policy seed requires migration owner or administrator")
        for table in TABLES:
            connection.execute(sql.SQL("ALTER TABLE {} NO FORCE ROW LEVEL SECURITY").format(sql.Identifier(table)))
    try:
        yield
    finally:
        if not privileged:
            for table in TABLES:
                connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(sql.Identifier(table)))


def seed(url):
    catalog = load_policy_catalog()
    versions = {}
    changes = {"policy_versions_created": 0, "bindings_updated": 0}
    with psycopg.connect(url) as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('agent-platform:policy-seed'))")
        with seed_scope(connection):
            for value in catalog["definitions"]:
                key = value["key"]
                digest = policy_hash(value["payload"])
                policy_id = identity(key)
                connection.execute(
                    "INSERT INTO creation_policy_sets(id,key,name) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                    (policy_id, key, value["name"]),
                )
                existing = connection.execute(
                    "SELECT id FROM creation_policy_versions WHERE policy_set_id=%s "
                    "AND content_hash=%s AND status='published'",
                    (policy_id, digest),
                ).fetchone()
                if existing:
                    versions[key] = str(existing[0])
                    continue
                number = connection.execute(
                    "SELECT coalesce(max(version),0)+1 FROM creation_policy_versions WHERE policy_set_id=%s",
                    (policy_id,),
                ).fetchone()[0]
                version = identity(key + ":" + digest)
                connection.execute(
                    "INSERT INTO creation_policy_versions(id,policy_set_id,version,status,payload,content_hash) "
                    "VALUES(%s,%s,%s,'published',%s,%s)",
                    (version, policy_id, number, Jsonb(value["payload"]), digest),
                )
                connection.execute(
                    "UPDATE creation_policy_sets SET current_version=%s WHERE id=%s", (number, policy_id)
                )
                versions[key] = version
                changes["policy_versions_created"] += 1
            for workflow, key in catalog["workflows"].items():
                current = connection.execute(
                    "SELECT policy_version_id FROM workflow_creation_policy_bindings "
                    "WHERE workspace_id IS NULL AND workflow=%s",
                    (workflow,),
                ).fetchone()
                if current and str(current[0]) == versions[key]:
                    continue
                connection.execute(
                    """INSERT INTO workflow_creation_policy_bindings(id,workflow,policy_version_id) VALUES(%s,%s,%s)
                    ON CONFLICT(workspace_id,workflow) DO UPDATE
                    SET policy_version_id=excluded.policy_version_id,created_at=now()""",
                    (identity("binding:" + workflow), workflow, versions[key]),
                )
                changes["bindings_updated"] += 1
    return {**changes, "policies": len(versions), "workflows": len(catalog["workflows"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-env", default=".env.control.external")
    args = parser.parse_args()
    url = dotenv_values(args.control_env).get("CONTROL_DATABASE_URL")
    if not url:
        parser.error("Control database URL required")
    try:
        print(json.dumps(seed(url)))
    except Exception as exc:
        print("policy seed failed: " + type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
