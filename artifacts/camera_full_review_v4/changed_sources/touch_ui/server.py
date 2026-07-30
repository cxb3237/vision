"""基于标准库的本地触摸Web服务；导入和构造均不访问硬件。"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from touch_ui.api import TouchAPI, api_error
from touch_ui.kiosk import KioskExitError, exit_kiosk
from touch_ui.models import CommandType, TouchUIConfig, validate_loopback_host


LOG = logging.getLogger(__name__)
MAX_REQUEST_BODY_BYTES = 64 * 1024


class InvalidContentLength(ValueError):
    pass


class RequestEntityTooLarge(ValueError):
    pass


class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class TouchUIServer:
    def __init__(self, runtime: Any, config: TouchUIConfig, web_root: Path | None = None) -> None:
        validate_loopback_host(config.host)
        self.runtime = runtime
        self.config = config
        self.api = TouchAPI(runtime)
        self.web_root = (web_root or Path(__file__).resolve().parents[1] / "touch_ui_web").resolve()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_request_lock = threading.Lock()
        self._stop_requested = False

    def _claim_runtime_stop(self) -> bool:
        with self._stop_request_lock:
            if self._stop_requested:
                return False
            self._stop_requested = True
            return True

    def _trigger_runtime_stop(self) -> None:
        def request_after_response() -> None:
            time.sleep(0.01)
            LOG.warning("维护菜单已确认停止视觉程序")
            self.runtime.request_stop()

        threading.Thread(
            target=request_after_response,
            name="touch-ui-stop-request",
            daemon=True,
        ).start()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        handler = self._build_handler()
        server_type = IPv6ThreadingHTTPServer if self.config.host == "::1" else ThreadingHTTPServer
        self._server = server_type((self.config.host, self.config.port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._serve,
            name="touch-ui-http",
            daemon=True,
        )
        self._thread.start()
        LOG.info("触摸界面已启动: http://%s:%d", self.config.host, self.config.port)

    def _serve(self) -> None:
        assert self._server is not None
        try:
            self._server.serve_forever(poll_interval=0.2)
        except Exception:
            LOG.exception("触摸Web服务异常；视觉主循环继续运行")

    def stop(self, timeout: float = 2.0) -> None:
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._server = None
        self._thread = None
        LOG.info("触摸界面已停止")

    def _build_handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "VisionTouch/1"

            def log_message(self, format_string: str, *args: Any) -> None:
                LOG.debug("web %s", format_string % args)

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> Any:
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length, 10)
                except (TypeError, ValueError) as exc:
                    raise InvalidContentLength("Content-Length 非法") from exc
                if length < 0:
                    raise InvalidContentLength("Content-Length 不能为负数")
                if length > MAX_REQUEST_BODY_BYTES:
                    raise RequestEntityTooLarge("请求体超过 64 KiB")
                previous_timeout = self.connection.gettimeout()
                self.connection.settimeout(2.0)
                try:
                    raw = self.rfile.read(length)
                except (TimeoutError, OSError) as exc:
                    raise InvalidContentLength("请求体读取超时或不完整") from exc
                finally:
                    self.connection.settimeout(previous_timeout)
                if len(raw) != length:
                    raise InvalidContentLength("请求体长度与 Content-Length 不一致")
                return json.loads(raw.decode("utf-8")) if raw else {}

            def _static(self, name: str, content_type: str) -> None:
                path = owner.web_root / name
                try:
                    data = path.read_bytes()
                except OSError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                try:
                    if path == "/healthz":
                        self._json(*owner.api.health())
                    elif path == "/api/status":
                        self._json(*owner.api.status())
                    elif path == "/api/config/camera":
                        self._json(*owner.api.camera_config())
                    elif path == "/api/preview.mjpg":
                        self._mjpeg()
                    elif path in {"/", "/index.html"}:
                        self._static("index.html", "text/html; charset=utf-8")
                    elif path == "/app.js":
                        self._static("app.js", "application/javascript; charset=utf-8")
                    elif path == "/control_scheduler.js":
                        self._static("control_scheduler.js", "application/javascript; charset=utf-8")
                    elif path == "/style.css":
                        self._static("style.css", "text/css; charset=utf-8")
                    else:
                        self._json(*api_error(404, "NOT_FOUND", "接口不存在"))
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:
                    LOG.exception("GET %s失败", path)
                    self._json(*api_error(500, "INTERNAL_ERROR", str(exc)))

            def _mjpeg(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                frame_id = -1
                while True:
                    new_id, jpeg = owner.runtime.frame_stream.wait_for_jpeg(frame_id, 1.0)
                    if jpeg is None:
                        jpeg = owner.runtime.get_latest_preview_jpeg()
                    else:
                        frame_id = new_id
                    if not jpeg:
                        continue
                    header = (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode("ascii")
                        + b"\r\n\r\n"
                    )
                    self.wfile.write(header + jpeg + b"\r\n")
                    self.wfile.flush()

            def do_PATCH(self) -> None:
                path = urlsplit(self.path).path
                try:
                    if path == "/api/config/camera":
                        self._json(*owner.api.patch_camera(self._body()))
                    else:
                        self._json(*api_error(404, "NOT_FOUND", "接口不存在"))
                except RequestEntityTooLarge as exc:
                    self._json(*api_error(413, "PAYLOAD_TOO_LARGE", str(exc)))
                except (ValueError, json.JSONDecodeError) as exc:
                    self._json(*api_error(400, "INVALID_JSON", str(exc)))

            def do_POST(self) -> None:
                path = urlsplit(self.path).path
                routes = {
                    "/api/runtime/save": CommandType.SAVE_RUNTIME,
                    "/api/runtime/restore-last-good": CommandType.RESTORE_LAST_GOOD,
                    "/api/runtime/restore-baseline": CommandType.RESTORE_BASELINE,
                    "/api/competition/enter": CommandType.ENTER_COMPETITION,
                    "/api/competition/exit": CommandType.EXIT_COMPETITION,
                }
                try:
                    if path == "/api/kiosk/exit":
                        body = self._body()
                        if body:
                            self._json(*api_error(400, "INVALID_BODY", "退出kiosk不接受PID或命令参数"))
                            return
                        try:
                            pid = exit_kiosk(owner.config.runtime_directory / "kiosk.pid")
                        except KioskExitError as exc:
                            LOG.warning("浏览器PID验证失败: %s", exc)
                            self._json(*api_error(409, "KIOSK_EXIT_REJECTED", str(exc)))
                        else:
                            self._json(200, {"ok": True, "status": "EXITING", "pid": pid})
                    elif path == "/api/runtime/stop":
                        body = self._body()
                        if body:
                            self._json(*api_error(400, "INVALID_BODY", "停止视觉程序不接受命令参数"))
                            return
                        scheduled = owner._claim_runtime_stop()
                        try:
                            self._json(
                                202,
                                {
                                    "ok": True,
                                    "status": "STOPPING" if scheduled else "ALREADY_STOPPING",
                                },
                            )
                        finally:
                            if scheduled:
                                owner._trigger_runtime_stop()
                    elif path in routes:
                        self._body()
                        self._json(*owner.api.command(routes[path]))
                    else:
                        self._json(*api_error(404, "NOT_FOUND", "接口不存在"))
                except RequestEntityTooLarge as exc:
                    self._json(*api_error(413, "PAYLOAD_TOO_LARGE", str(exc)))
                except (ValueError, json.JSONDecodeError) as exc:
                    self._json(*api_error(400, "INVALID_JSON", str(exc)))

        return Handler
