"""Connect to RocketMQ Proxy with the Runtime SDK without publishing messages."""

import argparse

from agent_runtime.infrastructure.mq.rocketmq import RocketMQRuntimeClient
from agent_runtime.settings import RuntimeSettings


def main() -> int:
    parser = argparse.ArgumentParser(description="Check RocketMQ 5.x Proxy using the Python SDK")
    parser.add_argument("--env-file", default=".env.runtime")
    args = parser.parse_args()
    settings = RuntimeSettings(_env_file=args.env_file)
    if not settings.rocketmq_endpoint:
        parser.error("RUNTIME_ROCKETMQ_ENDPOINT is required")
    client = RocketMQRuntimeClient(settings)
    try:
        client.start()
    except Exception as exc:
        print(f"RocketMQ Proxy SDK check failed: {type(exc).__name__}")
        return 1
    try:
        print("RocketMQ Proxy Python SDK clients started")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
