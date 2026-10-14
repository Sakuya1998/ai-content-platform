from httpx import ASGITransport, AsyncClient

from agent_runtime.api.app import app


async def test_artifacts_cannot_be_uploaded_through_runtime_http():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        response = await client.put(
            "/internal/v1/jobs/job-1/artifacts/asset-1/versions/version-1/content.json",
            content=b"must use object storage",
        )
    assert response.status_code == 404

