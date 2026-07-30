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

for module in serial cv2 yaml; do
  if ! "$PROJECT_DIR/.venv/bin/python" -c "import ${module}"; then
    echo "树莓派运行依赖缺失: Python模块 ${module}；请先安装 requirements-rpi-ncnn.txt" >&2
    exit 9
  fi
done

if [[ ! -e /dev/ttyAMA0 ]]; then
  echo "提示：当前未发现/dev/ttyAMA0；服务仍会安装并由串口重连机制等待设备。" >&2
fi

escape_sed() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
escaped_user="$(escape_sed "$USER_NAME")"
escaped_project="$(escape_sed "$PROJECT_DIR")"
sed -e "s|@USER@|$escaped_user|g" -e "s|@PROJECT_DIR@|$escaped_project|g" \
  "$PROJECT_DIR/deploy/vision-touch.service.template" \
  > /etc/systemd/system/vision-touch.service

user_home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
# 升级时主动删除旧版本本机浏览器自启动残留。
rm -f -- "$user_home/.config/autostart/vision-touch-kiosk.desktop"
usermod -aG video,dialout "$USER_NAME"
systemctl daemon-reload
systemctl enable vision-touch.service
if [[ "$START_NOW" -eq 1 ]]; then
  systemctl restart vision-touch.service
fi
echo "视觉主服务安装完成。组权限需重新登录后生效；本机浏览器自启动已移除。"
