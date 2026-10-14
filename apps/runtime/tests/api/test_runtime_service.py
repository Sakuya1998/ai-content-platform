import pytest
from httpx import ASGITransport, AsyncClient

from agent_runtime.api.app import app


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
async def test_runtime_exposes_health_probes(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        response = await client.get(path)
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/internal/v1/commands/accept", "/internal/v1/operations/benchmarks"])
async def test_runtime_no_longer_accepts_business_http_requests(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        response = await client.post(path, json={})
    assert response.status_code == 404

