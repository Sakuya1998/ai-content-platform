"""Re-encrypt legacy model credentials and preserve immutable resource versions."""

import base64
import os
from collections import defaultdict

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.types.json import Jsonb

from tools.migrate.platform_legacy_export import stable_id


def import_governance(connection, tables, decode, digest, report, *, legacy_key=None, encryption_key=None):
    owners = {
        row.get("tenant_id") or "tenant_local": stable_id("user", row["id"])
        for row in tables.get("users", [])
        if row.get("role") == "owner"
    }
    for row in tables.get("workspace_resources", []):
        tenant = row.get("tenant_id") or "tenant_local"
        workspace = stable_id("workspace", tenant)
        if tenant not in owners:
            continue
        connection.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
        resource = stable_id("resource", row["id"])
        version = int(row.get("version", 1))
        identity = stable_id("resource-version", str(row["id"]) + ":" + str(version))
        payload = decode(row).get("payload", {})
        connection.execute(
            """INSERT INTO governance_resources(id,workspace_id,kind,key,current_version)
               VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE
               SET current_version=GREATEST(governance_resources.current_version,excluded.current_version)""",
            (resource, workspace, row["kind"], row["key"], version),
        )
        connection.execute(
            """INSERT INTO governance_resource_versions(id,workspace_id,resource_id,version,name,
               description,payload,status,content_hash,created_by)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                identity,
                workspace,
                resource,
                version,
                row.get("name") or row["key"],
                row.get("description", ""),
                Jsonb(payload),
                row.get("status", "draft"),
                digest(payload),
                owners[tenant],
            ),
        )
    profiles = {}
    for row in tables.get("model_profiles", []):
        tenant = row.get("tenant_id") or "tenant_local"
        workspace = stable_id("workspace", tenant)
        connection.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
        identity, version = stable_id("model-profile", row["id"]), stable_id("model-profile-version", row["id"])
        value = row.get("api_key_encrypted", {"$binary": ""})
        raw = base64.b64decode(value["$binary"]) if isinstance(value, dict) else value or b""
        encrypted = b""
        if raw and legacy_key and encryption_key:
            secret = Fernet(legacy_key).decrypt(raw)
            try:
                key = bytes.fromhex(encryption_key)
            except ValueError:
                key = base64.b64decode(encryption_key, validate=True)
            nonce = os.urandom(12)
            encrypted = nonce + AESGCM(key).encrypt(nonce, secret, b"novel-agent:model-key:v1")
        elif raw:
            warning = "Model ciphertext archived; supply both encryption keys to activate migrated model credentials."
            if warning not in report["warnings"]:
                report["warnings"].append(warning)
            continue
        payload = decode(row)
        connection.execute(
            """INSERT INTO governance_model_profiles(id,workspace_id,current_version)
               VALUES(%s,%s,1) ON CONFLICT DO NOTHING""",
            (identity, workspace),
        )
        connection.execute(
            """INSERT INTO governance_model_profile_versions(id,workspace_id,profile_id,version,name,
               provider,base_url,encrypted_key,chat_models,embedding_models,max_tokens)
               VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,%s,4096) ON CONFLICT DO NOTHING""",
            (
                version,
                workspace,
                identity,
                row["name"],
                row["provider"],
                row.get("base_url", ""),
                encrypted,
                Jsonb(payload.get("chat_models", [])),
                Jsonb(payload.get("embedding_models", [])),
            ),
        )
        profiles[row["id"]] = version
    routes = defaultdict(dict)
    for row in tables.get("model_routes", []):
        tenant = row.get("tenant_id") or "tenant_local"
        if row["profile_id"] not in profiles:
            continue
        target = {
            "profile_id": stable_id("model-profile", row["profile_id"]),
            "profile_version_id": profiles[row["profile_id"]],
            "model_name": row["model_name"],
        }
        if row.get("fallback_profile_id") in profiles:
            target["fallback"] = [
                {
                    "profile_id": stable_id("model-profile", row["fallback_profile_id"]),
                    "profile_version_id": profiles[row["fallback_profile_id"]],
                    "model_name": row["fallback_model_name"],
                }
            ]
        routes[tenant][row["purpose"]] = target
    for tenant, payload in routes.items():
        if tenant not in owners:
            continue
        workspace = stable_id("workspace", tenant)
        connection.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
        connection.execute(
            """INSERT INTO governance_model_routes(id,workspace_id,version,routes,created_by)
               VALUES(%s,%s,1,%s,%s) ON CONFLICT DO NOTHING""",
            (stable_id("model-routes", tenant), workspace, Jsonb(payload), owners[tenant]),
        )
