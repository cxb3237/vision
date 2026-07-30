"""Idempotently configure the fixed CXB hotspot through nmcli.

Hardware and subprocess access occurs only when ``configure_hotspot`` or
``main`` is called, so Windows unit tests can import this module safely.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Callable, Sequence


@dataclass(frozen=True, slots=True)
class HotspotSettings:
    connection_name: str = "cxb-hotspot"
    ssid: str = "cxb"
    password: str = "123@chenzi"
    interface: str = "wlan0"
    address: str = "192.168.50.1/24"


class HotspotError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]
Chmod = Callable[[Path, int], None]


def _checked(run: Runner, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    result = run(
        list(arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "nmcli failed").strip()
        raise HotspotError(f"nmcli 失败: {' '.join(arguments[:4])}: {detail}")
    return result


def configure_hotspot(
    settings: HotspotSettings = HotspotSettings(),
    *,
    run: Runner = subprocess.run,
    chmod: Chmod = os.chmod,
    interface_root: Path = Path("/sys/class/net"),
    profile_root: Path = Path("/etc/NetworkManager/system-connections"),
) -> bool:
    """Create or update only ``cxb-hotspot``; return True when newly created."""

    if not (interface_root / settings.interface).is_dir():
        raise HotspotError(f"无线接口不存在: {settings.interface}")
    shown = _checked(run, ["nmcli", "-t", "-f", "NAME", "connection", "show"])
    names = {line.strip() for line in shown.stdout.splitlines() if line.strip()}
    created = settings.connection_name not in names
    if created:
        _checked(
            run,
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                settings.interface,
                "con-name",
                settings.connection_name,
                "ssid",
                settings.ssid,
            ],
        )
    _checked(
        run,
        [
            "nmcli",
            "connection",
            "modify",
            settings.connection_name,
            "connection.interface-name",
            settings.interface,
            "connection.autoconnect",
            "yes",
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.ssid",
            settings.ssid,
            "802-11-wireless.band",
            "bg",
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            settings.password,
            "ipv4.method",
            "shared",
            "ipv4.addresses",
            settings.address,
            "ipv6.method",
            "disabled",
        ],
    )
    profile = profile_root / f"{settings.connection_name}.nmconnection"
    if profile.is_file():
        chmod(profile, 0o600)
    _checked(
        run,
        [
            "nmcli",
            "connection",
            "up",
            settings.connection_name,
            "ifname",
            settings.interface,
        ],
    )
    return created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="配置 CXB NetworkManager 热点")
    parser.add_argument("--interface", default="wlan0")
    parser.add_argument("--interface-root", type=Path, default=Path("/sys/class/net"))
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=Path("/etc/NetworkManager/system-connections"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = HotspotSettings(interface=args.interface)
    try:
        created = configure_hotspot(
            settings,
            interface_root=args.interface_root,
            profile_root=args.profile_root,
        )
    except HotspotError as exc:
        print(exc)
        return 1
    action = "创建" if created else "更新"
    print(f"热点已{action}: SSID={settings.ssid} interface={settings.interface} address={settings.address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
