import os

import pytest

from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from tests.infrastructure.test_mq_store import command


def test_explicit_job_claim_does_not_claim_other_jobs_in_same_workspace():
    url = os.getenv("MQ_TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("MQ_TEST_RUNTIME_DATABASE_URL is not configured")
    store = RuntimeMQStore(url)
    first = command()
    second = command(workspace_id=first.workspace_id)
    try:
        store.accept_task(first)
        store.accept_task(second)
        selected = store.claim_next(first.workspace_id, "runtime", "worker", 60, job_id=second.job_id)
        assert selected.command.job_id == second.job_id
        assert store.get_scoped(first.job_id, first.workspace_id).status == "accepted"
    finally:
        store.close()
