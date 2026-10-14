"""Apply the control/runtime PostgreSQL migrations exactly once.

Migration files are immutable after application. A changed file with the same
name is rejected by checksum, which prevents a partially upgraded deployment
from silently drifting between environments.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

try:
    from scripts.platform_bootstrap import bootstrap
except ModuleNotFoundError:
    from platform_bootstrap import bootstrap

ROOT = Path(__file__).resolve().parents[1]
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS platform_migrations (
    component text NOT NULL,
    migration_name text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (component, migration_name)
)
"""


@dataclass(frozen=True)
class Migration:
    component: str
    name: str
    path: Path

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def migrations() -> tuple[Migration, ...]:
    return (
        Migration("control", "001_init", ROOT / "migrations/control/001_init.sql"),
        Migration("control", "002_canonical", ROOT / "migrations/control/002_canonical.sql"),
        Migration("control", "003_messaging", ROOT / "migrations/control/003_messaging.sql"),
        Migration("control", "004_generation", ROOT / "migrations/control/004_generation.sql"),
        Migration("control", "005_identity_governance", ROOT / "migrations/control/005_identity_governance.sql"),
        Migration("control", "006_novel_management", ROOT / "migrations/control/006_novel_management.sql"),
        Migration("control", "007_operations", ROOT / "migrations/control/007_operations.sql"),
        Migration("control", "008_benchmarks", ROOT / "migrations/control/008_benchmarks.sql"),
        Migration("control", "009_prompt_catalog", ROOT / "migrations/control/009_prompt_catalog.sql"),
        Migration(
            "control", "010_prompt_catalog_identity", ROOT / "migrations/control/010_prompt_catalog_identity.sql"
        ),
        Migration("control", "011_creation_policies", ROOT / "migrations/control/011_creation_policies.sql"),
        Migration("control", "012_video_models", ROOT / "migrations/control/012_video_models.sql"),
        Migration("control", "013_image_speech_models", ROOT / "migrations/control/013_image_speech_models.sql"),
        Migration("control", "014_timeline_workflows", ROOT / "migrations/control/014_timeline_workflows.sql"),
        Migration("control", "015_production_runs", ROOT / "migrations/control/015_production_runs.sql"),
        Migration(
            "control", "016_music_sound_effect_models", ROOT / "migrations/control/016_music_sound_effect_models.sql"
        ),
        Migration("control", "017_generic_resources", ROOT / "migrations/control/017_generic_resources.sql"),
        Migration("control", "018_resource_archive", ROOT / "migrations/control/018_resource_archive.sql"),
        Migration("control", "019_retire_novel_state", ROOT / "migrations/control/019_retire_novel_state.sql"),
        Migration(
            "control",
            "020_project_workflow_concurrency",
            ROOT / "migrations/control/020_project_workflow_concurrency.sql",
        ),
        Migration("control", "021_workflow_catalog", ROOT / "migrations/control/021_workflow_catalog.sql"),
        Migration(
            "control", "022_document_title_identity", ROOT / "migrations/control/022_document_title_identity.sql"
        ),
        Migration("runtime", "001_init", ROOT / "migrations/runtime/001_init.sql"),
        Migration("runtime", "002_runtime_defaults", ROOT / "migrations/runtime/002_runtime_defaults.sql"),
        Migration("runtime", "003_artifacts", ROOT / "migrations/runtime/003_artifacts.sql"),
        Migration("runtime", "004_messaging", ROOT / "migrations/runtime/004_messaging.sql"),
        Migration("runtime", "005_execution", ROOT / "migrations/runtime/005_execution.sql"),
        Migration("runtime", "006_novel_workflows", ROOT / "migrations/runtime/006_novel_workflows.sql"),
        Migration("runtime", "007_knowledge_operations", ROOT / "migrations/runtime/007_knowledge_operations.sql"),
        Migration("runtime", "008_shared_canon", ROOT / "migrations/runtime/008_shared_canon.sql"),
        Migration("runtime", "009_media_tasks", ROOT / "migrations/runtime/009_media_tasks.sql"),
        Migration("runtime", "010_generated_media", ROOT / "migrations/runtime/010_generated_media.sql"),
        Migration("runtime", "011_composition_runs", ROOT / "migrations/runtime/011_composition_runs.sql"),
        Migration(
            "runtime", "012_music_sound_effect_media", ROOT / "migrations/runtime/012_music_sound_effect_media.sql"
        ),
        Migration("runtime", "013_generic_workflow_state", ROOT / "migrations/runtime/013_generic_workflow_state.sql"),
        Migration(
            "runtime", "014_workflow_memory_identity", ROOT / "migrations/runtime/014_workflow_memory_identity.sql"
        ),
        Migration(
            "runtime", "015_legacy_resource_archive", ROOT / "migrations/runtime/015_legacy_resource_archive.sql"
        ),
        Migration("runtime", "016_retire_novel_state", ROOT / "migrations/runtime/016_retire_novel_state.sql"),
    )


