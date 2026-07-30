from __future__ import annotations

from types import SimpleNamespace

from tools import camera_profile_check


def test_device_from_yaml_is_not_overridden_by_none(monkeypatch) -> None:
    captured = {}

    def load(_path, overrides=None):
        captured["overrides"] = overrides
        return SimpleNamespace(device=0, v4l2_controls={"enabled": False})

    monkeypatch.setattr(camera_profile_check, "load_camera_config", load)
    assert camera_profile_check.main([]) == 0
    assert captured["overrides"] is None


def test_explicit_device_overrides_yaml(monkeypatch) -> None:
    captured = {}

    def load(_path, overrides=None):
        captured["overrides"] = overrides
        return SimpleNamespace(device=overrides["device"], v4l2_controls={"enabled": False})

    monkeypatch.setattr(camera_profile_check, "load_camera_config", load)
    assert camera_profile_check.main(["--device", "/dev/video2"]) == 0
    assert captured["overrides"] == {"device": "/dev/video2"}


def test_invalid_device_returns_nonzero() -> None:
    assert camera_profile_check.main(["--device", "not-a-video-device"]) == 2
