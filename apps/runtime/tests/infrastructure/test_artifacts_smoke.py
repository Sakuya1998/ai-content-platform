"""Exercise the artifact smoke against the actual S3 adapter without a network."""

import base64
import hashlib
import io
from types import SimpleNamespace

from botocore.stub import Stubber
from tools.migrate.platform_artifacts_check import verify_artifacts

from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.settings import RuntimeSettings
from tests.infrastructure.test_artifacts import client


def test_direct_artifact_smoke_checks_replay_conflict_and_download_hash(monkeypatch):
    monkeypatch.setattr("tools.migrate.platform_artifacts_check.uuid4", lambda: SimpleNamespace(hex="fixed"))
    sdk = client()
    data = b"shared agent artifact integration"
    digest = hashlib.sha256(data).hexdigest()
    key = "workspaces/ws/projects/project/assets/artifact-check-fixed/versions/v-1/content.txt"
    head = {"ContentLength": len(data), "Metadata": {"sha256": digest}, "ContentType": "text/plain", "ETag": '"etag"'}
    with Stubber(sdk) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"etag"'},
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
        for _ in range(2):
            stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
            stub.add_response("head_object", head, {"Bucket": "agent-artifacts", "Key": key})
        for _ in range(3):
            stub.add_response(
                "get_object",
                {"Body": io.BytesIO(data), "ContentLength": len(data), "Metadata": {"sha256": digest}},
                {"Bucket": "agent-artifacts", "Key": key},
            )
        result = verify_artifacts(S3ArtifactStore(RuntimeSettings(_env_file=None), sdk), "ws", "project")
        assert result["status"] == "passed" and result["artifact"]["sha256"] == digest
        stub.assert_no_pending_responses()
