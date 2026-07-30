"""触摸Web接口的纯业务层；不会直接访问摄像头或V4L2。"""

from __future__ import annotations

from typing import Any, Protocol

from touch_ui.models import CommandType


ALLOWED_CONTROLS = {
    "white_balance_automatic",
    "white_balance_temperature",
    "exposure_auto",
    "exposure_absolute",
    "focus_auto",
    "focus_absolute",
    "gain",
    "brightness",
    "contrast",
    "saturation",
    "hue",
    "gamma",
    "sharpness",
    "backlight_compensation",
    "power_line_frequency",
}
class RuntimeAPI(Protocol):
    def submit_command(self, command_type: CommandType | str, payload: dict[str, Any] | None = None) -> str: ...
    def get_status_snapshot(self) -> dict[str, Any]: ...
    def get_runtime_config_snapshot(self) -> dict[str, Any]: ...


def api_error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"ok": False, "error_code": code, "message": message}


class TouchAPI:
    def __init__(self, runtime: RuntimeAPI) -> None:
        self.runtime = runtime

    def health(self) -> tuple[int, dict[str, Any]]:
        status = self.runtime.get_status_snapshot()
        return 200, {
            "ok": True,
            "service": "vision-touch-ui",
            "runtime_running": bool(status.get("runtime_running", False)),
        }

    def status(self) -> tuple[int, dict[str, Any]]:
        return 200, {"ok": True, "status": self.runtime.get_status_snapshot()}

    def camera_config(self) -> tuple[int, dict[str, Any]]:
        return 200, {"ok": True, "camera": self.runtime.get_runtime_config_snapshot()}

    def _submit(
        self,
        command_type: CommandType,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            command_id = self.runtime.submit_command(command_type, payload)
        except RuntimeError as exc:
            return api_error(409, "COMMAND_REJECTED", str(exc))
        except Exception as exc:
            return api_error(500, "COMMAND_QUEUE_ERROR", str(exc))
        return 202, {"ok": True, "command_id": command_id, "status": "QUEUED"}

    def patch_camera(self, body: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict):
            return api_error(400, "INVALID_BODY", "请求体必须为JSON对象")
        status = self.runtime.get_status_snapshot()
        if status.get("competition_mode"):
            return api_error(409, "COMPETITION_MODE", "比赛模式禁止修改参数")
        controls = body.get("controls")
        if not isinstance(controls, dict) or not controls:
            return api_error(400, "INVALID_CONTROLS", "controls必须为非空对象")
        if len(controls) != 1:
            return api_error(400, "ONE_CONTROL_AT_A_TIME", "每次请求只能修改一个控制项")
        name, value = next(iter(controls.items()))
        if name not in ALLOWED_CONTROLS:
            return api_error(400, "UNKNOWN_CONTROL", f"不允许的摄像头控制: {name}")
        if isinstance(value, bool) or not isinstance(value, int):
            return api_error(400, "INVALID_VALUE", f"{name}必须为整数")
        camera = self.runtime.get_runtime_config_snapshot()
        info = camera.get("controls", {}).get(name, {})
        if info and not info.get("supported", False):
            return api_error(409, "UNSUPPORTED_CONTROL", f"摄像头不支持{name}")
        minimum, maximum = info.get("minimum"), info.get("maximum")
        if minimum is not None and value < minimum or maximum is not None and value > maximum:
            return api_error(
                422,
                "OUT_OF_RANGE",
                f"{name}必须在{minimum}..{maximum}范围内",
            )
        controls_snapshot = camera.get("controls", {})
        if name == "white_balance_temperature":
            auto = controls_snapshot.get("white_balance_automatic", {}).get("actual")
            if auto not in (None, 0):
                return api_error(409, "AUTO_CONTROL_ACTIVE", "自动白平衡开启时不能设置色温")
        if name == "exposure_absolute":
            auto = controls_snapshot.get("exposure_auto", {}).get("actual")
            if auto not in (None, 1):
                return api_error(409, "AUTO_CONTROL_ACTIVE", "自动曝光开启时不能设置曝光值")
        return self._submit(
            CommandType.SET_CAMERA_CONTROL, {"name": name, "value": value}
        )

    def command(self, command_type: CommandType) -> tuple[int, dict[str, Any]]:
        status = self.runtime.get_status_snapshot()
        if status.get("competition_mode") and command_type not in {
            CommandType.EXIT_COMPETITION,
        }:
            return api_error(409, "COMPETITION_MODE", "比赛模式禁止该操作")
        return self._submit(command_type)
