from __future__ import annotations

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading

import pytest

from web_debug.server import DebugWebConfigError, DebugWebServer, parse_backend_url


ROOT = Path(__file__).resolve().parents[1]


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _BackendHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, bytes]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def do_GET(self) -> None:
        type(self).requests.append(("GET", self.path, b""))
        if self.path == "/api/preview.mjpg":
            data = b"frame-data"
            content_type = "multipart/x-mixed-replace; boundary=frame"
        else:
            data = json.dumps({"ok": True, "camera_online": True}).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = self._body()
        type(self).requests.append(("POST", self.path, body))
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PATCH(self) -> None:
        body = self._body()
        type(self).requests.append(("PATCH", self.path, body))
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(port: int, method: str, path: str, body: bytes | None = None):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    connection.close()
    return result


def test_homepage_loads_while_backend_is_offline_then_proxy_recovers() -> None:
    debug = DebugWebServer(port=_free_port())
    backend: _ReusableHTTPServer | None = None
    backend_thread: threading.Thread | None = None
    _BackendHandler.requests.clear()
    try:
        debug.start()
        status, _, page = _request(debug.bound_port, "GET", "/")
        assert status == 200
        assert "钢球平板调试台" in page.decode("utf-8")

        status, _, payload = _request(debug.bound_port, "GET", "/api/status")
        assert status == 503
        assert json.loads(payload)["message"] == "视觉主服务未连接"

        try:
            backend = _ReusableHTTPServer(("127.0.0.1", 8765), _BackendHandler)
        except OSError as exc:
            pytest.skip(f"local test port 8765 unavailable: {exc}")
        backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
        backend_thread.start()

        status, _, payload = _request(debug.bound_port, "GET", "/api/status")
        assert status == 200
        assert json.loads(payload)["camera_online"] is True

        status, headers, payload = _request(debug.bound_port, "GET", "/api/preview.mjpg")
        assert status == 200
        assert headers["Content-Type"].startswith("multipart/x-mixed-replace")
        assert payload == b"frame-data"

        status, _, _ = _request(
            debug.bound_port,
            "POST",
            "/api/competition/enter",
            b"{}",
        )
        assert status == 200
        assert ("POST", "/api/competition/enter", b"{}") in _BackendHandler.requests
    finally:
        debug.stop()
        if backend is not None:
            backend.shutdown()
            backend.server_close()
        if backend_thread is not None:
            backend_thread.join(2)


def test_proxy_rejects_unsafe_bind_backend_paths_and_bodies() -> None:
    with pytest.raises(DebugWebConfigError, match="127.0.0.1"):
        DebugWebServer(host="0.0.0.0")
    with pytest.raises(DebugWebConfigError, match="8765"):
        parse_backend_url("http://127.0.0.1:9000")
    with pytest.raises(DebugWebConfigError):
        parse_backend_url("http://example.com:8765")

    debug = DebugWebServer(port=_free_port())
    try:
        debug.start()
        status, _, _ = _request(debug.bound_port, "GET", "/%2e%2e/secret")
        assert status == 400
        status, _, _ = _request(
            debug.bound_port,
            "PATCH",
            "/api/config/camera",
            b'{"controls":{"brightness":"not-a-number"',
        )
        assert status == 400

        connection = HTTPConnection("127.0.0.1", debug.bound_port, timeout=3)
        connection.putrequest("POST", "/api/runtime/save")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(64 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        assert response.status == 413
        connection.close()

        status, _, _ = _request(debug.bound_port, "GET", "/etc/passwd")
        assert status == 404
    finally:
        debug.stop()


def test_debug_frontend_has_no_web_serial_or_local_kiosk_action() -> None:
    html = (ROOT / "web_debug/static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "web_debug/static/app.js").read_text(encoding="utf-8")
    combined = html + javascript
    assert "navigator.serial" not in combined
    assert "Web Serial" not in combined
    assert "/api/kiosk/exit" not in combined
    assert "eventLog" in combined
    assert "MCU READY" in combined
