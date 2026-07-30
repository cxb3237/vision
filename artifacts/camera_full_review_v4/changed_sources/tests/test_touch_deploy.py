"""部署文件只做静态检查，不在Windows执行systemd或Chromium。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_systemd_template_uses_placeholders_and_safe_stop() -> None:
    text = (ROOT / "deploy/vision-touch.service.template").read_text(encoding="utf-8")
    assert "User=@USER@" in text
    assert "WorkingDirectory=@PROJECT_DIR@" in text
    assert "@PROJECT_DIR@/.venv/bin/python" in text
    assert "KillSignal=SIGINT" in text and "RestartSec=" in text
    assert "Restart=on-failure" in text and "Restart=always" not in text
    assert "--detector digit" not in text
    assert "C:\\" not in text and "/home/" not in text


def test_install_script_validates_platform_and_does_not_enable_network_or_ssh() -> None:
    text = (ROOT / "deploy/install_touch_ui.sh").read_text(encoding="utf-8")
    assert "uname -s" in text and "systemctl daemon-reload" in text
    assert "video,dialout" in text and ".venv/bin/python" in text
    assert "ssh" not in text.lower() and "nmcli" not in text.lower()


def test_kiosk_is_local_only_and_waits_for_health() -> None:
    text = (ROOT / "deploy/start_kiosk.sh").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8765" in text
    assert 'VISION_TOUCH_URL="${VISION_TOUCH_URL:-' in text
    assert "localhost" in text and "::1" in text and "eval" not in text
    assert "/healthz" in text and "--kiosk" in text
    assert "chromium-browser" in text and "https://" not in text
    assert "google-chrome" in text and "google-chrome-stable" in text
    assert "firefox" in text
    assert "kiosk.pid" in text and "mktemp" in text and "mv -f" in text
    assert "kiosk.exit_requested" in text
    assert "while true" not in text
    assert "MAX_RESTARTS" in text and "browser_status" in text
    assert '--user-data-dir="$CHROME_PROFILE"' in text
    assert 'CHROME_PROFILE="$RUNTIME_DIR/chrome-profile"' in text
    assert "--no-sandbox" not in text


def test_installer_passes_validated_touch_url_as_environment_variable() -> None:
    install = (ROOT / "deploy/install_touch_ui.sh").read_text(encoding="utf-8")
    desktop = (ROOT / "deploy/vision-touch-kiosk.desktop.template").read_text(
        encoding="utf-8"
    )
    assert "load_touch_ui_config" in install
    assert "tools.check_digit_templates" not in install
    assert "@TOUCH_URL@" in desktop and "VISION_TOUCH_URL=" in desktop
    assert "eval" not in install


def test_raspberry_pi_dependencies_include_serial_and_are_checked() -> None:
    requirements = (ROOT / "requirements-rpi-ncnn.txt").read_text(encoding="utf-8")
    install = (ROOT / "deploy/install_touch_ui.sh").read_text(encoding="utf-8")
    assert any(line.strip() == "pyserial" for line in requirements.splitlines())
    for module in ("serial", "cv2", "yaml"):
        assert f"import {module}" in install or "import ${module}" in install
