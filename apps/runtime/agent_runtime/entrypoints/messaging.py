"""Run one independently scalable Runtime messaging role."""

import argparse
import asyncio
import logging
import signal
from threading import Event

from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent
from agent_runtime.execution.worker import DurableWorker, load_handler
from agent_runtime.infrastructure.mq.rocketmq import RocketMQRuntimeClient
from agent_runtime.infrastructure.postgres.messaging import RuntimeMQStore
from agent_runtime.settings import RuntimeSettings

logger = logging.getLogger(__name__)


async def execute_until_stopped(worker, job, stop):
    task = asyncio.create_task(worker.execute(job))

    async def shutdown():
        while not stop.is_set():
            await asyncio.sleep(0.2)

    monitor = asyncio.create_task(shutdown())
    try:
        done, _ = await asyncio.wait({task, monitor}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
    finally:
        task.cancel()
        monitor.cancel()
        await asyncio.gather(task, monitor, return_exceptions=True)


def run(settings, role, stop):
    store = RuntimeMQStore(settings.database_url)
    client = None
    try:
        if role in {"worker", "memory-worker"}:
            path = settings.memory_handler if role == "memory-worker" else settings.worker_handler
            handler = load_handler(path)
            queue = "memory" if role == "memory-worker" else "runtime"
            worker = DurableWorker(store, settings, handler)
            while not stop.is_set():
                worked = False
                for workspace in store.scopes():
                    if stop.is_set():
                        break
                    job = store.claim_next(workspace, queue, settings.worker_id, settings.job_lease_seconds)
                    if job:
                        worked = True
                        try:
                            asyncio.run(execute_until_stopped(worker, job, stop))
                        except Exception as exc:
                            logger.error("worker execution interrupted: %s", type(exc).__name__)
                if not worked:
                    stop.wait(1)
            return
        client = RocketMQRuntimeClient(settings, role)
        client.start()
        handlers = {
            "dispatch": lambda body: store.accept_task(RuntimeCommand.model_validate_json(body)),
            "control": lambda body: store.accept_control(RuntimeCommand.model_validate_json(body)),
            "memory": lambda body: store.accept_task(RuntimeCommand.model_validate_json(body), "memory"),
            "memory-scheduler": lambda body: store.schedule_memory(
                RuntimeEvent.model_validate_json(body), settings.rocketmq_memory_topic
            ),
        }
        while not stop.is_set():
            try:
                if role == "relay":
                    for workspace in store.scopes():
                        store.relay(workspace, settings.rocketmq_event_topic, client.publish, 5)
                    stop.wait(1)
                else:
                    client.receive(handlers[role])
            except Exception as exc:
                logger.error("MQ %s iteration failed: %s", role, type(exc).__name__)
                stop.wait(1)
    finally:
        if client:
            client.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "role", choices=["dispatch", "control", "relay", "worker", "memory", "memory-worker", "memory-scheduler"]
    )
    parser.add_argument("--env-file", default=".env.runtime")
    args = parser.parse_args()
    settings = RuntimeSettings(_env_file=args.env_file)
    if not settings.database_url or not settings.rocketmq_endpoint:
        parser.error("RUNTIME_DATABASE_URL and RUNTIME_ROCKETMQ_ENDPOINT are required")
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO)
    run(settings, args.role, stop)


if __name__ == "__main__":
    main()
