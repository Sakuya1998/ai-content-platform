"""Database-leased execution with heartbeat, cancellation and bounded retries."""

import asyncio
import importlib
import inspect

from agent_runtime.execution.context import _EXECUTION, ExecutionContext
from agent_runtime.execution.errors import JobCancelled, RetryableTaskError, WaitingReview
from agent_runtime.execution.leases import FencingTokenError


def load_handler(path):
    legacy_builtins = {
        "novel_agent.platform.handlers:generate_content": "agent_runtime.execution.handlers:generate_content",
        "novel_agent.platform.handlers:refresh_memory": "agent_runtime.execution.handlers:refresh_memory",
    }
    path = legacy_builtins.get(path, path)
    module, separator, name = path.partition(":")
    if not separator or not module or not name:
        raise ValueError("handler must be a trusted module:function configuration")
    handler = getattr(importlib.import_module(module), name)
    if not inspect.iscoroutinefunction(handler):
        raise ValueError("handler must be an async callable")
    return handler


class DurableWorker:
    def __init__(self, store, settings, handler):
        self.store, self.settings, self.handler = store, settings, handler

    async def execute(self, job):
        execution = ExecutionContext(job, self.store, self.settings)

        async def invoke_handler():
            token = _EXECUTION.set(execution)
            try:
                return await self.handler(job.command)
            finally:
                _EXECUTION.reset(token)

        task = asyncio.create_task(invoke_handler())

        async def heartbeat():
            while True:
                await asyncio.sleep(self.settings.job_heartbeat_seconds)
                await asyncio.to_thread(self.store.renew, job, self.settings.worker_id, self.settings.job_lease_seconds)

        pulse = asyncio.create_task(heartbeat())
        status, result, retry = "failed", {}, 0
        try:
            done, _ = await asyncio.wait({task, pulse}, return_when=asyncio.FIRST_COMPLETED)
            if pulse in done:
                try:
                    await pulse
                except JobCancelled, FencingTokenError:
                    raise
                except Exception:
                    raise FencingTokenError("worker heartbeat could not be confirmed") from None
            result = await task
            if not isinstance(result, dict):
                raise ValueError("handler must return an object")
            status = "waiting_review" if isinstance(result, WaitingReview) else "completed"
        except JobCancelled:
            execution.cancel_requested = True
            status, result = "cancelled", {}
        except FencingTokenError:
            raise
        except RetryableTaskError as exc:
            status = "accepted" if job.attempt < self.settings.worker_max_attempts else "failed"
            result = {"type": type(exc).__name__}
            retry = self.settings.worker_retry_seconds * 2 ** min(job.attempt - 1, 10)
        except asyncio.CancelledError:
            # Process shutdown leaves the lease to expire, allowing safe recovery.
            raise
        except Exception as exc:
            result = {"type": type(exc).__name__}
        finally:
            task.cancel()
            pulse.cancel()
            await asyncio.gather(task, pulse, return_exceptions=True)
        await asyncio.to_thread(self.store.finish, job, self.settings.worker_id, status, result, retry_seconds=retry)
