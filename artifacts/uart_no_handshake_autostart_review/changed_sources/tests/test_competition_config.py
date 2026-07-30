from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import app
from competition_ui.models import CompetitionUIConfigError, load_competition_ui_config


ROOT = Path(__file__).resolve().parents[1]


def test_default_competition_config_is_enabled_and_loopback_only() -> None:
    config = load_competition_ui_config(project_root=ROOT)
    assert config.enabled is True
    assert config.host == "127.0.0.1"
    assert config.port == 8000


def test_competition_config_rejects_non_loopback_host(tmp_path: Path) -> None:
    source = (ROOT / "config/competition_ui.yaml").read_text(encoding="utf-8")
    path = tmp_path / "competition.yaml"
    path.write_text(source.replace("host: 127.0.0.1", "host: 0.0.0.0"), encoding="utf-8")
    with pytest.raises(CompetitionUIConfigError, match="127.0.0.1"):
        load_competition_ui_config(path, project_root=ROOT)


def test_touch_ui_automatically_selects_enabled_competition_config(monkeypatch) -> None:
    captured = {}
    args = app.build_argument_parser().parse_args(["--touch-ui", "--no-serial"])
    monkeypatch.setattr(app, "load_mission_config", lambda _path: {"ball_uart": {"enabled": False}})
    monkeypatch.setattr(app, "load_camera_config", lambda _path: object())
    monkeypatch.setattr(
        app,
        "resolve_touch_ui_config",
        lambda _args: SimpleNamespace(startup_competition_mode=True),
    )
    monkeypatch.setattr(app, "configure_touch_logging", lambda: None)
    monkeypatch.setattr(app, "create_detector", lambda _path: object())
    monkeypatch.setattr(app, "CameraService", lambda _config: object())
    monkeypatch.setattr(app, "resolve_ball_uart_settings", lambda _args, _mission: {"port": "none", "baudrate": 9600})
    monkeypatch.setattr(app, "BallUartClient", lambda *_args, **_kwargs: object())

    def fake_run(*_args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(app, "run_application", fake_run)
    assert app.main(["--touch-ui", "--no-serial"]) == 0
    assert captured["competition_config"].enabled is True
    assert captured["competition_config"].host == "127.0.0.1"
    assert captured["initial_competition_mode"] is True


def test_vision_touch_service_template_is_byte_identical_to_head() -> None:
    current = (ROOT / "deploy/vision-touch.service.template").read_bytes()
    committed = subprocess.run(
        ["git", "show", "HEAD:deploy/vision-touch.service.template"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    assert current == committed
    assert b"--mode track --serial-port /dev/ttyAMA0 --baudrate 9600 --serial-rate 50 --touch-ui --headless" in current


def test_competition_http_bind_failure_does_not_terminate_runtime(monkeypatch) -> None:
    events = []

    class FakeRuntime:
        def __init__(self, **_kwargs):
            self._started = False
            self.competition_media_service = object()

        def start(self):
            self._started = True
            events.append("runtime-start")

        def run_forever(self):
            events.append("runtime-run")
            return 0

        def stop(self):
            self._started = False
            events.append("runtime-stop")

        def request_stop(self):
            pass

    class FailingServer:
        def __init__(self, *_args):
            pass

        def start(self):
            raise OSError("address unavailable")

        def stop(self):
            events.append("server-stop")

    monkeypatch.setattr(app, "VisionRuntime", FakeRuntime)
    monkeypatch.setattr(app, "CompetitionUIServer", FailingServer)
    monkeypatch.setattr(app.cv2, "destroyAllWindows", lambda: None)
    config = load_competition_ui_config(project_root=ROOT)
    result = app.run_application(
        SimpleNamespace(display=False, headless=True),
        {"smoothing_alpha": 0.5, "max_jump_px": 100, "confirm_frames": 1, "lost_frames": 1},
        object(),
        object(),
        object(),
        competition_config=config,
    )
    assert result == 0
    assert events[:2] == ["runtime-start", "runtime-run"]
