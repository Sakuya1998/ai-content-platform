"""Create application database roles, databases, extensions and permissions."""

from __future__ import annotations

import argparse
import os

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def ensure_role(connection: psycopg.Connection, name: str, password: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(password))
            )
        else:
            cursor.execute(
                sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(password))
            )


def ensure_database(admin_url: str, name: str, owner: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8'").format(
                    sql.Identifier(name),
                    sql.Identifier(owner),
                )
            )


def database_url_for(admin_url: str, database: str) -> str:
    """Point the administrator connection at a target database."""
    params = conninfo_to_dict(admin_url)
    params["dbname"] = database
    return make_conninfo(**params)


def prepare_extensions(admin_url: str, database: str, *, vector: bool) -> None:
    url = database_url_for(admin_url, database)
    with psycopg.connect(url, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        if vector:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")


def prepare_database(url: str, user: str) -> None:
    with psycopg.connect(url, autocommit=True) as connection, connection.cursor() as cursor:
        user_ident = sql.Identifier(user).as_string(connection)
        cursor.execute("GRANT USAGE, CREATE ON SCHEMA public TO " + user_ident)
        grant = "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "
        cursor.execute(grant + user_ident)


def bootstrap(
    admin_url: str,
    control_url: str,
    runtime_url: str,
    control_user: str,
    runtime_user: str,
    control_password: str,
    runtime_password: str,
    control_db: str,
    runtime_db: str,
) -> None:
    with psycopg.connect(admin_url, autocommit=True) as connection:
        ensure_role(connection, control_user, control_password)
        ensure_role(connection, runtime_user, runtime_password)
    ensure_database(admin_url, control_db, control_user)
    ensure_database(admin_url, runtime_db, runtime_user)
    prepare_extensions(admin_url, control_db, vector=False)
    prepare_extensions(admin_url, runtime_db, vector=True)
    prepare_database(control_url, control_user)
    prepare_database(runtime_url, runtime_user)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Novel Agent PostgreSQL roles and databases")
    parser.add_argument("--admin-url", default=os.getenv("PLATFORM_BOOTSTRAP_ADMIN_URL", ""))
    parser.add_argument("--control-url", default=os.getenv("CONTROL_DATABASE_URL", ""))
    parser.add_argument("--runtime-url", default=os.getenv("RUNTIME_DATABASE_URL", ""))
    parser.add_argument("--control-user", default=os.getenv("POSTGRES_CONTROL_USER", "control"))
    parser.add_argument("--runtime-user", default=os.getenv("POSTGRES_RUNTIME_USER", "runtime"))
    parser.add_argument("--control-password", default=os.getenv("POSTGRES_CONTROL_PASSWORD", ""))
    parser.add_argument("--runtime-password", default=os.getenv("POSTGRES_RUNTIME_PASSWORD", ""))
    parser.add_argument("--control-db", default=os.getenv("POSTGRES_CONTROL_DB", "control"))
    parser.add_argument("--runtime-db", default=os.getenv("POSTGRES_RUNTIME_DB", "runtime"))
    args = parser.parse_args()
    if not args.admin_url:
        raise SystemExit("--admin-url or PLATFORM_BOOTSTRAP_ADMIN_URL is required")
    if not args.control_password or not args.runtime_password:
        raise SystemExit("control and runtime passwords are required")

    bootstrap(
        args.admin_url,
        args.control_url,
        args.runtime_url,
        args.control_user,
        args.runtime_user,
        args.control_password,
        args.runtime_password,
        args.control_db,
        args.runtime_db,
    )
    print("database bootstrap completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
