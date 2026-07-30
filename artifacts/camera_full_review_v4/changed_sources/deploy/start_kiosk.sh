#!/usr/bin/env bash
set -u

VISION_TOUCH_URL="${VISION_TOUCH_URL:-http://127.0.0.1:8765}"
TIMEOUT_SECONDS="${VISION_KIOSK_TIMEOUT:-60}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$PROJECT_DIR/runtime"
PID_FILE="$RUNTIME_DIR/kiosk.pid"
EXIT_REQUESTED="$RUNTIME_DIR/kiosk.exit_requested"
CHROME_PROFILE="$RUNTIME_DIR/chrome-profile"
MAX_RESTARTS="${VISION_KIOSK_MAX_RESTARTS:-5}"
RESTART_DELAY="${VISION_KIOSK_RESTART_DELAY:-2}"

mkdir -p "$RUNTIME_DIR"
rm -f -- "$EXIT_REQUESTED"

url_address="${VISION_TOUCH_URL#http://}"
url_host=""
url_port=""
if [[ "$VISION_TOUCH_URL" == http://* && "$url_address" =~ ^(127\.0\.0\.1|localhost):([0-9]+)$ ]]; then
  url_host="${BASH_REMATCH[1]}"
  url_port="${BASH_REMATCH[2]}"
elif [[ "$VISION_TOUCH_URL" == http://* && "$url_address" =~ ^\[::1\]:([0-9]+)$ ]]; then
  url_host="::1"
  url_port="${BASH_REMATCH[1]}"
fi
if [[ -z "$url_host" ]] \
  || [[ ! "$url_port" =~ ^[0-9]+$ ]] \
  || ((10#$url_port < 1 || 10#$url_port > 65535)); then
  echo "VISION_TOUCH_URL仅允许本机回环HTTP地址和有效端口" >&2
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

browser_pid=""
if [[ ! "$MAX_RESTARTS" =~ ^[0-9]+$ ]] || [[ ! "$RESTART_DELAY" =~ ^[0-9]+$ ]]; then
  echo "VISION_KIOSK_MAX_RESTARTS和VISION_KIOSK_RESTART_DELAY必须为非负整数" >&2
  exit 5
fi

cleanup_pid() {
  if [[ -n "$browser_pid" && -f "$PID_FILE" ]]; then
    recorded_pid="$(tr -d '\r\n' < "$PID_FILE")"
    if [[ "$recorded_pid" =~ ^[0-9]+$ && "$recorded_pid" == "$browser_pid" ]]; then
      rm -f -- "$PID_FILE"
    fi
  fi
}
trap cleanup_pid EXIT

stop_session() {
  if [[ "$browser_pid" =~ ^[0-9]+$ ]] && kill -0 "$browser_pid" 2>/dev/null; then
    kill -TERM "$browser_pid" 2>/dev/null || true
    wait "$browser_pid" 2>/dev/null || true
  fi
  cleanup_pid
  exit 0
}
trap stop_session INT TERM

write_pid_atomically() {
  local pid="$1"
  local temporary
  temporary="$(mktemp "$RUNTIME_DIR/.kiosk.pid.XXXXXX")" || return 1
  printf '%s\n' "$pid" > "$temporary"
  chmod 0600 "$temporary"
  mv -f -- "$temporary" "$PID_FILE"
}

echo "触摸服务已就绪，启动kiosk。维护菜单可安全退出浏览器或视觉服务。"
restart_count=0
while ((restart_count <= MAX_RESTARTS)); do
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
  wait "$browser_pid" 2>/dev/null
  browser_status="$?"
  cleanup_pid
  browser_pid=""
  if [[ -f "$EXIT_REQUESTED" ]]; then
    rm -f -- "$EXIT_REQUESTED"
    echo "已按维护菜单请求退出kiosk；下次桌面登录或手动执行脚本时可重新打开。"
    exit 0
  fi
  if [[ "$browser_status" -eq 0 || "$browser_status" -eq 130 || "$browser_status" -eq 143 ]]; then
    echo "kiosk浏览器已正常关闭；视觉后端继续运行。"
    exit 0
  fi
  restart_count=$((restart_count + 1))
  if ((restart_count > MAX_RESTARTS)); then
    echo "kiosk浏览器连续异常退出，已达到最大重试次数${MAX_RESTARTS}，停止重启。" >&2
    exit "$browser_status"
  fi
  echo "浏览器异常退出（code=$browser_status），${RESTART_DELAY}秒后进行第${restart_count}次重试。" >&2
  sleep "$RESTART_DELAY"
done
