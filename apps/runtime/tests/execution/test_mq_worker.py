import asyncio

import pytest

from agent_runtime.execution.errors import RetryableTaskError
from agent_runtime.execution.worker import DurableWorker
from agent_runtime.settings import RuntimeSettings


def test_previous_builtin_handler_configuration_resolves_to_current_module():
    from agent_runtime.execution.handlers import generate_content, refresh_memory
    from agent_runtime.execution.worker import load_handler

    assert load_handler("novel_agent.platform.handlers:generate_content") is generate_content
    assert load_handler("novel_agent.platform.handlers:refresh_memory") is refresh_memory
    assert load_handler("agent_runtime.execution.handlers:generate_content") is generate_content


class Store:
    def __init__(self):
        self.outcome = None
        self.renewed = 0

    def renew(self, job, worker, lease):
        self.renewed += 1

    def finish(self, job, worker, status, result, **kwargs):
        self.outcome = (status, result, kwargs)


def job():
    from agent_runtime.execution.leases import RuntimeJob
    from tests.execution.test_worker import make_command

    return RuntimeJob(command=make_command(), fencing_token=1, attempt=1)


async def test_worker_renews_lease_while_handler_runs_and_finishes():
    store = Store()
    settings = RuntimeSettings(_env_file=None, job_lease_seconds=5, job_heartbeat_seconds=1)

    async def handler(command):
        await asyncio.sleep(1.1)
        return {"artifact_id": "generated"}

    await DurableWorker(store, settings, handler).execute(job())
    assert store.renewed == 1
    assert store.outcome[0:2] == ("completed", {"artifact_id": "generated"})


async def test_retryable_failure_schedules_database_retry():
    store = Store()

    async def handler(command):
        raise RetryableTaskError("provider rate limit")

    await DurableWorker(store, RuntimeSettings(_env_file=None), handler).execute(job())
    assert store.outcome[0] == "accepted"
    assert store.outcome[2]["retry_seconds"] == 30


async def test_worker_cancels_handler_on_durable_cancel_request():
    from agent_runtime.execution.errors import JobCancelled

    store = Store()

    def renew(*args):
        raise JobCancelled()

    store.renew = renew
    stopped = asyncio.Event()

    async def handler(command):
        try:
            await asyncio.sleep(10)
        finally:
            from agent_runtime.execution.context import current_execution

            assert current_execution().cancel_requested
            stopped.set()

    settings = RuntimeSettings(_env_file=None, job_lease_seconds=5, job_heartbeat_seconds=1)
    await DurableWorker(store, settings, handler).execute(job())
    assert stopped.is_set()
    assert store.outcome[0] == "cancelled"


async def test_heartbeat_transport_failure_leaves_job_for_lease_recovery():
    from agent_runtime.execution.leases import FencingTokenError

    store = Store()
    store.renew = lambda *args: (_ for _ in ()).throw(ConnectionError("database unavailable"))
    stopped = asyncio.Event()

    async def handler(command):
        try:
            await asyncio.sleep(10)
        finally:
            from agent_runtime.execution.context import current_execution

            assert not current_execution().cancel_requested
            stopped.set()

    settings = RuntimeSettings(_env_file=None, job_lease_seconds=5, job_heartbeat_seconds=1)
    with pytest.raises(FencingTokenError, match="heartbeat"):
        await DurableWorker(store, settings, handler).execute(job())
    assert stopped.is_set() and store.outcome is None


def test_handler_configuration_cannot_be_supplied_by_a_message():
    from agent_runtime.execution.worker import load_handler

    with pytest.raises(ValueError, match="module:function"):
        load_handler("")


async def test_handler_receives_the_claimed_execution_token_and_context_is_reset():
    from agent_runtime.execution.context import current_execution

    observed = []

    async def handler(command):
        context = current_execution()
        observed.append((context.job.fencing_token, context.job.command.job_id))
        return {"ok": True}

    claimed = job()
    await DurableWorker(Store(), RuntimeSettings(_env_file=None), handler).execute(claimed)
    assert observed == [(claimed.fencing_token, claimed.command.job_id)]
    assert current_execution(required=False) is None
