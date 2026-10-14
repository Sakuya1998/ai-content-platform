"""Encrypted PostgreSQL and artifact backup using official pg_dump/pg_restore."""

import argparse
import hashlib
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.maintenance.backup_crypto import decrypt_backup, encrypt_backup, is_encrypted_backup
from agent_runtime.settings import RuntimeSettings


def database_environment(url):
    params = conninfo_to_dict(url)
    result = dict(os.environ)
    for key, variable in {
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "dbname": "PGDATABASE",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
    }.items():
        if key in params:
            result[variable] = params[key]
    return result


def make_archive(files, password):
    if not password:
        raise ValueError("a backup encryption password is required")
    manifest = {
        "schema_version": "agent-platform-backup@1",
        "files": {
            name: {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)} for name, value in files.items()
        },
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, value in files.items():
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("unsafe backup entry")
            archive.writestr(name, value)
    return encrypt_backup(buffer.getvalue(), password)


def open_archive(data, password):
    if not is_encrypted_backup(data):
        raise ValueError("platform backups must be encrypted")
    plain = decrypt_backup(data, password)
    with zipfile.ZipFile(io.BytesIO(plain)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise ValueError("unsafe backup archive")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema_version") != "agent-platform-backup@1":
            raise ValueError("unsupported backup schema")
        if set(names) != {"manifest.json", *manifest["files"]}:
            raise ValueError("backup members mismatch")
        files = {}
        for name, metadata in manifest["files"].items():
            value = archive.read(name)
            if len(value) != metadata["size"] or hashlib.sha256(value).hexdigest() != metadata["sha256"]:
                raise ValueError("backup checksum mismatch")
            files[name] = value
        return files


def run_database(tool, url, args, data=None):
    environment = database_environment(url)
    command = [tool, *args]
    image = os.getenv("PLATFORM_PG_TOOLS_IMAGE")
    if image:
        variables = [key for key in environment if key.startswith("PG")]
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "host",
            *[item for key in variables for item in ["--env", key]],
            "--entrypoint",
            tool,
            image,
            *args,
        ]
    result = subprocess.run(command, env=environment, input=data, capture_output=True, timeout=1800)
    if result.returncode:
        raise RuntimeError(tool + " failed; inspect database permissions and service state")
    return result.stdout


