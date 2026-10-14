import pytest

from agent_runtime.domain.protocol import RuntimeCommand
from agent_runtime.infrastructure.mq.rocketmq import command_message, consume_command, sdk_configuration
from agent_runtime.settings import RuntimeSettings


def test_poison_message_does_not_block_successful_sibling():
    from rocketmq.v5.model import Message

    from agent_runtime.infrastructure.mq.rocketmq import RocketMQRuntimeClient

    bad, good = Message(), Message()
    bad.body, good.body = b"invalid", b"valid"
    acknowledged = []

    class Consumer:
        def receive(self, *args):
            return [bad, good]

        def ack(self, message):
            acknowledged.append(message.body)

    client = object.__new__(RocketMQRuntimeClient)
    client.consumer = Consumer()
    processed = []

    def accept(body):
        processed.append(body)
        if body == b"invalid":
            raise ValueError()

    with pytest.raises(ExceptionGroup):
        client.receive(accept)
    assert processed == [b"invalid", b"valid"]
    assert acknowledged == [b"valid"]


def test_topics_and_groups_are_environment_scoped_and_control_is_separate():
    settings = RuntimeSettings(_env_file=None, environment="prod")
    assert settings.rocketmq_task_topic == "agent-prod-runtime-tasks"
    assert settings.rocketmq_control_topic == "agent-prod-runtime-control"
    assert settings.rocketmq_event_topic == "agent-prod-runtime-events"
    assert settings.rocketmq_memory_topic == "agent-prod-memory-tasks"
    assert (
        len(
            {
                settings.rocketmq_dispatch_group,
                settings.rocketmq_control_group,
                settings.rocketmq_memory_group,
                settings.rocketmq_memory_scheduler_group,
            }
        )
        == 4
    )


def test_sdk_configuration_requires_proxy_endpoint():
    with pytest.raises(ValueError, match="Proxy"):
        sdk_configuration(RuntimeSettings(_env_file=None))


@pytest.mark.parametrize("role,missing", [("relay", "events"), ("relay", "memory"), ("dispatch", "tasks")])
def test_start_rejects_missing_topic_routes_even_when_sdk_startup_succeeds(monkeypatch, role, missing):
    from types import SimpleNamespace

    from agent_runtime.infrastructure.mq import rocketmq as messaging

    settings = RuntimeSettings(_env_file=None, rocketmq_endpoint="proxy.test:8081")
    missing_topic = {
        "events": settings.rocketmq_event_topic,
        "memory": settings.rocketmq_memory_topic,
        "tasks": settings.rocketmq_task_topic,
    }[missing]
    clients = []

    class SDKClient:
        def __init__(self, *args, **kwargs):
            self.route_manager = self
            self.closed = False
            clients.append(self)

        def startup(self):
            pass

        def shutdown(self):
            self.closed = True

        def retrieve_topic_route_data(self, topic):
            if topic == missing_topic:
                raise RuntimeError("SDK route cache unavailable")
            return SimpleNamespace(message_queues=[object()])

    monkeypatch.setattr(messaging, "Producer", SDKClient)
    monkeypatch.setattr(messaging, "SimpleConsumer", SDKClient)
    client = messaging.RocketMQRuntimeClient(settings, role)
    with pytest.raises(ValueError, match="topic route unavailable"):
        client.start()
    assert all(sdk.closed for sdk in clients)


def test_start_checks_routes_without_sending_or_receiving_messages(monkeypatch):
    from types import SimpleNamespace

    from agent_runtime.infrastructure.mq import rocketmq as messaging

    settings = RuntimeSettings(_env_file=None, rocketmq_endpoint="proxy.test:8081")
    inspected = []

    class SDKClient:
        def __init__(self, *args, **kwargs):
            self.route_manager = self

        def startup(self):
            pass

        def shutdown(self):
            pass

        def retrieve_topic_route_data(self, topic):
            inspected.append(topic)
            return SimpleNamespace(message_queues=[object()])

    monkeypatch.setattr(messaging, "Producer", SDKClient)
    monkeypatch.setattr(messaging, "SimpleConsumer", SDKClient)
    client = messaging.RocketMQRuntimeClient(settings, "control")
    try:
        client.start()
        assert set(inspected) == {
            settings.rocketmq_event_topic,
            settings.rocketmq_memory_topic,
            settings.rocketmq_control_topic,
        }
    finally:
        client.close()


def test_real_sdk_message_round_trips_command_and_acks_after_acceptance():
    command = RuntimeCommand(
        command_id="mq-1",
        command_type="workflow.run",
        workspace_id="ws-1",
        project_id="project-1",
        job_id="job-1",
        workflow_run_id="run-1",
        capability_snapshot_hash="a" * 64,
        trace_id="trace-1",
        idempotency_key="mq-1",
    )
    message = command_message(command, "commands")
    calls = []
    consume_command(message, lambda value: calls.append(value.command_id), lambda value: calls.append("ack"))
    assert message.topic == "commands"
    assert message.tag == "workflow.run"
    assert calls == ["mq-1", "ack"]

    def fail(_value):
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError):
        consume_command(message, fail, lambda value: calls.append("unexpected ack"))
    assert "unexpected ack" not in calls
