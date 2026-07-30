#!/usr/bin/env bash
set -euo pipefail

USER_NAME=""
PROJECT_DIR=""

usage() {
  echo "用法: sudo bash deploy/install_tablet_web.sh --user USER --project-dir DIR"
}

while (($#)); do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    --project-dir) PROJECT_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "当前平台不支持：安装脚本仅适用于 Raspberry Pi Linux" >&2
  exit 3
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "请使用 sudo 运行安装脚本" >&2
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
if [[ ! -f "$PROJECT_DIR/app.py" || ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "工程目录或虚拟环境无效: $PROJECT_DIR" >&2
  exit 6
fi
for command in nmcli nginx systemctl sed sha256sum; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "缺少部署命令: $command；请先安装 NetworkManager 和 nginx" >&2
    exit 7
  fi
done

main_service_template="$PROJECT_DIR/deploy/vision-touch.service.template"
if [[ ! -f "$main_service_template" ]]; then
  echo "视觉主服务模板不存在: $main_service_template" >&2
  exit 8
fi
main_service_hash_before="$(sha256sum "$main_service_template" | awk '{print $1}')"

# The tablet deployment deliberately removes the legacy desktop kiosk entry.
user_home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
rm -f -- "$user_home/.config/autostart/vision-touch-kiosk.desktop"

chmod +x \
  "$PROJECT_DIR/deploy/hotspot/install_hotspot.sh" \
  "$PROJECT_DIR/deploy/hotspot/uninstall_hotspot.sh" \
  "$PROJECT_DIR/deploy/hotspot/hotspot_healthcheck.sh"
"$PROJECT_DIR/deploy/hotspot/install_hotspot.sh"

escape_sed() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
escaped_user="$(escape_sed "$USER_NAME")"
escaped_project="$(escape_sed "$PROJECT_DIR")"

sed -e "s|@PROJECT_DIR@|$escaped_project|g" \
  "$PROJECT_DIR/deploy/systemd/cxb-hotspot.service" \
  > /etc/systemd/system/cxb-hotspot.service
sed -e "s|@USER@|$escaped_user|g" -e "s|@PROJECT_DIR@|$escaped_project|g" \
  "$PROJECT_DIR/deploy/systemd/camera-debug-web.service" \
  > /etc/systemd/system/camera-debug-web.service

nginx_available="/etc/nginx/sites-available/camera-tablet.conf"
nginx_enabled="/etc/nginx/sites-enabled/camera-tablet.conf"
sed -e "s|@PROJECT_DIR@|$escaped_project|g" \
  "$PROJECT_DIR/deploy/nginx/camera-tablet.conf.template" \
  > "$nginx_available"
ln -sfn "$nginx_available" "$nginx_enabled"

# Bind 192.168.50.1 only after NetworkManager has activated the project hotspot.
nginx_dropin_dir="/etc/systemd/system/nginx.service.d"
nginx_dropin="$nginx_dropin_dir/camera-tablet-hotspot.conf"
mkdir -p -- "$nginx_dropin_dir"
cat > "$nginx_dropin" <<'EOF'
[Unit]
After=cxb-hotspot.service
Wants=cxb-hotspot.service
EOF

if ! nginx -t; then
  echo "Nginx 配置检查失败，未启动网站" >&2
  exit 9
fi

main_service_hash_after="$(sha256sum "$main_service_template" | awk '{print $1}')"
if [[ "$main_service_hash_before" != "$main_service_hash_after" ]]; then
  echo "视觉主服务模板意外变化，停止安装" >&2
  exit 10
fi

systemctl daemon-reload
systemctl enable cxb-hotspot.service
systemctl enable camera-debug-web.service
systemctl enable nginx.service
systemctl restart cxb-hotspot.service
systemctl restart camera-debug-web.service
systemctl restart nginx.service

for service in cxb-hotspot.service camera-debug-web.service nginx.service; do
  if ! systemctl is-enabled --quiet "$service"; then
    echo "服务未启用: $service" >&2
    exit 11
  fi
done
"$PROJECT_DIR/deploy/hotspot/hotspot_healthcheck.sh"

echo "热点名称：cxb"
echo "热点密码：123@chenzi"
echo "比赛网站：http://192.168.50.1/"
echo "调试网站：http://192.168.50.1:8080/"
