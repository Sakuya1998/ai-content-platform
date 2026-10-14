"""Prepare external databases and artifacts using split service config files."""

import argparse
import os

from dotenv import dotenv_values

from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.settings import RuntimeSettings
from scripts.platform_migrate import apply_database, migration_connection, migrations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-env", default=".env.control.external")
    parser.add_argument("--runtime-env", default=".env.runtime.external")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--create-bucket", action="store_true")
    parser.add_argument("--migration-admin-url", default=os.getenv("PLATFORM_MIGRATION_ADMIN_URL", ""))
    args = parser.parse_args()
    control = dotenv_values(args.control_env)
    runtime = RuntimeSettings(_env_file=args.runtime_env)
    failed = False
    for component, url in [("control", control.get("CONTROL_DATABASE_URL")), ("runtime", runtime.database_url)]:
        try:
            if not url:
                raise ValueError("database URL missing")
            if args.migrate:
                selected = tuple(item for item in migrations() if item.component == component)
                migration_url, role = migration_connection(url, args.migration_admin_url)
                for result in apply_database(migration_url, component, selected, application_role=role):
                    print(result)
                if component == "control":
                    from scripts.platform_prompt_seed import seed

                    print("prompt catalog initialized: " + str(seed(url)))
                    from scripts.platform_policy_seed import seed as seed_policies

                    print("creation policies initialized: " + str(seed_policies(url)))
            else:
                import psycopg

                with psycopg.connect(url, connect_timeout=5) as connection:
                    connection.execute("SELECT 1")
                print(component + " database reachable")
        except Exception as exc:
            print(component + " database preparation failed: " + type(exc).__name__)
            failed = True
    try:
        artifacts = S3ArtifactStore(runtime)
        if not artifacts.ready() and args.create_bucket:
            params = {"Bucket": artifacts.bucket}
            if runtime.object_storage_region != "us-east-1":
                params["CreateBucketConfiguration"] = {"LocationConstraint": runtime.object_storage_region}
            artifacts.client.create_bucket(**params)
        if not artifacts.ready():
            raise RuntimeError("artifact bucket not ready")
        print("artifact bucket ready")
    except Exception as exc:
        print("artifact preparation failed: " + type(exc).__name__)
        failed = True
    if not runtime.rocketmq_endpoint:
        print("MQ check pending: Proxy endpoint missing")
    if not runtime.llm_model or not runtime.llm_api_key.get_secret_value():
        print("AI check pending: model configuration missing")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
