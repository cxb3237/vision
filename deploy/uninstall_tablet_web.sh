#!/usr/bin/env bash
set -euo pipefail

USER_NAME=""
PROJECT_DIR=""

while (($#)); do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    --project-dir) PROJECT_DIR="${2:-}"; shift 2 ;;
    -h|--help)
      echo "用法: sudo bash deploy/uninstall_tablet_web.sh --user USER --project-dir DIR"
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "当前平台不支持：卸载脚本仅适用于 Linux" >&2
  exit 3
fi
if [[ "$EUID" -ne 0 || -z "$USER_NAME" || -z "$PROJECT_DIR" ]]; then
  echo "请使用 sudo 并提供 --user 与 --project-dir" >&2
  exit 4
fi
PROJECT_DIR="$(realpath "$PROJECT_DIR")"

systemctl disable --now camera-debug-web.service 2>/dev/null || true
systemctl disable --now cxb-hotspot.service 2>/dev/null || true
rm -f -- /etc/systemd/system/camera-debug-web.service
rm -f -- /etc/systemd/system/cxb-hotspot.service

if [[ -x "$PROJECT_DIR/deploy/hotspot/uninstall_hotspot.sh" ]]; then
  "$PROJECT_DIR/deploy/hotspot/uninstall_hotspot.sh"
fi

rm -f -- /etc/nginx/sites-enabled/camera-tablet.conf
rm -f -- /etc/nginx/sites-available/camera-tablet.conf
rm -f -- /etc/systemd/system/nginx.service.d/camera-tablet-hotspot.conf
rmdir /etc/systemd/system/nginx.service.d 2>/dev/null || true

user_home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
rm -f -- "$user_home/.config/autostart/vision-touch-kiosk.desktop"
systemctl daemon-reload
if command -v nginx >/dev/null 2>&1 && nginx -t; then
  systemctl reload nginx.service 2>/dev/null || true
fi
echo "平板网站、热点和本工程 Nginx 配置已卸载；视觉主服务、工程、模型和录像未修改。"
