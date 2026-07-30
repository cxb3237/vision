from __future__ import annotations

from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from competition_ui.models import load_competition_ui_config
from competition_ui.server import CompetitionUIServer


ROOT = Path(__file__).resolve().parents[1]


class StubRecorder:
    def request_start(self):
        return {"state": "RECORDING", "active": True}

    def request_stop(self):
        return {"state": "IDLE", "active": False}

    def list_recordings(self):
        return []


class StubMedia:
    def __init__(self, config) -> None:
        self.config = config
        self.recorder = StubRecorder()
        self.frame_stream = SimpleNamespace(wait_for_jpeg=lambda *_args: (-1, None))

    def status(self):
        return {"recording": {"active": False}, "storage": {"free_bytes": 1}}


class StubRuntime:
    def __init__(self, media) -> None:
        self.competition_media_service = media

    def get_status_snapshot(self):
        return {"runtime_running": True, "camera_online": True}


def make_server(tmp_path: Path) -> CompetitionUIServer:
    config = replace(
        load_competition_ui_config(project_root=ROOT),
        port=0,
        recording_directory=tmp_path / "recordings",
    )
    config.recording_directory.mkdir()
    media = StubMedia(config)
    server = CompetitionUIServer(StubRuntime(media), config)
    server.start()
    return server


def request(server, method, path, body=None, headers=None):
    connection = HTTPConnection("127.0.0.1", server.bound_port, timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_health_status_and_static_page_are_available(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    try:
        assert request(server, "GET", "/healthz")[0] == 200
        assert request(server, "GET", "/api/status")[0] == 200
        status, headers, body = request(server, "GET", "/")
        assert status == 200
        assert "钢球比赛图传" in body.decode("utf-8")
        assert headers["X-Content-Type-Options"] == "nosniff"
    finally:
        server.stop()


def test_range_head_and_download_responses(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    movie = server.config.recording_directory / "H_safe.mp4"
    movie.write_bytes(b"0123456789")
    try:
        status, headers, body = request(
            server, "GET", "/recordings/H_safe.mp4", headers={"Range": "bytes=2-5"}
        )
        assert status == 206
        assert body == b"2345"
        assert headers["Content-Range"] == "bytes 2-5/10"
        assert headers["Accept-Ranges"] == "bytes"
        assert headers["Content-Length"] == "4"
        assert headers["Content-Type"] == "video/mp4"

        status, headers, body = request(server, "HEAD", "/recordings/H_safe.mp4")
        assert status == 200 and body == b""
        assert headers["Content-Length"] == "10"

        status, headers, body = request(server, "GET", "/recordings/H_safe.mp4?download=1")
        assert status == 200 and body == b"0123456789"
        assert headers["Content-Disposition"] == 'attachment; filename="H_safe.mp4"'
    finally:
        server.stop()


def test_recording_paths_reject_traversal_absolute_and_symlink(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"secret")
    link = server.config.recording_directory / "H_link.mp4"
    try:
        try:
            link.symlink_to(outside)
        except OSError:
            link = None
        assert request(server, "GET", "/recordings/%2e%2e%2foutside.mp4")[0] == 404
        assert request(server, "GET", "/recordings/C:%5csecret.mp4")[0] == 404
        if link is not None:
            assert request(server, "GET", "/recordings/H_link.mp4")[0] == 404
    finally:
        server.stop()


def test_post_body_limits_negative_length_and_invalid_json(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    try:
        assert request(
            server,
            "POST",
            "/api/recording/start",
            body=b"{" * (64 * 1024 + 1),
            headers={"Content-Length": str(64 * 1024 + 1)},
        )[0] == 413
        assert request(
            server,
            "POST",
            "/api/recording/start",
            body=b"{bad",
            headers={"Content-Length": "4"},
        )[0] == 400

        connection = HTTPConnection("127.0.0.1", server.bound_port, timeout=3)
        connection.putrequest("POST", "/api/recording/start")
        connection.putheader("Content-Length", "-1")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        assert response.status == 400
        connection.close()
    finally:
        server.stop()


def test_server_refuses_non_loopback_even_if_dataclass_is_manually_replaced(tmp_path: Path) -> None:
    config = replace(load_competition_ui_config(project_root=ROOT), host="0.0.0.0")
    media = StubMedia(config)
    with pytest.raises(ValueError, match="127.0.0.1"):
        CompetitionUIServer(StubRuntime(media), config)
