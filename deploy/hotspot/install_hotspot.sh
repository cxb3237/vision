#!/usr/bin/env bash
set -euo pipefail

INTERFACE="${HOTSPOT_INTERFACE:-wlan0}"
SYS_CLASS_NET="${HOTSPOT_SYS_CLASS_NET:-/sys/class/net}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ACTIVATION_FLAG="--activate"

usage() {
  echo "用法: sudo deploy/hotspot/install_hotspot.sh [--activate|--no-activate]"
}

while (($#)); do
  case "$1" in
    --activate) ACTIVATION_FLAG="--activate"; shift ;;
    --no-activate) ACTIVATION_FLAG="--no-activate"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "当前平台不支持：热点安装仅适用于 Raspberry Pi Linux" >&2
  exit 3
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "请使用 sudo 安装热点" >&2
  exit 4
fi
if ! command -v nmcli >/dev/null 2>&1; then
  echo "未找到 nmcli；请安装并启用 NetworkManager" >&2
  exit 5
fi
if [[ ! -d "$SYS_CLASS_NET/$INTERFACE" ]]; then
  echo "无线接口不存在: $INTERFACE" >&2
  exit 6
fi
if ! systemctl is-active --quiet NetworkManager.service; then
  echo "NetworkManager.service 未运行" >&2
  exit 7
fi

python3 "$SCRIPT_DIR/configure.py" \
  --interface "$INTERFACE" \
  --interface-root "$SYS_CLASS_NET" \
  --profile-root /etc/NetworkManager/system-connections \
  "$ACTIVATION_FLAG"
