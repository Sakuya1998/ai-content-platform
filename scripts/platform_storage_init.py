"""Check or explicitly create the shared artifact bucket."""

import argparse
import time

from botocore.exceptions import ClientError, EndpointConnectionError

from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.settings import RuntimeSettings


def initialize_bucket(store: S3ArtifactStore, region: str, *, create: bool) -> None:
    try:
        store.client.head_bucket(Bucket=store.bucket)
        return
    except ClientError as exc:
        if not create or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise
    params = {"Bucket": store.bucket}
    if region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        store.client.create_bucket(**params)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
    store.client.head_bucket(Bucket=store.bucket)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.runtime")
    parser.add_argument("--create-bucket", action="store_true")
    args = parser.parse_args()
    settings = RuntimeSettings(_env_file=args.env_file)
    store = S3ArtifactStore(settings)
    for attempt in range(30):
        try:
            initialize_bucket(store, settings.object_storage_region, create=args.create_bucket)
            print(f"artifact bucket ready: {store.bucket}")
            return 0
        except EndpointConnectionError:
            if attempt == 29:
                raise
            time.sleep(2)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
