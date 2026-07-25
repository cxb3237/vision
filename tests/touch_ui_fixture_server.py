"""仅供本地浏览器几何检查使用的无硬件触摸UI夹具服务。"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "touch_ui_web"
CONTROLS = {
    "brightness": {
        "supported": True,
        "minimum": -64,
        "maximum": 64,
        "step": 1,
        "requested": 0,
        "actual": 0,
        "mismatch": False,
    },
    "contrast": {
        "supported": True,
        "minimum": 0,
        "maximum": 64,
        "step": 1,
        "requested": 16,
        "actual": 16,
        "mismatch": False,
    },
    "gain": {
        "supported": True,
        "minimum": 0,
        "maximum": 255,
        "step": 1,
        "requested": 7,
        "actual": 7,
        "mismatch": False,
    },
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def _json(self, value: dict) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/status":
            self._json(
                {
                    "ok": True,
                    "status": {
                        "runtime_running": True,
                        "camera_online": True,
                        "serial_online": True,
                        "vmc_tx_count": 3,
                        "detector": "digit",
                        "state": "LOCKED",
                        "fps": 30,
                        "commands": {},
                        "ui": {"parameter_debounce_ms": 20},
                    },
                }
            )
            return
        if path == "/api/config/camera":
            self._json(
                {
                    "ok": True,
                    "camera": {
                        "controls": CONTROLS,
                        "modified": False,
                        "override_file_active": False,
                    },
                }
            )
            return
        name = "index.html" if path in {"/", "/index.html"} else path.removeprefix("/")
        candidate = (WEB_ROOT / name).resolve()
        try:
            candidate.relative_to(WEB_ROOT.resolve())
            data = candidate.read_bytes()
        except (ValueError, OSError):
            self.send_error(404)
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(candidate.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
