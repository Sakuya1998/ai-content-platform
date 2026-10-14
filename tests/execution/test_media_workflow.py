from types import SimpleNamespace

import httpx
import pytest

from agent_runtime.execution.errors import WaitingReview
from agent_runtime.settings import RuntimeSettings


@pytest.mark.asyncio
async def test_unknown_submit_returns_review_without_repeating_remote_request():
    from agent_runtime.workflows.media.video import execute_video_task

    class Store:
        task = SimpleNamespace(state="submission_unknown", request=None, remote_task_id=None)

        def get(self, *args):
            return self.task

    class Provider:
        async def submit(self, request):
            raise AssertionError("ambiguous submission must never be repeated")

    result = await execute_video_task(Store(), object(), "worker", Provider(), RuntimeSettings(_env_file=None))
    assert isinstance(result, WaitingReview)
    assert result["media_status"] == "submission_unknown"


@pytest.mark.asyncio
async def test_rejected_plan_remains_waiting_after_a_crash():
    from agent_runtime.workflows.media.video import execute_video_task

    class Store:
        def get(self, *args):
            return SimpleNamespace(state="prepared", review={"approved": False, "notes": ["revise"]})

        def begin_submit(self, *args):
            raise AssertionError("recovery must respect persisted review")

    result = await execute_video_task(Store(), object(), "worker", object(), RuntimeSettings(_env_file=None))
    assert isinstance(result, WaitingReview)
    assert result["media_status"] == "prepared"


@pytest.mark.asyncio
async def test_transport_failure_after_submit_is_not_a_business_retry():
    from agent_runtime.domain.media import VideoRequest
    from agent_runtime.infrastructure.media.errors import SubmissionUnknown
    from agent_runtime.workflows.media.video import execute_video_task

    class Store:
        state = "prepared"

        def get(self, *args):
            return SimpleNamespace(state=self.state, review={"approved": True}, request=VideoRequest(prompt="shot"))

        def begin_submit(self, *args):
            self.state = "submitting"

        def mark_unknown(self, *args):
            self.state = "submission_unknown"

    class Provider:
        async def submit(self, request):
            raise SubmissionUnknown("provider may have accepted")

    store = Store()
    result = await execute_video_task(store, object(), "worker", Provider(), RuntimeSettings(_env_file=None))
    assert store.state == "submission_unknown"
    assert isinstance(result, WaitingReview)


@pytest.mark.asyncio
async def test_explicit_reconciliation_attaches_confirmed_remote_task_without_submit():
    from agent_runtime.domain.media import VideoTaskStatus
    from agent_runtime.workflows.media.video import execute_video_task

    class Store:
        def get(self, *args):
            return SimpleNamespace(state="submission_unknown")

        def submitted(self, job, worker, remote_id):
            assert remote_id == "operator-confirmed-id"

        def update_status(self, job, worker, status):
            return SimpleNamespace(state=status.state)

    class Provider:
        async def status(self, remote_id):
            assert remote_id == "operator-confirmed-id"
            return VideoTaskStatus(state="succeeded", content_url="https://provider.invalid/video.mp4")

    result = await execute_video_task(
        Store(),
        object(),
        "worker",
        Provider(),
        RuntimeSettings(_env_file=None),
        resume_input={"action": "reconcile", "remote_task_id": "operator-confirmed-id"},
    )
    assert result.state == "succeeded"


@pytest.mark.asyncio
async def test_failed_reconciliation_lookup_keeps_manual_resume_available():
    from agent_runtime.domain.media import VideoTaskStatus
    from agent_runtime.infrastructure.media.errors import VideoProviderError
    from agent_runtime.workflows.media.video import execute_video_task

    class Store:
        remote_id = None

        def get(self, *args):
            return SimpleNamespace(state="submission_unknown")

        def submitted(self, job, worker, remote_id):
            self.remote_id = remote_id

        def update_status(self, job, worker, status):
            return SimpleNamespace(state=status.state)

    class Provider:
        failed = False

        async def status(self, remote_id):
            if not self.failed:
                self.failed = True
                raise VideoProviderError("temporary HTTP 502")
            return VideoTaskStatus(state="succeeded", content_url="https://provider.invalid/video.mp4")

    store, provider = Store(), Provider()
    args = (store, object(), "worker", provider, RuntimeSettings(_env_file=None))
    resume = {"action": "reconcile", "remote_task_id": "confirmed-id"}
    first = await execute_video_task(*args, resume_input=resume)
    assert isinstance(first, WaitingReview) and first["media_status"] == "submission_unknown"
    assert store.remote_id is None
    second = await execute_video_task(*args, resume_input=resume)
    assert second.state == "succeeded" and store.remote_id == "confirmed-id"


@pytest.mark.asyncio
async def test_cancel_marker_seen_during_poll_attempts_provider_cancel():
    from agent_runtime.execution.errors import JobCancelled
    from agent_runtime.workflows.media.video import execute_video_task

    class Store:
        confirmed = False

        def get(self, *args):
            return SimpleNamespace(state="cancel_pending", remote_task_id="remote-id")

        def mark_cancelled(self, *args):
            self.confirmed = True

    class Provider:
        route = SimpleNamespace(video_capabilities=SimpleNamespace(cancellation=True))

        async def cancel(self, remote_id):
            assert remote_id == "remote-id"

    store = Store()
    with pytest.raises(JobCancelled):
        await execute_video_task(store, object(), "worker", Provider(), RuntimeSettings(_env_file=None))
    assert store.confirmed


@pytest.mark.asyncio
async def test_remote_id_is_reused_on_runtime_recovery():
    from agent_runtime.workflows.media.video import execute_video_task

    class Store:
        task = SimpleNamespace(state="running", remote_task_id="original", request=None)

        def get(self, *args):
            return self.task

        def update_status(self, job, worker, status):
            self.task = SimpleNamespace(state=status.state, content_url=status.content_url)
            return self.task

    class Provider:
        async def submit(self, request):
            raise AssertionError("recovery must not create another video")

        async def status(self, remote_id):
            from agent_runtime.domain.media import VideoTaskStatus

            assert remote_id == "original"
            return VideoTaskStatus(state="succeeded", content_url="https://provider.invalid/video.mp4")

    result = await execute_video_task(Store(), object(), "worker", Provider(), RuntimeSettings(_env_file=None))
    assert result.state == "succeeded"


@pytest.mark.asyncio
async def test_provider_video_download_is_bounded_and_does_not_send_api_key():
    from agent_runtime.workflows.media.video import download_video

    def serve(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=b"video")

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        assert await download_video("https://provider.invalid/video.mp4", 5, client=client) == b"video"
        with pytest.raises(ValueError, match="size"):
            await download_video("https://provider.invalid/video.mp4", 4, client=client)
