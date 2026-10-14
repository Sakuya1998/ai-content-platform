import base64
import hashlib
import io

import boto3
import pytest
from botocore.stub import Stubber

from agent_runtime.infrastructure.storage.artifacts import ArtifactConflict, S3ArtifactStore
from agent_runtime.settings import RuntimeSettings


def client():
    return boto3.client(
        "s3",
        endpoint_url="http://localhost:9000",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test-secret",
    )


def test_upload_uses_shared_path_conditional_put_and_sha256():
    sdk = client()
    data = b"generated scene"
    digest = hashlib.sha256(data).hexdigest()
    key = "workspaces/ws-1/projects/p-1/assets/a-1/versions/v-1/scene.txt"
    with Stubber(sdk) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"etag-1"'},
            {
                "Bucket": "agent-artifacts",
                "Key": key,
                "Body": data,
                "ContentType": "text/plain",
                "IfNoneMatch": "*",
                "Metadata": {"sha256": digest},
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
            },
        )
        ref = S3ArtifactStore(RuntimeSettings(_env_file=None), sdk).upload(
            "ws-1",
            "p-1",
            "assets",
            "a-1",
            "scene.txt",
            data,
            version_id="v-1",
            content_type="text/plain",
        )
        assert ref.bucket == "agent-artifacts"
        assert ref.object_key == key
        assert ref.sha256 == digest
        assert ref.size_bytes == len(data)


def test_upload_rejects_overwriting_a_different_version_payload():
    sdk = client()
    key = "workspaces/ws-1/projects/p-1/assets/a-1/versions/v-1/scene.txt"
    with Stubber(sdk) as stub:
        stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
        stub.add_response(
            "head_object",
            {
                "ContentLength": 10,
                "Metadata": {"sha256": "a" * 64},
                "ETag": '"old"',
            },
            {"Bucket": "agent-artifacts", "Key": key},
        )
        with pytest.raises(ArtifactConflict):
            S3ArtifactStore(RuntimeSettings(_env_file=None), sdk).upload(
                "ws-1",
                "p-1",
                "assets",
                "a-1",
                "scene.txt",
                b"new content",
                version_id="v-1",
            )


def test_download_verifies_sha256_and_returns_bytes():
    sdk = client()
    data = b"stored bytes"
    digest = hashlib.sha256(data).hexdigest()
    key = "workspaces/ws-1/projects/p-1/uploads/a-1/file.txt"
    with Stubber(sdk) as stub:
        stub.add_response(
            "get_object",
            {
                "Body": io.BytesIO(data),
                "ContentLength": len(data),
                "Metadata": {"sha256": digest},
            },
            {"Bucket": "agent-artifacts", "Key": key},
        )
        assert (
            S3ArtifactStore(RuntimeSettings(_env_file=None), sdk).download(
                "ws-1",
                "p-1",
                "uploads",
                "a-1",
                "file.txt",
                expected_sha256=digest,
            )
            == data
        )


def test_download_rejects_corrupt_bytes():
    sdk = client()
    key = "workspaces/ws-1/projects/p-1/uploads/a-1/file.txt"
    with Stubber(sdk) as stub:
        stub.add_response(
            "get_object",
            {
                "Body": io.BytesIO(b"corrupt bytes"),
                "Metadata": {"sha256": "a" * 64},
            },
            {"Bucket": "agent-artifacts", "Key": key},
        )
        with pytest.raises(ArtifactConflict):
            S3ArtifactStore(RuntimeSettings(_env_file=None), sdk).download(
                "ws-1",
                "p-1",
                "uploads",
                "a-1",
                "file.txt",
            )


def test_download_key_verifies_execution_snapshot_hash_and_size():
    sdk = client()
    data = b'{"job_id":"job-1"}'
    digest = hashlib.sha256(data).hexdigest()
    key = "workspaces/ws-1/projects/p-1/assets/job-1/versions/hash/execution.json"
    with Stubber(sdk) as stub:
        stub.add_response(
            "get_object",
            {
                "Body": io.BytesIO(data),
                "ContentLength": len(data),
                "Metadata": {"sha256": digest},
            },
            {"Bucket": "agent-artifacts", "Key": key},
        )
        result = S3ArtifactStore(RuntimeSettings(_env_file=None), sdk).download_key(
            key,
            expected_sha256=digest,
            expected_size_bytes=len(data),
        )
        assert result == data


def test_download_key_rejects_size_mismatch():
    sdk = client()
    data = b'{"job_id":"job-1"}'
    digest = hashlib.sha256(data).hexdigest()
    key = "workspaces/ws-1/projects/p-1/assets/job-1/versions/hash/execution.json"
    with Stubber(sdk) as stub:
        stub.add_response(
            "get_object",
            {"Body": io.BytesIO(data), "ContentLength": len(data), "Metadata": {"sha256": digest}},
            {"Bucket": "agent-artifacts", "Key": key},
        )
        with pytest.raises(ArtifactConflict):
            S3ArtifactStore(RuntimeSettings(_env_file=None), sdk).download_key(
                key,
                expected_sha256=digest,
                expected_size_bytes=len(data) + 1,
            )
