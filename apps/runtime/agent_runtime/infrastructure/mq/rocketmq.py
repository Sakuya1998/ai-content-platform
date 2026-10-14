"""RocketMQ 5 gRPC Proxy adapter using rocketmq-python-client."""

from collections.abc import Callable

from rocketmq.v5.client import ClientConfiguration, Credentials
from rocketmq.v5.consumer import SimpleConsumer
from rocketmq.v5.model import FilterExpression, Message
from rocketmq.v5.producer import Producer

from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent
from agent_runtime.settings import RuntimeSettings


def sdk_configuration(settings: RuntimeSettings) -> ClientConfiguration:
    if not settings.rocketmq_endpoint.strip():
        raise ValueError("RocketMQ Proxy endpoint is required")
    return ClientConfiguration(
        settings.rocketmq_endpoint,
        Credentials(
            settings.rocketmq_access_key.get_secret_value(),
            settings.rocketmq_secret_key.get_secret_value(),
        ),
        namespace=settings.rocketmq_namespace,
    )


def command_message(command: RuntimeCommand, topic: str) -> Message:
    message = Message()
    message.topic = topic
    message.body = command.model_dump_json().encode("utf-8")
    message.keys = command.command_id
    message.tag = command.command_type
    return message


def consume_command(message: Message, accept: Callable, ack: Callable) -> None:
    command = RuntimeCommand.model_validate_json(message.body)
    accept(command)
    ack(message)


class RocketMQRuntimeClient:
    """Explicit lifecycle: call start/close outside the ASGI request loop."""

    def __init__(self, settings: RuntimeSettings, role: str = "dispatch"):
        cfg = sdk_configuration(settings)
        self.settings = settings
        self.producer_topics = (settings.rocketmq_event_topic, settings.rocketmq_memory_topic)
        self.consumer_topic = None
        self.producer = Producer(
            cfg,
            topics=list(self.producer_topics),
            tls_enable=settings.rocketmq_tls_enabled,
        )
        if role == "relay":
            self.consumer = None
            return
        channels = {
            "dispatch": (settings.rocketmq_dispatch_group, settings.rocketmq_task_topic, "*"),
            "control": (settings.rocketmq_control_group, settings.rocketmq_control_topic, "*"),
            "memory": (settings.rocketmq_memory_group, settings.rocketmq_memory_topic, "*"),
            "memory-scheduler": (
                settings.rocketmq_memory_scheduler_group,
                settings.rocketmq_event_topic,
                "content.version.committed",
            ),
        }
        group, topic, expression = channels[role]
        self.consumer_topic = topic
        self.consumer = SimpleConsumer(
            cfg,
            group,
            {topic: FilterExpression(expression)},
            await_duration=5,
            tls_enable=settings.rocketmq_tls_enabled,
        )

    def start(self) -> None:
        try:
            self.producer.startup()
            for topic in self.producer_topics:
                self._check_route(self.producer, topic)
            if self.consumer:
                self.consumer.startup()
                self._check_route(self.consumer, self.consumer_topic)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check_route(client, topic):
        # SDK startup logs route failures without raising; explicitly require
        # queues before reporting readiness or starting a message process.
        try:
            route = client.route_manager.retrieve_topic_route_data(topic)
            if not route.message_queues:
                raise ValueError("empty topic route")
        except Exception:
            raise ValueError("RocketMQ topic route unavailable: " + topic) from None

    def close(self) -> None:
        try:
            if self.consumer:
                self.consumer.shutdown()
        finally:
            self.producer.shutdown()

    def receive_once(self, accept: Callable, batch_size: int = 16) -> int:
        return self.receive(lambda body: accept(RuntimeCommand.model_validate_json(body)), batch_size)

    def publish_event(self, event: RuntimeEvent):
        message = Message()
        message.topic = self.settings.rocketmq_event_topic
        message.body = event.model_dump_json().encode("utf-8")
        message.keys = event.event_id
        message.tag = event.event_type
        return self.producer.send(message)

    def publish(self, topic: str, key: str, tag: str, body: bytes):
        message = Message()
        message.topic, message.keys, message.tag, message.body = topic, key, tag, body
        return self.producer.send(message)

    def receive(self, handle: Callable, batch_size: int = 16) -> int:
        messages = self.consumer.receive(batch_size, 60)
        errors = []
        for message in messages:
            try:
                handle(message.body)
                self.consumer.ack(message)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("MQ batch has unacknowledged messages", errors)
        return len(messages)
