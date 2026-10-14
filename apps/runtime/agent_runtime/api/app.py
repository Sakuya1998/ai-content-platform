"""Runtime health endpoints; task commands and results only travel through MQ."""

import asyncio

from fastapi import FastAPI, HTTPException

from agent_runtime.infrastructure.postgres.jobs import PostgresRuntimeJobStore, RuntimeDatabaseUnavailable
from agent_runtime.infrastructure.storage.artifacts import S3ArtifactStore
from agent_runtime.settings import RuntimeSettings

app = FastAPI(title="Agent Runtime", version="2.0.0")
settings = RuntimeSettings()
artifact_store = S3ArtifactStore(settings) if settings.object_storage_endpoint else None
try:
    job_store = PostgresRuntimeJobStore(settings.database_url) if settings.database_url else None
    _store_error = False
except RuntimeDatabaseUnavailable:
    job_store = None
    _store_error = True


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    if _store_error:
        raise HTTPException(status_code=503, detail="runtime database unavailable")
    if job_store is not None and not await asyncio.to_thread(job_store.ready):
        raise HTTPException(status_code=503, detail="runtime database unavailable")
    if artifact_store is not None:
        try:
            available = await asyncio.to_thread(artifact_store.ready)
        except Exception:
            available = False
        if not available:
            raise HTTPException(status_code=503, detail="artifact bucket unavailable")
    return {"status": "ready"}

