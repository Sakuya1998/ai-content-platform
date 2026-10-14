"""Verify direct immutable S3/RustFS uploads, replay and checksum validation."""

import argparse
import hashlib
import json
from dataclasses import asdict
from uuid import uuid4

from agent_runtime.infrastructure.storage.artifacts import ArtifactConflict, S3ArtifactStore
from agent_runtime.settings import RuntimeSettings


def verify_artifacts(artifacts, workspace_id, project_id):
    data = b"shared agent artifact integration"
    resource = "artifact-check-" + uuid4().hex
    location = (workspace_id, project_id, "assets", resource, "content.txt")
    options = {"version_id": "v-1", "content_type": "text/plain"}
    reference = artifacts.upload(*location, data, **options)
    replay = artifacts.upload(*location, data, **options)
    if asdict(reference) != asdict(replay):
        raise AssertionError("immutable upload retry changed the object reference")
    try:
        artifacts.upload(*location, b"conflicting artifact", **options)
    except ArtifactConflict:
        pass
    else:
        raise AssertionError("immutable object allowed conflicting content")
    downloaded = artifacts.download(*location, version_id="v-1", expected_sha256=reference.sha256)
    if downloaded != data or reference.sha256 != hashlib.sha256(data).hexdigest():
        raise AssertionError("artifact content or checksum mismatch")
    verified = artifacts.download_key(
        reference.object_key, expected_sha256=reference.sha256, expected_size_bytes=reference.size_bytes
    )
    if verified != data:
        raise AssertionError("object reference did not resolve to the uploaded artifact")
    try:
        artifacts.download_key(reference.object_key, expected_sha256="0" * 64, expected_size_bytes=reference.size_bytes)
    except ArtifactConflict:
        pass
    else:
        raise AssertionError("artifact download accepted a mismatched checksum")
    return {"status": "passed", "artifact": asdict(reference)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-env-file", default=".env.runtime")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--create-bucket",
        action="store_true",
        help="Explicitly create the configured bucket if absent",
    )
    args = parser.parse_args()
    settings = RuntimeSettings(_env_file=args.runtime_env_file)
    artifacts = S3ArtifactStore(settings)
    if args.create_bucket and not artifacts.ready():
        params = {"Bucket": artifacts.bucket}
        if settings.object_storage_region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": settings.object_storage_region}
        artifacts.client.create_bucket(**params)
    print(json.dumps(verify_artifacts(artifacts, args.workspace_id, args.project_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
