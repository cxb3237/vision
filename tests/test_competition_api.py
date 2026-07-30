from __future__ import annotations

from types import SimpleNamespace

from competition_ui.api import CompetitionAPI


class FakeRecorder:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def request_start(self):
        self.starts += 1
        return {"state": "RECORDING", "active": True}

    def request_stop(self):
        self.stops += 1
        return {"state": "IDLE", "active": False}

    def list_recordings(self):
        return [{"file_name": "H_1.mp4"}]


class FakeMedia:
    def __init__(self) -> None:
        self.recorder = FakeRecorder()
        self.config = SimpleNamespace(status_poll_interval_ms=500)

    def status(self):
        return {
            "recording": {"active": False},
            "storage": {"free_bytes": 100},
            "media_worker_alive": False,
            "last_media_error": "比赛媒体帧处理失败",
            "last_media_error_at": 123.0,
        }


class FakeRuntime:
    def get_status_snapshot(self):
        return {
            "runtime_running": True,
            "camera_online": True,
            "serial_online": True,
            "mcu_status": {"secret": "must not leak"},
        }


def test_recording_start_stop_are_idempotent_delegations() -> None:
    media = FakeMedia()
    api = CompetitionAPI(FakeRuntime(), media)
    assert api.start_recording({})[0] == 202
    assert api.start_recording({})[0] == 202
    assert api.stop_recording({})[0] == 200
    assert api.stop_recording({})[0] == 200
    assert media.recorder.starts == 2
    assert media.recorder.stops == 2


def test_recording_endpoints_only_accept_empty_json_object() -> None:
    api = CompetitionAPI(FakeRuntime(), FakeMedia())
    for body in (None, [], {"path": "elsewhere"}, ""):
        assert api.start_recording(body)[0] == 400
        assert api.stop_recording(body)[0] == 400


def test_competition_status_exposes_media_but_not_detailed_mcu_control() -> None:
    status_code, payload = CompetitionAPI(FakeRuntime(), FakeMedia()).status()
    assert status_code == 200
    assert payload["status"]["camera_online"] is True
    assert "mcu_status" not in payload["status"]
    assert payload["status"]["storage"]["free_bytes"] == 100
    assert payload["status"]["media_worker_alive"] is False
    assert payload["status"]["last_media_error"] == "比赛媒体帧处理失败"
