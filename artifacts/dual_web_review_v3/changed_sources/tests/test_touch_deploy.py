"""部署文件静态检查；Windows 不执行 systemd、NetworkManager 或 Nginx。"""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_SERVICE = ROOT / "deploy/vision-touch.service.template"
MAIN_SERVICE_SHA256 = "0f6e077e0dff49f52b6502c4a289d11b5893a383367fc6f94b1dd56fa7ed1557"


def test_visual_main_service_content_and_hash_are_invariant() -> None:
    data = MAIN_SERVICE.read_bytes()
    text = data.decode("utf-8")
    assert hashlib.sha256(data).hexdigest() == MAIN_SERVICE_SHA256
    assert "User=@USER@" in text
    assert "SupplementaryGroups=video dialout" in text
    assert "WorkingDirectory=@PROJECT_DIR@" in text
    assert (
        "ExecStart=@PROJECT_DIR@/.venv/bin/python app.py --mode track "
        "--serial-port /dev/ttyAMA0 --baudrate 9600 --serial-rate 50 "
        "--touch-ui --headless"
    ) in text
    assert "Restart=on-failure" in text
    assert "TimeoutStopSec=15" in text


def test_app_startup_entrypoint_is_unchanged_by_hotspot_deployment() -> None:
    data = (ROOT / "app.py").read_bytes()
    assert b"hotspot" not in data.lower()
    assert b"cxb-hotspot" not in data.lower()
    assert b"192.168.50.1" not in data


def test_original_visual_installer_keeps_service_generation_but_removes_browser_autostart() -> None:
    text = (ROOT / "deploy/install_touch_ui.sh").read_text(encoding="utf-8")
    assert "vision-touch.service.template" in text
    assert "systemctl enable vision-touch.service" in text
    assert "vision-touch-kiosk.desktop" in text
    assert "rm -f --" in text
    assert "start_kiosk.sh" not in text
    assert "chromium" not in text.lower()


def test_local_browser_launcher_and_desktop_template_are_removed() -> None:
    assert not (ROOT / "deploy/start_kiosk.sh").exists()
    assert not (ROOT / "deploy/vision-touch-kiosk.desktop.template").exists()
    assert not (ROOT / "touch_ui/kiosk.py").exists()


def test_tablet_installer_never_modifies_or_restarts_visual_main_service() -> None:
    install = (ROOT / "deploy/install_tablet_web.sh").read_text(encoding="utf-8")
    uninstall = (ROOT / "deploy/uninstall_tablet_web.sh").read_text(encoding="utf-8")
    assert "main_service_hash_before" in install and "main_service_hash_after" in install
    # The template name is deliberately read for the before/after hash guard.  The
    # tablet installer must never install, enable, restart, or remove the unit.
    assert "/etc/systemd/system/vision-touch.service" not in install
    assert "systemctl enable vision-touch.service" not in install
    assert "systemctl restart vision-touch.service" not in install
    assert "systemctl stop vision-touch.service" not in install
    assert "vision-touch.service" not in uninstall
    assert "systemctl restart cxb-hotspot.service" in install
    assert "systemctl restart camera-debug-web.service" in install
    assert "systemctl restart nginx.service" in install
    assert "systemctl enable cxb-hotspot.service" in install
    assert "systemctl enable camera-debug-web.service" in install
    assert "systemctl enable nginx.service" in install
    assert "systemctl is-enabled --quiet" in install


def test_new_services_are_independent_and_restart_on_failure() -> None:
    hotspot = (ROOT / "deploy/systemd/cxb-hotspot.service").read_text(encoding="utf-8")
    debug = (ROOT / "deploy/systemd/camera-debug-web.service").read_text(encoding="utf-8")
    assert "After=NetworkManager.service" in hotspot
    assert "Wants=NetworkManager.service" in hotspot
    assert "ExecStart=@PROJECT_DIR@/deploy/hotspot/install_hotspot.sh" in hotspot
    assert "ExecStart=@PROJECT_DIR@/.venv/bin/python -m web_debug.server" in debug
    assert "--host 127.0.0.1 --port 8081" in debug
    assert "Restart=on-failure" in debug
    assert "CameraService" not in debug and "ttyAMA" not in debug


