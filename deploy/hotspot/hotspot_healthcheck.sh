#!/usr/bin/env bash
set -euo pipefail

CONNECTION_NAME="cxb-hotspot"
INTERFACE="${HOTSPOT_INTERFACE:-wlan0}"
EXPECTED_ADDRESS="192.168.50.1/24"
SYS_CLASS_NET="${HOTSPOT_SYS_CLASS_NET:-/sys/class/net}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "当前平台不支持热点健康检查" >&2
  exit 3
fi
if ! command -v nmcli >/dev/null 2>&1 || ! command -v ip >/dev/null 2>&1; then
  echo "热点健康检查需要 nmcli 和 ip" >&2
  exit 5
fi
if [[ ! -d "$SYS_CLASS_NET/$INTERFACE" ]]; then
  echo "无线接口不存在: $INTERFACE" >&2
  exit 6
fi
if ! nmcli -t -f NAME connection show --active | grep -Fxq "$CONNECTION_NAME"; then
  echo "热点连接未激活: $CONNECTION_NAME" >&2
  exit 7
fi
if ! ip -4 -o address show dev "$INTERFACE" | grep -Fq "$EXPECTED_ADDRESS"; then
  echo "热点地址不正确，期望 $EXPECTED_ADDRESS" >&2
  exit 8
fi
echo "热点健康: $CONNECTION_NAME $INTERFACE $EXPECTED_ADDRESS"

