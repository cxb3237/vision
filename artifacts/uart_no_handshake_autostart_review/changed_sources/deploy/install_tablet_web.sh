#!/usr/bin/env bash
set -Eeuo pipefail

USER_NAME=""
PROJECT_DIR=""
CURRENT_STEP="参数检查"
ACTIVATION_ATTEMPTED=false
INSTALL_COMPLETE=false
STAGING_DIR=""

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
for command in nmcli nginx systemctl sed sha256sum mktemp; do
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

CURRENT_STEP="创建热点配置（不激活）"
chmod +x \
  "$PROJECT_DIR/deploy/hotspot/install_hotspot.sh" \
  "$PROJECT_DIR/deploy/hotspot/uninstall_hotspot.sh" \
  "$PROJECT_DIR/deploy/hotspot/hotspot_healthcheck.sh"
"$PROJECT_DIR/deploy/hotspot/install_hotspot.sh" --no-activate

STAGING_DIR="$(mktemp -d /tmp/camera-tablet-install.XXXXXX)"
BACKUP_DIR="$STAGING_DIR/backup"
mkdir -p -- "$BACKUP_DIR"

unit_hotspot="/etc/systemd/system/cxb-hotspot.service"
unit_debug="/etc/systemd/system/camera-debug-web.service"
nginx_available="/etc/nginx/sites-available/camera-tablet.conf"
nginx_enabled="/etc/nginx/sites-enabled/camera-tablet.conf"
nginx_dropin_dir="/etc/systemd/system/nginx.service.d"
nginx_dropin="$nginx_dropin_dir/camera-tablet-hotspot.conf"
managed_paths=("$unit_hotspot" "$unit_debug" "$nginx_available" "$nginx_enabled" "$nginx_dropin")
managed_services=(vision-touch.service cxb-hotspot.service camera-debug-web.service nginx.service)
declare -A service_was_enabled
declare -A service_was_active

for service in "${managed_services[@]}"; do
  service_was_enabled["$service"]="$(systemctl is-enabled "$service" 2>/dev/null || true)"
  service_was_active["$service"]="$(systemctl is-active "$service" 2>/dev/null || true)"
done

backup_managed_files() {
  local index=0 target
  for target in "${managed_paths[@]}"; do
    if [[ -e "$target" || -L "$target" ]]; then
      cp -a -- "$target" "$BACKUP_DIR/$index"
      printf 'present' > "$BACKUP_DIR/$index.state"
    else
      printf 'absent' > "$BACKUP_DIR/$index.state"
    fi
    index=$((index + 1))
  done
}

restore_managed_files() {
  local index=0 target state
  for target in "${managed_paths[@]}"; do
    state="$(<"$BACKUP_DIR/$index.state")"
    rm -f -- "$target"
    if [[ "$state" == "present" ]]; then
      mkdir -p -- "$(dirname -- "$target")"
      cp -a -- "$BACKUP_DIR/$index" "$target"
    fi
    index=$((index + 1))
  done
  systemctl daemon-reload >/dev/null 2>&1 || true
  for service in "${managed_services[@]}"; do
    if [[ "${service_was_enabled[$service]}" != "enabled" ]]; then
      systemctl disable "$service" >/dev/null 2>&1 || true
    fi
    if [[ "${service_was_active[$service]}" == "active" ]]; then
      systemctl restart "$service" >/dev/null 2>&1 || true
    else
      systemctl stop "$service" >/dev/null 2>&1 || true
    fi
  done
}

cleanup_staging() {
  if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then
    rm -rf -- "$STAGING_DIR"
  fi
}

on_install_error() {
  local code=$?
  trap - ERR
  echo "安装失败步骤：$CURRENT_STEP" >&2
  if [[ "$ACTIVATION_ATTEMPTED" == false ]]; then
    echo "热点尚未激活，当前 wlan0 网络保持不变。正在恢复本工程管理的系统文件。" >&2
    restore_managed_files
  else
    echo "热点激活阶段失败；已安装配置予以保留。可通过有线网络或本地终端执行：" >&2
    echo "sudo systemctl restart cxb-hotspot.service" >&2
  fi
  cleanup_staging
  exit "$code"
}

backup_managed_files
trap on_install_error ERR

escape_sed() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
escaped_user="$(escape_sed "$USER_NAME")"
escaped_project="$(escape_sed "$PROJECT_DIR")"

CURRENT_STEP="生成 systemd unit"
sed -e "s|@PROJECT_DIR@|$escaped_project|g" \
  "$PROJECT_DIR/deploy/systemd/cxb-hotspot.service" > "$unit_hotspot"
sed -e "s|@USER@|$escaped_user|g" -e "s|@PROJECT_DIR@|$escaped_project|g" \
  "$PROJECT_DIR/deploy/systemd/camera-debug-web.service" > "$unit_debug"

CURRENT_STEP="生成 Nginx 配置与启动顺序 drop-in"
sed -e "s|@PROJECT_DIR@|$escaped_project|g" \
  "$PROJECT_DIR/deploy/nginx/camera-tablet.conf.template" > "$nginx_available"
ln -sfn "$nginx_available" "$nginx_enabled"
mkdir -p -- "$nginx_dropin_dir"
cat > "$nginx_dropin" <<'EOF'
[Unit]
After=cxb-hotspot.service
Wants=cxb-hotspot.service
EOF

CURRENT_STEP="Nginx 配置校验"
nginx -t

CURRENT_STEP="systemd daemon-reload"
systemctl daemon-reload

CURRENT_STEP="enable 开机服务"
systemctl enable vision-touch.service
systemctl enable cxb-hotspot.service
systemctl enable camera-debug-web.service
systemctl enable nginx.service
for service in vision-touch.service cxb-hotspot.service camera-debug-web.service nginx.service; do
  systemctl is-enabled --quiet "$service"
done

CURRENT_STEP="启动内部调试网站服务"
systemctl restart camera-debug-web.service

CURRENT_STEP="验证视觉主服务模板未变化"
main_service_hash_after="$(sha256sum "$main_service_template" | awk '{print $1}')"
if [[ "$main_service_hash_before" != "$main_service_hash_after" ]]; then
  echo "视觉主服务模板意外变化" >&2
  false
fi

user_home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
rm -f -- "$user_home/.config/autostart/vision-touch-kiosk.desktop"

echo "所有前置配置已完成。注意：若当前通过 wlan0 SSH 安装，下一步激活热点会断开 SSH。"
CURRENT_STEP="最终激活 cxb 热点"
ACTIVATION_ATTEMPTED=true
if ! systemctl restart cxb-hotspot.service; then
  echo "热点激活失败；已安装的服务和配置已保留。" >&2
  echo "请通过有线网络或本地终端执行：sudo systemctl restart cxb-hotspot.service" >&2
  cleanup_staging
  exit 12
fi

CURRENT_STEP="热点激活后的 Nginx 启动"
systemctl restart nginx.service
"$PROJECT_DIR/deploy/hotspot/hotspot_healthcheck.sh"

INSTALL_COMPLETE=true
trap - ERR
cleanup_staging

echo "热点名称：cxb"
echo "热点密码：123@chenzi"
echo "热点地址：192.168.50.1/24"
echo "比赛网站：http://192.168.50.1/"
echo "调试网站：http://192.168.50.1:8080/"