def create(control_url, settings, output, password, *, artifacts=True):
    output = Path(output)
    if output.exists():
        raise FileExistsError(output.name)
    import psycopg

    for url in [control_url, settings.database_url]:
        with psycopg.connect(url) as connection:
            role = connection.execute(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            if not role or not role[0]:
                raise ValueError(
                    "backup requires administrative or BYPASSRLS credentials separate from app credentials"
                )
    files = {
        "control.dump": run_database(
            "pg_dump",
            control_url,
            ["--format=custom", "--no-owner", "--no-acl", "--exclude-table=platform_restore_progress"],
        ),
        "runtime.dump": run_database(
            "pg_dump",
            settings.database_url,
            ["--format=custom", "--no-owner", "--no-acl", "--exclude-table=platform_restore_progress"],
        ),
    }
    if artifacts:
        storage = S3ArtifactStore(settings)
        index = []
        for page in storage.client.get_paginator("list_objects_v2").paginate(Bucket=storage.bucket):
            for item in page.get("Contents", []):
                key = item["Key"]
                response = storage.client.get_object(Bucket=storage.bucket, Key=key)
                try:
                    data = response["Body"].read()
                finally:
                    response["Body"].close()
                name = "artifacts/" + hashlib.sha256(key.encode()).hexdigest()
                files[name] = data
                index.append(
                    {
                        "key": key,
                        "file": name,
                        "metadata": response.get("Metadata", {}),
                        "content_type": response.get("ContentType", "application/octet-stream"),
                    }
                )
        files["artifacts.json"] = json.dumps(index).encode()
    encrypted = make_archive(files, password)
    with output.open("xb") as destination:
        output.chmod(0o600)
        destination.write(encrypted)
    return {"output": output.name, "sha256": hashlib.sha256(encrypted).hexdigest(), "files": len(files)}


def restore(files, control_url, settings, *, control_role="control", runtime_role="runtime"):
    import psycopg

    fingerprint = hashlib.sha256(files["control.dump"] + files["runtime.dump"]).hexdigest()
    progress = []
    for url in [control_url, settings.database_url]:
        with psycopg.connect(url) as connection:
            exists = connection.execute("SELECT to_regclass('public.platform_restore_progress')").fetchone()[0]
            row = (
                connection.execute("SELECT archive_hash,phase FROM platform_restore_progress").fetchone()
                if exists
                else None
            )
            if row and row[0] != fingerprint:
                raise ValueError("restore target belongs to another archive")
            if not row and connection.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0]:
                raise ValueError("restore target must be empty or contain this archive's restore progress")
            if not exists:
                connection.execute(
                    "CREATE TABLE platform_restore_progress(archive_hash text PRIMARY KEY,phase text NOT NULL)"
                )
                connection.execute("INSERT INTO platform_restore_progress VALUES(%s,'pending')", (fingerprint,))
            progress.append(row[1] if row else "pending")
    for index, (name, url) in enumerate([("control.dump", control_url), ("runtime.dump", settings.database_url)]):
        if progress[index] in {"database", "complete"}:
            continue
        run_database(
            "pg_restore",
            url,
            [
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--single-transaction",
                "--dbname=" + conninfo_to_dict(url)["dbname"],
            ],
            files[name],
        )
        with psycopg.connect(url) as connection:
            connection.execute(
                "UPDATE platform_restore_progress SET phase='database' WHERE archive_hash=%s", (fingerprint,)
            )
    if "artifacts.json" in files:
        storage = S3ArtifactStore(settings)
        for item in json.loads(files["artifacts.json"]):
            data = files[item["file"]]
            try:
                storage.client.put_object(
                    Bucket=storage.bucket,
                    Key=item["key"],
                    Body=data,
                    Metadata=item["metadata"],
                    ContentType=item["content_type"],
                    IfNoneMatch="*",
                )
            except Exception as exc:
                if getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                    raise
                response = storage.client.get_object(Bucket=storage.bucket, Key=item["key"])
                try:
                    existing = response["Body"].read()
                finally:
                    response["Body"].close()
                if hashlib.sha256(existing).digest() != hashlib.sha256(data).digest():
                    raise ValueError("restore artifact content conflict") from None
    for url in [control_url, settings.database_url]:
        with psycopg.connect(url) as connection:
            connection.execute(
                "UPDATE platform_restore_progress SET phase='complete' WHERE archive_hash=%s", (fingerprint,)
            )
    from psycopg import sql

    for url, role in [(control_url, control_role), (settings.database_url, runtime_role)]:
        with psycopg.connect(url) as connection:
            if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                raise ValueError("restore application role must already exist")
            for statement in [
                "GRANT USAGE ON SCHEMA public TO {}",
                "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}",
                "GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}",
            ]:
                connection.execute(sql.SQL(statement).format(sql.Identifier(role)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "verify", "restore"])
    parser.add_argument("path")
    parser.add_argument("--control-env", default=".env.control.external")
    parser.add_argument("--runtime-env", default=".env.runtime.external")
    parser.add_argument("--password-env", default="PLATFORM_BACKUP_PASSWORD")
    parser.add_argument("--confirm-stopped", action="store_true")
    parser.add_argument("--skip-artifacts", action="store_true")
    parser.add_argument("--control-role", default="control")
    parser.add_argument("--runtime-role", default="runtime")
    args = parser.parse_args()
    password = os.getenv(args.password_env, "")
    if not password:
        parser.error("backup password environment variable is required")
    if args.command == "verify":
        files = open_archive(Path(args.path).read_bytes(), password)
        print(json.dumps({"verified": True, "files": len(files)}))
        return
    if not args.confirm_stopped:
        parser.error("stop all writers first, then pass --confirm-stopped")
    control = os.getenv("PLATFORM_BACKUP_CONTROL_DATABASE_URL")
    runtime = os.getenv("PLATFORM_BACKUP_RUNTIME_DATABASE_URL")
    if not control or not runtime:
        parser.error("PLATFORM_BACKUP_CONTROL_DATABASE_URL and PLATFORM_BACKUP_RUNTIME_DATABASE_URL are required")
    settings = RuntimeSettings(_env_file=args.runtime_env)
    settings = settings.model_copy(update={"database_url": runtime})
    if args.command == "create":
        print(json.dumps(create(control, settings, args.path, password, artifacts=not args.skip_artifacts)))
    else:
        restore(
            open_archive(Path(args.path).read_bytes(), password),
            control,
            settings,
            control_role=args.control_role,
            runtime_role=args.runtime_role,
        )
        print("platform restore completed")


if __name__ == "__main__":
    main()
