"""Execute one explicitly scoped accepted Runtime job for operational verification."""

import argparse
import asyncio

from agent_runtime.execution.handlers import generate_content
from agent_runtime.execution.worker import DurableWorker
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from agent_runtime.settings import RuntimeSettings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    settings = RuntimeSettings(_env_file=None)
    store = RuntimeMQStore(settings.database_url)
    try:
        expected = store.get_scoped(args.job_id, args.workspace_id)
        if expected.status not in {"accepted", "running"}:
            raise ValueError("job is not eligible for execution")
        job = store.claim_next(
            args.workspace_id, "runtime", settings.worker_id, settings.job_lease_seconds, job_id=args.job_id
        )
        if job is None or job.command.job_id != args.job_id:
            raise ValueError("specified job was not the next eligible scoped task")
        asyncio.run(DurableWorker(store, settings, generate_content).execute(job))
        finished = store.get_scoped(args.job_id, args.workspace_id)
        print("Runtime job status: " + finished.status)
        return 0 if finished.status == "completed" else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