def migration_connection(application_url: str, administrator_url: str = "") -> tuple[str, str | None]:
    if not administrator_url:
        return application_url, None
    application = conninfo_to_dict(application_url)
    administrator = conninfo_to_dict(administrator_url)
    administrator["dbname"] = application["dbname"]
    return make_conninfo(**administrator), application.get("user")


def apply_database(
    url: str,
    component: str,
    selected: tuple[Migration, ...],
    *,
    dry_run: bool = False,
    application_role: str | None = None,
) -> list[str]:
    if dry_run:
        return [f"{item.component}/{item.name}:{item.checksum}" for item in selected]
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as connection:
        if application_role:
            with connection.cursor() as cursor:
                cursor.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
                if not cursor.fetchone()[0]:
                    raise RuntimeError("migration administrator must bypass RLS for cross-workspace backfills")
        with connection.cursor() as cursor:
            cursor.execute(LEDGER_DDL)
        for item in selected:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"novel-agent:{component}",))
                cursor.execute(
                    "SELECT checksum FROM platform_migrations WHERE component = %s AND migration_name = %s",
                    (item.component, item.name),
                )
                row = cursor.fetchone()
                if row is not None:
                    if row[0] != item.checksum:
                        raise RuntimeError(f"migration checksum mismatch: {item.component}/{item.name}")
                    applied.append(f"skipped {item.component}/{item.name}")
                    continue
                cursor.execute(item.path.read_text(encoding="utf-8"))
                cursor.execute(
                    "INSERT INTO platform_migrations (component, migration_name, checksum) VALUES (%s, %s, %s)",
                    (item.component, item.name, item.checksum),
                )
                applied.append(f"applied {item.component}/{item.name}")
        if application_role:
            with connection.cursor() as cursor:
                role = sql.Identifier(application_role)
                cursor.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(role))
                cursor.execute(
                    sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(role)
                )
                cursor.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                    ).format(role)
                )
                cursor.execute(
                    sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}").format(
                        role
                    )
                )
    return applied


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate Novel Agent platform PostgreSQL databases")
    parser.add_argument("--control-url", default=os.getenv("CONTROL_DATABASE_URL", ""))
    parser.add_argument("--runtime-url", default=os.getenv("RUNTIME_DATABASE_URL", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--bootstrap-admin-url", default=os.getenv("PLATFORM_BOOTSTRAP_ADMIN_URL", ""))
    parser.add_argument("--migration-admin-url", default=os.getenv("PLATFORM_MIGRATION_ADMIN_URL", ""))
    parser.add_argument("--bootstrap-control-password", default=os.getenv("POSTGRES_CONTROL_PASSWORD", ""))
    parser.add_argument("--bootstrap-runtime-password", default=os.getenv("POSTGRES_RUNTIME_PASSWORD", ""))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.dry_run:
        for item in migrations():
            print(f"{item.component}/{item.name}:{item.checksum}")
        return 0
    if not args.control_url or not args.runtime_url:
        raise SystemExit("CONTROL_DATABASE_URL and RUNTIME_DATABASE_URL are required")
    if args.bootstrap_admin_url:
        bootstrap(
            args.bootstrap_admin_url,
            args.control_url,
            args.runtime_url,
            os.getenv("POSTGRES_CONTROL_USER", "control"),
            os.getenv("POSTGRES_RUNTIME_USER", "runtime"),
            args.bootstrap_control_password,
            args.bootstrap_runtime_password,
            os.getenv("POSTGRES_CONTROL_DB", "control"),
            os.getenv("POSTGRES_RUNTIME_DB", "runtime"),
        )
    control_migrations = tuple(item for item in migrations() if item.component == "control")
    runtime_migrations = tuple(item for item in migrations() if item.component == "runtime")
    control_url, control_role = migration_connection(
        args.control_url, args.migration_admin_url or args.bootstrap_admin_url
    )
    runtime_url, runtime_role = migration_connection(
        args.runtime_url, args.migration_admin_url or args.bootstrap_admin_url
    )
    for line in apply_database(control_url, "control", control_migrations, application_role=control_role):
        print(line)
    for line in apply_database(runtime_url, "runtime", runtime_migrations, application_role=runtime_role):
        print(line)
    try:
        from scripts.platform_prompt_seed import seed
    except ModuleNotFoundError:
        from platform_prompt_seed import seed

    print("prompt catalog initialized: " + str(seed(args.control_url)))
    try:
        from scripts.platform_policy_seed import seed as seed_policies
    except ModuleNotFoundError:
        from platform_policy_seed import seed as seed_policies

    print("creation policies initialized: " + str(seed_policies(args.control_url)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
