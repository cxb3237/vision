#!/usr/bin/env bash
set -euo pipefail

CONNECTION_NAME="cxb-hotspot"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "当前平台不支持：热点卸载仅适用于 Linux" >&2
  exit 3
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "请使用 sudo 卸载热点" >&2
  exit 4
fi
if ! command -v nmcli >/dev/null 2>&1; then
  echo "未找到 nmcli，无法检查热点连接" >&2
  exit 5
fi

if nmcli -t -f NAME connection show | grep -Fxq "$CONNECTION_NAME"; then
  nmcli connection down "$CONNECTION_NAME" >/dev/null 2>&1 || true
  nmcli connection delete "$CONNECTION_NAME"
  echo "已删除热点连接: $CONNECTION_NAME"
else
  echo "热点连接不存在，无需删除: $CONNECTION_NAME"
fi

