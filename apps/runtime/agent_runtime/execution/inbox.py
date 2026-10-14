"""Small runtime-side inbox abstraction used by gRPC and MQ adapters.

The in-memory implementation is intentionally deterministic for tests. Production
adapters should persist the same accept-before-ack semantics in Runtime DB.
"""

from threading import RLock

from agent_runtime.domain.protocol import RuntimeCommand


class CommandAlreadyAccepted(ValueError):
    """A command ID was already accepted with a different request body."""


class InMemoryCommandInbox:
    """Idempotent command inbox with the Runtime acceptance contract."""

    def __init__(self) -> None:
        self._commands: dict[str, RuntimeCommand] = {}
        self._lock = RLock()

    def accept(self, command: RuntimeCommand) -> RuntimeCommand:
        with self._lock:
            previous = self._commands.get(command.command_id)
            if previous is None:
                self._commands[command.command_id] = command
                return command
            if previous.model_dump(mode="json") != command.model_dump(mode="json"):
                raise CommandAlreadyAccepted(command.command_id)
            return previous

    def get(self, command_id: str) -> RuntimeCommand | None:
        with self._lock:
            return self._commands.get(command_id)
