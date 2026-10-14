"""Load and verify immutable execution input without a Control API request."""

import asyncio

from agent_runtime.domain.execution_snapshot import ExecutionSnapshot
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore


async def load_execution_snapshot(command, settings) -> dict:
    reference = command.execution_snapshot
    if reference is None:
        raise ValueError("immutable execution snapshot reference is required")
    if reference.size_bytes > settings.artifact_max_bytes:
        raise ValueError("execution snapshot exceeds configured size limit")

    store = S3ArtifactStore(settings)
    data = await asyncio.to_thread(
        store.download_key,
        reference.object_key,
        expected_sha256=reference.sha256,
        expected_size_bytes=reference.size_bytes,
    )
    snapshot = ExecutionSnapshot.model_validate_json(data)
    snapshot.verify_command(command)
    return snapshot.model_dump(mode="json")
