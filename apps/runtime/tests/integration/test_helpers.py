"""Guard the Runtime boundary while allowing deterministic vendor HTTP."""

import httpx
import pytest

from tests.integration.helpers import forbid_control_business_http, submit_generation


def test_public_generation_helper_submits_workflow_version_and_returns_receipt():
    def handle(request):
        import json

        assert request.url.path == "/api/v2/jobs"
        body = json.loads(request.content)
        assert body["workflow"] == "media.image" and body["workflow_version"] == 1
        assert body["document_type"] == "image"
        return httpx.Response(202, json={"job_id": "job"})

    with httpx.Client(base_url="http://control.test", transport=httpx.MockTransport(handle)) as api:
        assert submit_generation(api, {"project_id": "project", "document_type": "image"}).job_id == "job"


def test_worker_http_guard_rejects_control_but_allows_vendor_requests():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"vendor": True}))
    with httpx.Client(transport=transport) as client, forbid_control_business_http("http://control.test:8080"):
        with pytest.raises(AssertionError, match="Runtime attempted an HTTP request to Control"):
            client.get("http://control.test:8080/api/v2/jobs/job")
        assert client.get("http://vendor.test:8080/v1/chat/completions").json() == {"vendor": True}


async def test_async_worker_http_guard_rejects_control_requests():
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    with forbid_control_business_http("http://control.test:8080"):
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(AssertionError, match="Runtime attempted an HTTP request to Control"):
                await client.get("http://control.test:8080/api/v2/content/versions/version")
