"""触摸界面使用的线程安全只读快照存储。"""

from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any

from touch_ui.models import CommandStatus, RuntimeCommand


class StateStore:
    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = deepcopy(initial or {})
        self._commands: dict[str, dict[str, Any]] = {}

    def update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(deepcopy(values))

    def update_nested(self, name: str, values: dict[str, Any]) -> None:
        with self._lock:
            current = dict(self._state.get(name, {}))
            current.update(deepcopy(values))
            self._state[name] = current

    def add_command(self, command: RuntimeCommand) -> None:
        with self._lock:
            self._commands[command.command_id] = {
                "command_id": command.command_id,
                "type": command.command_type.value,
                "status": CommandStatus.QUEUED.value,
                "message": "",
            }
            self._trim_commands()

    def set_command_status(
        self,
        command_id: str,
        status: CommandStatus,
        message: str = "",
    ) -> None:
        with self._lock:
            record = self._commands.setdefault(command_id, {"command_id": command_id})
            record.update({"status": status.value, "message": str(message)})
            self._state["last_command"] = deepcopy(record)

    def _trim_commands(self, maximum: int = 200) -> None:
        while len(self._commands) > maximum:
            oldest = next(iter(self._commands))
            del self._commands[oldest]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = deepcopy(self._state)
            result["commands"] = deepcopy(self._commands)
            return result

    def command_snapshot(self, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._commands.get(command_id)
            return deepcopy(value) if value is not None else None
