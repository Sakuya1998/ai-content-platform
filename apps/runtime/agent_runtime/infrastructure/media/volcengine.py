"""Ark content-generation tasks protocol, verified against the official SDK.

https://github.com/volcengine/volcengine-python-sdk/blob/master/
volcenginesdkarkruntime/resources/content_generation/tasks.py
"""

import re
from urllib.parse import quote

import httpx

from agent_runtime.domain.media import VideoRequest, VideoRoute, VideoTaskStatus, _http_url
from agent_runtime.infrastructure.media.errors import VideoProviderError, VideoSubmissionUnknown


class VolcengineVideoProvider:
    def __init__(self, route: VideoRoute, *, client: httpx.AsyncClient | None = None):
        self.route = route
        self._client = client or httpx.AsyncClient(timeout=120, follow_redirects=False)
        self._owns_client = client is None
        base = route.base_url.rstrip("/")
        if not base.endswith("/api/v3"):
            base += "/api/v3"
        self._url = base + "/contents/generations/tasks"

    async def submit(self, request: VideoRequest) -> str:
        self.route.validate_request(request)
        parameters = dict(request.parameters)
        image = parameters.pop("image_url", None)
        content = [{"type": "text", "text": request.prompt}]
        if image:
            content.append({"type": "image_url", "image_url": {"url": image}, "role": "first_frame"})
        response = await self._request(
            "POST",
            self._url,
            submission=True,
            json={"model": self.route.model_name, "content": content, **parameters},
        )
        payload = self._payload(response, submission=True)
        identity = payload.get("id")
        if not isinstance(identity, str) or not identity.strip():
            raise VideoSubmissionUnknown("video submission returned no remote task ID")
        return identity

    async def status(self, remote_task_id: str) -> VideoTaskStatus:
        response = await self._request("GET", self._task_url(remote_task_id))
        payload = self._payload(response)
        states = {
            "queued": "running",
            "running": "running",
            "succeeded": "succeeded",
            "failed": "failed",
            "expired": "failed",
            "cancelled": "cancelled",
        }
        state = states.get(payload.get("status"))
        if state is None:
            raise VideoProviderError("video status response has unsupported state")
        if payload.get("id", remote_task_id) != remote_task_id:
            raise VideoProviderError("video status response task ID mismatch")
        if payload.get("model", self.route.model_name) != self.route.model_name:
            raise VideoProviderError("video status response model mismatch")
        content = payload.get("content") or {}
        url = content.get("video_url") if isinstance(content, dict) else None
        if state == "succeeded" and (not isinstance(url, str) or not _http_url(url)):
            raise VideoProviderError("successful video task has no valid content URL")
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            raise VideoProviderError("video task usage response is invalid")
        error = payload.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
        safe_error = {"code": code} if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) else {}
        return VideoTaskStatus(state=state, content_url=url, usage=usage, error=safe_error)

    async def cancel(self, remote_task_id: str) -> None:
        if not self.route.video_capabilities.cancellation:
            raise ValueError("model does not support cancellation")
        await self._request("DELETE", self._task_url(remote_task_id))

    async def delete(self, remote_task_id: str) -> None:
        await self.cancel(remote_task_id)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _task_url(self, identity: str) -> str:
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("remote video task ID is required")
        return self._url + "/" + quote(identity, safe="")

    async def _request(self, method: str, url: str, *, submission: bool = False, **kwargs):
        try:
            response = await self._client.request(
                method,
                url,
                headers={"Authorization": "Bearer " + self.route.api_key.get_secret_value()},
                **kwargs,
            )
        except httpx.HTTPError:
            error = VideoSubmissionUnknown if submission else VideoProviderError
            raise error("video provider transport failed") from None
        if response.is_success:
            return response
        error = (
            VideoSubmissionUnknown
            if submission and (response.status_code >= 500 or response.status_code == 408)
            else VideoProviderError
        )
        raise error(f"video provider returned HTTP {response.status_code}")

    @staticmethod
    def _payload(response: httpx.Response, *, submission: bool = False) -> dict:
        error = VideoSubmissionUnknown if submission else VideoProviderError
        try:
            payload = response.json()
        except ValueError:
            raise error("video provider returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise error("video provider returned invalid payload")
        return payload
