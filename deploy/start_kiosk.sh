#!/usr/bin/env bash
set -u

VISION_TOUCH_URL="${VISION_TOUCH_URL:-http://127.0.0.1:8765}"
TIMEOUT_SECONDS="${VISION_KIOSK_TIMEOUT:-60}"

url_address="${VISION_TOUCH_URL#http://}"
url_host="${url_address%%:*}"
url_port="${url_address##*:}"
if [[ "$VISION_TOUCH_URL" != http://* ]] \
  || [[ "$url_address" == "$url_host" ]] \
  || [[ "$url_host" != "127.0.0.1" && "$url_host" != "localhost" ]] \
  || [[ ! "$url_port" =~ ^[0-9]+$ ]] \
  || ((10#$url_port < 1 || 10#$url_port > 65535)); then
  echo "VISION_TOUCH_URL仅允许http://127.0.0.1:PORT或http://localhost:PORT" >&2
  exit 3
fi

if command -v chromium >/dev/null 2>&1; then
  BROWSER="$(command -v chromium)"
elif command -v chromium-browser >/dev/null 2>&1; then
  BROWSER="$(command -v chromium-browser)"
else
  echo "未找到Chromium；请安装chromium或chromium-browser" >&2
  exit 1
fi

deadline=$((SECONDS + TIMEOUT_SECONDS))
until curl --fail --silent --max-time 2 "$VISION_TOUCH_URL/healthz" >/dev/null; do
  if (( SECONDS >= deadline )); then
    echo "等待触摸服务超时：$VISION_TOUCH_URL/healthz" >&2
    exit 2
  fi
  sleep 1
done

echo "触摸服务已就绪，启动kiosk。调试退出：Alt+F4，或通过SSH停止桌面自启动。"
while true; do
  "$BROWSER" \
    --kiosk \
    --app="$VISION_TOUCH_URL" \
    --no-first-run \
    --no-default-browser-check \
    --disable-translate \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --overscroll-history-navigation=0 \
    --disable-pinch
  echo "Chromium已退出，2秒后重新启动。按Ctrl+C停止本脚本。" >&2
  sleep 2
done
