#!/usr/bin/env bash
set -euo pipefail

USER_NAME=""
while (($#)); do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    -h|--help) echo "用法: sudo bash deploy/uninstall_touch_ui.sh --user USER"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "当前平台不支持：该卸载脚本只能在Linux执行" >&2
  exit 3
fi
if [[ "$EUID" -ne 0 || -z "$USER_NAME" ]]; then
  echo "请使用sudo并提供--user" >&2
  exit 4
fi
systemctl disable --now vision-touch.service 2>/dev/null || true
rm -f /etc/systemd/system/vision-touch.service
user_home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
rm -f "$user_home/.config/autostart/vision-touch-kiosk.desktop"
systemctl daemon-reload
echo "vision-touch服务和kiosk自启动已移除；项目与runtime现场配置未删除。"
