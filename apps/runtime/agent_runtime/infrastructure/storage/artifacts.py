"""S3/RustFS artifact storage for shared, immutable project objects."""

import base64
import hashlib
from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from agent_runtime.infrastructure.storage.keys import object_key
from agent_runtime.settings import RuntimeSettings


class ArtifactConflict(ValueError):
    """The object version already exists with a different content hash."""


@dataclass(frozen=True)
class ArtifactReference:
    bucket: str
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str
    etag: str


class S3ArtifactStore:
    def __init__(self, settings: RuntimeSettings, client=None):
        if client is None and not settings.object_storage_endpoint:
            raise ValueError("object storage endpoint is required")
        self.bucket = settings.object_storage_bucket
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.object_storage_endpoint,
            region_name=settings.object_storage_region,
            aws_access_key_id=settings.object_storage_access_key,
            aws_secret_access_key=settings.object_storage_secret_key.get_secret_value(),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 2},
            ),
        )

    def ready(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except ClientError:
            return False

    def upload(
        self,
        workspace_id: str,
        project_id: str,
        category: str,
        resource_id: str,
        filename: str,
        data: bytes,
        *,
        version_id: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> ArtifactReference:
        key = object_key(workspace_id, project_id, category, resource_id, filename, version_id=version_id)
        digest = hashlib.sha256(data)
        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                IfNoneMatch="*",
                Metadata={"sha256": digest.hexdigest()},
                ChecksumSHA256=base64.b64encode(digest.digest()).decode("ascii"),
            )
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            existing = self.client.head_object(Bucket=self.bucket, Key=key)
            if existing.get("Metadata", {}).get("sha256") != digest.hexdigest():
                raise ArtifactConflict("object version already has different content") from exc
            if existing["ContentLength"] != len(data):
                raise ArtifactConflict("object version size mismatch") from exc
            if existing.get("ContentType", content_type) != content_type:
                raise ArtifactConflict("object version content type mismatch") from exc
            response = existing
        return ArtifactReference(
            bucket=self.bucket,
            object_key=key,
            sha256=digest.hexdigest(),
            size_bytes=len(data),
            content_type=content_type,
            etag=response.get("ETag", "").strip('"'),
        )

    def download(
        self,
        workspace_id: str,
        project_id: str,
        category: str,
        resource_id: str,
        filename: str,
        *,
        version_id: str | None = None,
        expected_sha256: str = "",
    ) -> bytes:
        key = object_key(workspace_id, project_id, category, resource_id, filename, version_id=version_id)
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        try:
            data = response["Body"].read()
        finally:
            response["Body"].close()
        expected = expected_sha256 or response.get("Metadata", {}).get("sha256", "")
        if not expected or hashlib.sha256(data).hexdigest() != expected:
            raise ArtifactConflict("downloaded artifact checksum mismatch")
        return data

    def download_key(self, key: str, *, expected_sha256: str, expected_size_bytes: int) -> bytes:
        if not key or len(expected_sha256) != 64 or expected_size_bytes < 1:
            raise ArtifactConflict("invalid immutable object reference")
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        try:
            if response.get("ContentLength", expected_size_bytes) != expected_size_bytes:
                raise ArtifactConflict("stored artifact size mismatch")
            data = response["Body"].read(expected_size_bytes + 1)
        finally:
            response["Body"].close()
        if len(data) != expected_size_bytes or hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ArtifactConflict("downloaded artifact checksum or size mismatch")
        metadata_hash = response.get("Metadata", {}).get("sha256")
        if metadata_hash and metadata_hash != expected_sha256:
            raise ArtifactConflict("stored artifact metadata checksum mismatch")
        return data
