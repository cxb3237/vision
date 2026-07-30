from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from deploy.hotspot.configure import HotspotError, HotspotSettings, configure_hotspot


class FakeRunner:
    def __init__(self, existing: bool = False, fail_at: int | None = None) -> None:
        self.existing = existing
        self.fail_at = fail_at
        self.calls: list[list[str]] = []

    def __call__(self, arguments, **kwargs):
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        call = list(arguments)
        self.calls.append(call)
        index = len(self.calls)
        if self.fail_at == index:
            return subprocess.CompletedProcess(call, 1, "", "mock nmcli failure")
        stdout = "cxb-hotspot\n" if index == 1 and self.existing else "home-wifi\n"
        return subprocess.CompletedProcess(call, 0, stdout, "")


def interface_root(tmp_path: Path) -> Path:
    root = tmp_path / "sys/class/net"
    (root / "wlan0").mkdir(parents=True)
    return root


def flattened(calls: list[list[str]]) -> list[str]:
    return [item for call in calls for item in call]


def test_hotspot_uses_fixed_ssid_password_address_and_wlan0(tmp_path) -> None:
    runner = FakeRunner()
    created = configure_hotspot(run=runner, interface_root=interface_root(tmp_path))
    values = flattened(runner.calls)
    assert created is True
    assert "cxb" in values
    assert "123@chenzi" in values
    assert "192.168.50.1/24" in values
    assert "wlan0" in values
    assert "shared" in values and "wpa-psk" in values
    assert not any("delete" in call for call in runner.calls)


def test_repeated_hotspot_install_is_idempotent_and_does_not_add_again(tmp_path) -> None:
    runner = FakeRunner(existing=True)
    created = configure_hotspot(run=runner, interface_root=interface_root(tmp_path))
    assert created is False
    assert not any(call[:3] == ["nmcli", "connection", "add"] for call in runner.calls)
    assert any(call[:3] == ["nmcli", "connection", "modify"] for call in runner.calls)
    assert not any("home-wifi" in call for call in runner.calls)


def test_nmcli_failure_is_reported_as_nonzero_path(tmp_path) -> None:
    runner = FakeRunner(fail_at=2)
    with pytest.raises(HotspotError, match="mock nmcli failure"):
        configure_hotspot(run=runner, interface_root=interface_root(tmp_path))


def test_missing_wlan0_has_clear_error_and_runs_no_command(tmp_path) -> None:
    runner = FakeRunner()
    with pytest.raises(HotspotError, match="无线接口不存在: wlan0"):
        configure_hotspot(run=runner, interface_root=tmp_path / "missing")
    assert runner.calls == []


def test_networkmanager_profile_is_restricted_to_root_readable_mode(tmp_path) -> None:
    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    profile = profile_root / "cxb-hotspot.nmconnection"
    profile.write_text("mock", encoding="utf-8")
    runner = FakeRunner(existing=True)
    chmod_calls: list[tuple[Path, int]] = []
    configure_hotspot(
        HotspotSettings(),
        run=runner,
        chmod=lambda path, mode: chmod_calls.append((path, mode)),
        interface_root=interface_root(tmp_path),
        profile_root=profile_root,
    )
    assert chmod_calls == [(profile, 0o600)]
