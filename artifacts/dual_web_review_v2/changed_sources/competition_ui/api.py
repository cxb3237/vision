"""Small, deliberately restricted API used by the competition page."""

from __future__ import annotations

from typing import Any


class CompetitionAPI:
    def __init__(self, runtime: Any, media: Any) -> None:
        self.runtime = runtime
        self.media = media

    def health(self) -> tuple[int, dict[str, Any]]:
        status = self.runtime.get_status_snapshot()
        return 200, {
            "ok": True,
            "service": "vision-competition-ui",
            "runtime_running": bool(status.get("runtime_running", False)),
        }

    def status(self) -> tuple[int, dict[str, Any]]:
        runtime_status = self.runtime.get_status_snapshot()
        safe = {
            key: runtime_status.get(key)
            for key in (
                "runtime_running",
                "camera_online",
                "latest_frame_age_s",
                "camera_fps",
                "vision_fps",
                "competition_mode",
                "vision_output_enabled",
            )
        }
        safe.update(self.media.status())
        safe["ui"] = {
            "status_poll_interval_ms": self.media.config.status_poll_interval_ms
        }
        return 200, {"ok": True, "status": safe}

    def start_recording(self, body: object) -> tuple[int, dict[str, Any]]:
        if body != {}:
            return 400, {"ok": False, "message": "请求体必须为空对象"}
        try:
            recording = self.media.recorder.request_start()
        except RuntimeError as exc:
            return 503, {"ok": False, "message": str(exc)}
        ok = recording.get("state") != "ERROR"
        return (202 if ok else 507), {"ok": ok, "recording": recording}

    def stop_recording(self, body: object) -> tuple[int, dict[str, Any]]:
        if body != {}:
            return 400, {"ok": False, "message": "请求体必须为空对象"}
        recording = self.media.recorder.request_stop()
        return 200, {"ok": True, "recording": recording}

    def recordings(self) -> tuple[int, dict[str, Any]]:
        return 200, {"ok": True, "recordings": self.media.recorder.list_recordings()}
