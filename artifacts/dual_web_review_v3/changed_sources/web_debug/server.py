"""Tablet-facing static server and strict proxy for the local vision backend.

This process does not import OpenCV, CameraService, NCNN, pyserial or any UART
driver.  It serves the migrated frontend and forwards a fixed allowlist of
HTTP requests to the existing loopback-only vision service.
"""

from __future__ import annotations

import argparse
from collections import deque
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import socket
import threading
import time
from typing import Any
from urllib.parse import unquote, urlsplit


LOG = logging.getLogger(__name__)
STATIC_ROOT = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BODY = 64 * 1024
MAX_PROXY_RESPONSE = 2 * 1024 * 1024
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/control_scheduler.js": (
        "control_scheduler.js",
        "application/javascript; charset=utf-8",
    ),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
GET_PROXY_PATHS = frozenset(
    {"/healthz", "/api/status", "/api/config/camera", "/api/preview.mjpg"}
)
PATCH_PROXY_PATHS = frozenset({"/api/config/camera"})
POST_PROXY_PATHS = frozenset(
    {
        "/api/runtime/save",
        "/api/runtime/restore-last-good",
        "/api/runtime/restore-baseline",
        "/api/runtime/stop",
        "/api/competition/enter",
        "/api/competition/exit",
    }
)


class DebugWebConfigError(ValueError):
    pass


def validate_bind_host(host: str) -> str:
    value = str(host).strip()
    if value != "127.0.0.1":
        raise DebugWebConfigError("调试代理只能监听 127.0.0.1")
    return value


def parse_backend_url(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DebugWebConfigError("视觉后端必须是无认证的本机 HTTP 地址")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DebugWebConfigError("视觉后端端口无效") from exc
    if port != 8765:
        raise DebugWebConfigError("视觉后端仅允许本机端口 8765")
    return parsed.hostname, port


class EventLog:
    """Bounded in-memory operational events; never reads arbitrary log files."""

    def __init__(self, maximum: int = 50) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=maximum)
        self._lock = threading.Lock()

    def add(self, level: str, message: str) -> None:
        item = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": str(message)[:240],
        }
        with self._lock:
            self._items.append(item)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)


class DebugWebServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8081,
        backend_url: str = "http://127.0.0.1:8765",
        static_root: Path | None = None,
    ) -> None:
        self.host = validate_bind_host(host)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise DebugWebConfigError("调试代理端口必须在 1..65535")
        self.port = port
        self.backend_host, self.backend_port = parse_backend_url(backend_url)
        self.static_root = (static_root or STATIC_ROOT).resolve()
        self.events = EventLog()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="camera-debug-web",
            daemon=True,
        )
        self._thread.start()
        self.events.add("INFO", f"调试代理已启动 {self.host}:{self.port}")

    def stop(self, timeout: float = 2.0) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout)
        self._server = None
        self._thread = None

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self.port
        return int(self._server.server_address[1])

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CameraDebugWeb/1"

            def log_message(self, format_string: str, *args: Any) -> None:
                LOG.debug("debug-web %s", format_string % args)

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _read_body(self) -> bytes:
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length, 10)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Content-Length 非法") from exc
                if length < 0:
                    raise ValueError("Content-Length 不能为负数")
                if length > MAX_REQUEST_BODY:
                    raise OverflowError("请求体超过 64 KiB")
                previous = self.connection.gettimeout()
                self.connection.settimeout(2.0)
                try:
                    body = self.rfile.read(length)
                except (TimeoutError, OSError) as exc:
                    raise ValueError("请求体读取超时") from exc
                finally:
                    self.connection.settimeout(previous)
                if len(body) != length:
                    raise ValueError("请求体长度不完整")
                return body

            def _serve_static(self, route: str) -> None:
                name, content_type = STATIC_FILES[route]
                path = (owner.static_root / name).resolve()
                try:
                    path.relative_to(owner.static_root)
                    data = path.read_bytes()
                except (ValueError, OSError):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                cache = "max-age=300" if name.endswith((".js", ".css")) else "no-cache"
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)

            def _backend_unavailable(self, message: str) -> None:
                owner.events.add("WARNING", "视觉主服务未连接")
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "ok": False,
                        "code": "BACKEND_UNAVAILABLE",
                        "message": "视觉主服务未连接",
                        "detail": str(message)[:160],
                    },
                )

            def _proxy(self, method: str, route: str, body: bytes = b"") -> None:
                timeout = 65.0 if route == "/api/preview.mjpg" else 3.0
                connection = HTTPConnection(owner.backend_host, owner.backend_port, timeout=timeout)
                headers = {"Accept": self.headers.get("Accept", "*/*")}
                headers_sent = False
                if body:
                    headers["Content-Type"] = "application/json"
                    headers["Content-Length"] = str(len(body))
                try:
                    connection.request(method, route, body=body or None, headers=headers)
                    response = connection.getresponse()
                    content_type = response.getheader("Content-Type")
                    if route == "/api/preview.mjpg":
                        self.send_response(response.status)
                        if content_type:
                            self.send_header("Content-Type", content_type)
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        headers_sent = True
                        while True:
                            chunk = response.read(16 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    else:
                        data = response.read(MAX_PROXY_RESPONSE + 1)
                        if len(data) > MAX_PROXY_RESPONSE:
                            raise HTTPException("视觉后端响应超过限制")
                        self.send_response(response.status)
                        if content_type:
                            self.send_header("Content-Type", content_type)
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        headers_sent = True
                        self.wfile.write(data)
                    owner.events.add("INFO", f"{method} {route} -> {response.status}")
                except (OSError, HTTPException, socket.timeout) as exc:
                    if not headers_sent and not self.wfile.closed:
                        try:
                            self._backend_unavailable(str(exc))
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                finally:
                    connection.close()

            def do_GET(self) -> None:
                route = unquote(urlsplit(self.path).path)
                if ".." in route:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "路径非法"})
                elif route in STATIC_FILES:
                    self._serve_static(route)
                elif route == "/debug/healthz":
                    self._json(HTTPStatus.OK, {"ok": True, "service": "camera-debug-web"})
                elif route == "/debug/events":
                    self._json(HTTPStatus.OK, {"ok": True, "events": owner.events.snapshot()})
                elif route in GET_PROXY_PATHS:
                    self._proxy("GET", route)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "接口不存在"})

            def _proxy_with_body(self, method: str, allowed: frozenset[str]) -> None:
                route = unquote(urlsplit(self.path).path)
                if ".." in route:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "路径非法"})
                    return
                if route not in allowed:
                    self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "接口不存在"})
                    return
                try:
                    body = self._read_body()
                    if body:
                        json.loads(body.decode("utf-8"))
                except OverflowError as exc:
                    self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "message": str(exc)})
                    return
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(exc)})
                    return
                self._proxy(method, route, body)

            def do_PATCH(self) -> None:
                self._proxy_with_body("PATCH", PATCH_PROXY_PATHS)

            def do_POST(self) -> None:
                self._proxy_with_body("POST", POST_PROXY_PATHS)

        return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="平板调试网站本机代理")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--backend", default="http://127.0.0.1:8765")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        server = DebugWebServer(args.host, args.port, args.backend)
    except DebugWebConfigError as exc:
        LOG.error("调试网站配置错误: %s", exc)
        return 2
    try:
        server.start()
        LOG.info("平板调试网站内部地址: http://%s:%d", args.host, args.port)
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0
    finally:
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