def test_nginx_routes_fixed_hotspot_addresses_without_exposing_backend() -> None:
    nginx = (ROOT / "deploy/nginx/camera-tablet.conf.template").read_text(encoding="utf-8")
    assert "listen 192.168.50.1:80;" in nginx
    assert "listen 192.168.50.1:8080;" in nginx
    assert "proxy_pass http://127.0.0.1:8081;" in nginx
    assert "proxy_pass http://127.0.0.1:8000;" in nginx
    assert "location ^~ /recordings/" in nginx
    assert "proxy_force_ranges on" in nginx
    assert "127.0.0.1:8765" not in nginx
    assert "proxy_buffering off" in nginx
    assert "proxy_cache off" in nginx
    assert "client_max_body_size 64k" in nginx
    assert "autoindex off" in nginx
    assert "listen 0.0.0.0" not in nginx


def test_uninstaller_only_removes_new_services_and_legacy_autostart() -> None:
    text = (ROOT / "deploy/uninstall_tablet_web.sh").read_text(encoding="utf-8")
    assert "camera-debug-web.service" in text
    assert "cxb-hotspot.service" in text
    assert "camera-tablet.conf" in text
    assert "vision-touch-kiosk.desktop" in text
    assert "/etc/systemd/system/nginx.service.d/camera-tablet-hotspot.conf" in text
    for forbidden in ("models", "recordings", "app.py"):
        assert f"rm -f -- {forbidden}" not in text


def test_nginx_systemd_dropin_orders_binding_after_hotspot() -> None:
    install = (ROOT / "deploy/install_tablet_web.sh").read_text(encoding="utf-8")
    assert "camera-tablet-hotspot.conf" in install
    assert "After=cxb-hotspot.service" in install
    assert "Wants=cxb-hotspot.service" in install
    assert "systemctl daemon-reload" in install


def test_unified_install_configures_without_activation_and_switches_hotspot_last() -> None:
    install = (ROOT / "deploy/install_tablet_web.sh").read_text(encoding="utf-8")
    no_activate = install.index('"$PROJECT_DIR/deploy/hotspot/install_hotspot.sh" --no-activate')
    unit_install = install.index('CURRENT_STEP="生成 systemd unit"')
    nginx_check = install.index("nginx -t")
    daemon_reload = install.index("systemctl daemon-reload", nginx_check)
    enable_debug = install.index("systemctl enable camera-debug-web.service")
    start_debug = install.index("systemctl restart camera-debug-web.service")
    activate = install.index("systemctl restart cxb-hotspot.service", start_debug)
    assert no_activate < unit_install < nginx_check < daemon_reload < enable_debug < start_debug < activate
    assert "ACTIVATION_ATTEMPTED=false" in install
    assert "热点尚未激活，当前 wlan0 网络保持不变" in install
    assert "sudo systemctl restart cxb-hotspot.service" in install
    assert "若当前通过 wlan0 SSH 安装" in install


def test_hotspot_helper_has_explicit_activate_and_no_activate_modes() -> None:
    shell = (ROOT / "deploy/hotspot/install_hotspot.sh").read_text(encoding="utf-8")
    python = (ROOT / "deploy/hotspot/configure.py").read_text(encoding="utf-8")
    assert "--no-activate" in shell and "--activate" in shell
    assert 'if activate:' in python
    assert '"connection",\n                "up"' in python


def test_raspberry_pi_dependencies_still_include_serial() -> None:
    requirements = (ROOT / "requirements-rpi-ncnn.txt").read_text(encoding="utf-8")
    assert any(line.strip() == "pyserial" for line in requirements.splitlines())


def test_debug_proxy_does_not_open_hardware_or_load_detector() -> None:
    source = (ROOT / "web_debug/server.py").read_text(encoding="utf-8")
    for forbidden in (
        "cv2.VideoCapture(",
        "CameraService(",
        "serial.Serial(",
        "BallUartClient(",
        "SteelBallYoloNcnnDetector(",
        "/dev/ttyAMA0",
    ):
        assert forbidden not in source
