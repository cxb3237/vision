#!/usr/bin/env bash
set -u

VISION_TOUCH_URL="${VISION_TOUCH_URL:-http://127.0.0.1:8765}"
TIMEOUT_SECONDS="${VISION_KIOSK_TIMEOUT:-60}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$PROJECT_DIR/runtime"
PID_FILE="$RUNTIME_DIR/kiosk.pid"
EXIT_MARKER="$RUNTIME_DIR/kiosk.exit"
CHROME_PROFILE="$RUNTIME_DIR/chrome-profile"

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
elif command -v google-chrome >/dev/null 2>&1; then
  BROWSER="$(command -v google-chrome)"
elif command -v google-chrome-stable >/dev/null 2>&1; then
  BROWSER="$(command -v google-chrome-stable)"
elif command -v firefox >/dev/null 2>&1; then
  BROWSER="$(command -v firefox)"
else
  echo "未找到Chromium、Chrome或Firefox" >&2
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

mkdir -p "$RUNTIME_DIR"
rm -f -- "$EXIT_MARKER"
browser_pid=""

cleanup_pid() {
  if [[ -n "$browser_pid" && -f "$PID_FILE" ]]; then
    recorded_pid="$(tr -d '\r\n' < "$PID_FILE")"
    if [[ "$recorded_pid" =~ ^[0-9]+$ && "$recorded_pid" == "$browser_pid" ]]; then
      rm -f -- "$PID_FILE"
    fi
  fi
}
trap cleanup_pid EXIT
trap 'exit 0' INT TERM

write_pid_atomically() {
  local pid="$1"
  local temporary
  temporary="$(mktemp "$RUNTIME_DIR/.kiosk.pid.XXXXXX")" || return 1
  printf '%s\n' "$pid" > "$temporary"
  chmod 0600 "$temporary"
  mv -f -- "$temporary" "$PID_FILE"
}

echo "触摸服务已就绪，启动kiosk。维护菜单可安全退出浏览器或视觉服务。"
while true; do
  browser_name="$(basename -- "$BROWSER")"
  if [[ "$browser_name" == "firefox" ]]; then
    "$BROWSER" --kiosk "$VISION_TOUCH_URL" &
  else
    mkdir -p "$CHROME_PROFILE"
    "$BROWSER" \
      --kiosk \
      --app="$VISION_TOUCH_URL" \
      --user-data-dir="$CHROME_PROFILE" \
      --no-first-run \
      --no-default-browser-check \
      --disable-session-crashed-bubble \
      --disable-features=Translate \
      --overscroll-history-navigation=0 \
      --disable-pinch &
  fi
  browser_pid="$!"
  if ! write_pid_atomically "$browser_pid"; then
    kill -TERM "$browser_pid" 2>/dev/null || true
    wait "$browser_pid" 2>/dev/null || true
    echo "无法原子写入kiosk PID文件" >&2
    exit 4
  fi
  wait "$browser_pid" 2>/dev/null || true
  cleanup_pid
  browser_pid=""
  if [[ -f "$EXIT_MARKER" ]]; then
    rm -f -- "$EXIT_MARKER"
    echo "已按维护菜单请求退出kiosk；下次桌面登录或手动执行脚本时可重新打开。"
    exit 0
  fi
  echo "浏览器意外退出，2秒后重新启动。" >&2
  sleep 2
done
