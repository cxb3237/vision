#!/usr/bin/env bash
set -euo pipefail

USER_NAME=""
PROJECT_DIR=""
START_NOW=0

usage() {
  echo "用法: sudo bash deploy/install_touch_ui.sh --user USER --project-dir DIR [--start]"
}

while (($#)); do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    --project-dir) PROJECT_DIR="${2:-}"; shift 2 ;;
    --start) START_NOW=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "当前平台不支持：该安装脚本只能在Raspberry Pi Linux执行" >&2
  exit 3
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "请使用sudo运行安装脚本" >&2
  exit 4
fi
if [[ -z "$USER_NAME" || -z "$PROJECT_DIR" ]]; then
  usage
  exit 2
fi
if ! id "$USER_NAME" >/dev/null 2>&1; then
  echo "用户不存在: $USER_NAME" >&2
  exit 5
fi
PROJECT_DIR="$(realpath "$PROJECT_DIR")"
if [[ ! -f "$PROJECT_DIR/app.py" ]]; then
  echo "项目目录缺少app.py: $PROJECT_DIR" >&2
  exit 6
fi
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "项目虚拟环境不存在: $PROJECT_DIR/.venv/bin/python" >&2
  exit 7
fi

touch_settings="$({
  cd "$PROJECT_DIR"
  "$PROJECT_DIR/.venv/bin/python" - "$PROJECT_DIR" <<'PY'
from pathlib import Path
import sys
import yaml

from touch_ui.models import load_touch_ui_config

project = Path(sys.argv[1])
config = load_touch_ui_config(
    project / "config/touch_ui.yaml",
    project_root=project,
    create_runtime=False,
)
if config.host not in {"localhost", "127.0.0.1"}:
    raise SystemExit("kiosk配置只允许server.host为localhost或127.0.0.1")
detector = config.startup_detector
if config.restore_runtime_overrides:
    try:
        state = yaml.safe_load(config.ui_state_file.read_text(encoding="utf-8"))
        if state is None:
            state = {}
        if not isinstance(state, dict):
            raise ValueError("触摸UI运行状态必须为映射")
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"忽略无效触摸UI运行状态: {exc}", file=sys.stderr)
    else:
        restored = state.get("detector")
        if restored in {"color", "shape", "steel_ball", "digit"}:
            detector = restored
print(config.host)
print(config.port)
print(detector)
PY
})" || exit $?
mapfile -t touch_values <<< "$touch_settings"
if [[ "${#touch_values[@]}" -ne 3 ]]; then
  echo "无法解析config/touch_ui.yaml中的host、port和启动检测器" >&2
  exit 8
fi
touch_host="${touch_values[0]}"
touch_port="${touch_values[1]}"
startup_detector="${touch_values[2]}"
touch_url="http://${touch_host}:${touch_port}"

(
  cd "$PROJECT_DIR"
  "$PROJECT_DIR/.venv/bin/python" -m tools.check_digit_templates \
    --detector "$startup_detector"
)

if [[ ! -e /dev/ttyAMA0 ]]; then
  echo "提示：当前未发现/dev/ttyAMA0；服务仍会安装并由串口重连机制等待设备。" >&2
fi

escape_sed() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
escaped_user="$(escape_sed "$USER_NAME")"
escaped_project="$(escape_sed "$PROJECT_DIR")"
escaped_touch_url="$(escape_sed "$touch_url")"
sed -e "s|@USER@|$escaped_user|g" -e "s|@PROJECT_DIR@|$escaped_project|g" \
  "$PROJECT_DIR/deploy/vision-touch.service.template" \
  > /etc/systemd/system/vision-touch.service

user_home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
autostart_dir="$user_home/.config/autostart"
install -d -m 0755 -o "$USER_NAME" -g "$USER_NAME" "$autostart_dir"
sed -e "s|@PROJECT_DIR@|$escaped_project|g" \
  -e "s|@TOUCH_URL@|$escaped_touch_url|g" \
  "$PROJECT_DIR/deploy/vision-touch-kiosk.desktop.template" \
  > "$autostart_dir/vision-touch-kiosk.desktop"
chown "$USER_NAME:$USER_NAME" "$autostart_dir/vision-touch-kiosk.desktop"
chmod +x "$PROJECT_DIR/deploy/start_kiosk.sh"
usermod -aG video,dialout "$USER_NAME"
systemctl daemon-reload
systemctl enable vision-touch.service
if [[ "$START_NOW" -eq 1 ]]; then
  systemctl restart vision-touch.service
fi
echo "安装完成。组权限需重新登录后生效；桌面kiosk需要为该用户启用图形桌面自动登录。"
