"""Read-only legacy SQLite/Chroma export with stable platform identities."""

import argparse
import base64
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid5

TABLES = {
    "tenants",
    "users",
    "novels",
    "chapters",
    "progress",
    "creative_brief_versions",
    "chapter_versions",
    "chapter_candidates",
    "planning_versions",
    "chapter_evaluations",
    "evaluation_benchmark_runs",
    "book_audits",
    "memory_snapshots",
    "memory_quality_runs",
    "run_jobs",
    "run_job_events",
    "transfer_jobs",
    "workspace_resources",
    "model_profiles",
    "model_routes",
    "model_call_metrics",
    "audit_logs",
}


def stable_id(kind, identity):
    try:
        return str(UUID(str(identity)))
    except ValueError:
        return str(uuid5(NAMESPACE_URL, f"agent-platform:legacy:{kind}:{identity}"))


def encode_value(value):
    if isinstance(value, bytes):
        return {"$binary": base64.b64encode(value).decode()}
    return value


def sqlite_tables(source, selected=None):
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(source.name)
    with sqlite3.connect("file:" + quote(str(source)) + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        available = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        names = available & selected if selected is not None else available - {"sqlite_sequence"}
        result = {}
        for name in sorted(names):
            escaped = '"' + name.replace('"', '""') + '"'
            result[name] = [
                {key: encode_value(value) for key, value in dict(row).items()}
                for row in connection.execute("SELECT * FROM " + escaped)
            ]
        connection.rollback()
        return result


def chroma_snapshot(source):
    """Chroma may write telemetry/database state on open, so read a private copy."""
    import chromadb

    with tempfile.TemporaryDirectory(prefix="agent-chroma-export-") as temporary:
        target = Path(temporary) / "chroma"
        shutil.copytree(source, target)
        client = chromadb.PersistentClient(path=str(target))
        collections = {}
        for collection in client.list_collections():
            values = collection.get(include=["documents", "metadatas", "embeddings"])
            embeddings = values.get("embeddings")
            collections[collection.name] = {
                "ids": values.get("ids", []),
                "documents": values.get("documents", []),
                "metadatas": values.get("metadatas", []),
                "embeddings": embeddings.tolist() if hasattr(embeddings, "tolist") else embeddings,
            }
        return collections


def export_legacy(source, output, *, checkpoint=None, chroma=None):
    output = Path(output)
    if output.exists():
        raise FileExistsError(output.name)
    tables = sqlite_tables(source, TABLES)
    mapping = {"workspace": {}, "user": {}, "project": {}}
    for table, kind, column in [("tenants", "workspace", "id"), ("users", "user", "id"), ("novels", "project", "id")]:
        for row in tables.get(table, []):
            mapping[kind][str(row[column])] = stable_id(kind, row[column])
    for row in tables.get("novels", []):
        tenant = row.get("tenant_id") or "tenant_local"
        mapping["workspace"].setdefault(tenant, stable_id("workspace", tenant))
    bundle = {
        "schema_version": "agent-platform-legacy-export@1",
        "tables": tables,
        "id_map": mapping,
        "checkpoints": sqlite_tables(checkpoint) if checkpoint else {},
        "chroma": chroma_snapshot(chroma) if chroma else {},
        "source_database_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
        "warnings": ["Legacy active sessions are not migrated; users must log in again."],
    }
    encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as destination:
        output.chmod(0o600)
        destination.write(encoded)
    return {
        "output": output.name,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "counts": {name: len(rows) for name, rows in tables.items()},
        "checkpoint_tables": len(bundle["checkpoints"]),
        "chroma_collections": len(bundle["chroma"]),
        "warnings": bundle["warnings"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--checkpoint")
    parser.add_argument("--chroma")
    args = parser.parse_args()
    print(json.dumps(export_legacy(args.sqlite, args.output, checkpoint=args.checkpoint, chroma=args.chroma)))


if __name__ == "__main__":
    main()
