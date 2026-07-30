"""Loopback-only HTTP server for the competition media API."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from pathlib import Path
import re
import socket
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from competition_ui.api import CompetitionAPI


LOG = logging.getLogger(__name__)
MAX_REQUEST_BODY = 64 * 1024
REQUEST_BODY_TIMEOUT_S = 2.0
SAFE_RECORDING_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.mp4\Z")


class _CompetitionHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def parse_byte_range(value: str, size: int) -> tuple[int, int] | None:
    """Return an inclusive byte range or ``None`` for an invalid range."""

    if not value.startswith("bytes=") or "," in value or size <= 0:
        return None
    spec = value[6:].strip()
    if "-" not in spec:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = size - 1 if not end_text else int(end_text)
            if start < 0 or end < start or start >= size:
                return None
            end = min(end, size - 1)
    except ValueError:
        return None
    return start, end


class CompetitionUIServer:
    def __init__(self, runtime: Any, config: Any) -> None:
        if config.host != "127.0.0.1":
            raise ValueError("比赛后端只允许监听 127.0.0.1")
        if runtime.competition_media_service is None:
            raise ValueError("比赛媒体服务未配置")
        self.runtime = runtime
        self.config = config
        self.media = runtime.competition_media_service
        self.api = CompetitionAPI(runtime, self.media)
        self.root = Path(__file__).resolve().parents[1] / "web_competition"
        self.server: _CompetitionHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        return int(self.server.server_address[1]) if self.server is not None else 0

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.server = _CompetitionHTTPServer(
            (self.config.host, self.config.port), self._handler_type()
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="competition-ui-http",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        server, thread = self.server, self.thread
        self.server = None
        self.thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(2)

    def _recording_path(self, raw_name: str) -> Path | None:
        name = unquote(raw_name)
        if not SAFE_RECORDING_NAME.fullmatch(name) or "/" in name or "\\" in name:
            return None
        candidate = self.config.recording_directory / name
        try:
            if candidate.is_symlink() or not candidate.is_file():
                return None
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.config.recording_directory.resolve())
        except (OSError, ValueError):
            return None
        return resolved

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format_string: str, *args: object) -> None:
                LOG.debug("competition HTTP: " + format_string, *args)

            def _headers(
                self,
                status: int,
                content_type: str,
                length: int,
                cache_control: str = "no-store",
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", cache_control)
                self.send_header("X-Content-Type-Options", "nosniff")

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._headers(status, "application/json; charset=utf-8", len(data))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def _read_json(self) -> tuple[bool, object]:
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError:
                    self._json(400, {"ok": False, "message": "Content-Length 非法"})
                    return False, None
                if length < 0:
                    self._json(400, {"ok": False, "message": "Content-Length 不能为负数"})
                    return False, None
                if length > MAX_REQUEST_BODY:
                    self._json(413, {"ok": False, "message": "请求体超过 64 KiB"})
                    return False, None
                previous_timeout = self.connection.gettimeout()
                try:
                    self.connection.settimeout(REQUEST_BODY_TIMEOUT_S)
                    data = self.rfile.read(length)
                except (socket.timeout, TimeoutError):
                    self.close_connection = True
                    self._json(408, {"ok": False, "message": "请求体读取超时"})
                    return False, None
                except OSError:
                    self.close_connection = True
                    self._json(400, {"ok": False, "message": "请求体读取失败"})
                    return False, None
                finally:
                    try:
                        self.connection.settimeout(previous_timeout)
                    except OSError:
                        pass
                if len(data) != length:
                    self.close_connection = True
                    self._json(400, {"ok": False, "message": "请求体不完整"})
                    return False, None
                try:
                    return True, json.loads(data) if data else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._json(400, {"ok": False, "message": "JSON 格式非法"})
                    return False, None

            def _static(self, name: str) -> None:
                path = owner.root / name
                try:
                    data = path.read_bytes()
                except OSError:
                    self._json(404, {"ok": False, "message": "页面资源不存在"})
                    return
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                cache_control = "public, max-age=300" if path.suffix in {".css", ".js"} else "no-cache"
                self._headers(200, content_type, len(data), cache_control)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def _preview(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last_id = -1
                try:
                    while owner.server is not None:
                        frame_id, jpeg = owner.media.frame_stream.wait_for_jpeg(last_id, 1.0)
                        if not jpeg:
                            continue
                        last_id = frame_id
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(jpeg)).encode("ascii")
                            + b"\r\n\r\n"
                            + jpeg
                            + b"\r\n"
                        )
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def _recording(self, raw_name: str, *, head_only: bool) -> None:
                path = owner._recording_path(raw_name)
                if path is None:
                    self._json(404, {"ok": False, "message": "录像不存在"})
                    return
                size = path.stat().st_size
                range_header = self.headers.get("Range")
                byte_range = parse_byte_range(range_header, size) if range_header else None
                if range_header and byte_range is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                start, end = byte_range if byte_range else (0, max(0, size - 1))
                length = max(0, end - start + 1)
                self._headers(206 if byte_range else 200, "video/mp4", length)
                self.send_header("Accept-Ranges", "bytes")
                if byte_range:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if query.get("download") == ["1"]:
                    self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
                self.end_headers()
                if head_only or length == 0:
                    return
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)

            def _dispatch_get(self, *, head_only: bool = False) -> None:
                path = urlsplit(self.path).path
                if path == "/healthz":
                    self._json(*owner.api.health())
                elif path == "/api/status":
                    self._json(*owner.api.status())
                elif path == "/api/recordings":
                    self._json(*owner.api.recordings())
                elif path == "/api/preview.mjpg" and not head_only:
                    self._preview()
                elif path.startswith("/recordings/"):
                    self._recording(path[len("/recordings/"):], head_only=head_only)
                elif path in {
                    "/",
                    "/index.html",
                    "/app.js",
                    "/style.css",
                    "/mjpeg_reconnect.js",
                    "/recording_finalize.js",
                }:
                    self._static("index.html" if path in {"/", "/index.html"} else path[1:])
                else:
                    self._json(404, {"ok": False, "message": "资源不存在"})

            def do_GET(self) -> None:
                self._dispatch_get()

            def do_HEAD(self) -> None:
                self._dispatch_get(head_only=True)

            def do_POST(self) -> None:
                ok, body = self._read_json()
                if not ok:
                    return
                path = urlsplit(self.path).path
                if path == "/api/recording/start":
                    self._json(*owner.api.start_recording(body))
                elif path == "/api/recording/stop":
                    self._json(*owner.api.stop_recording(body))
                else:
                    self._json(404, {"ok": False, "message": "资源不存在"})

        return Handler
