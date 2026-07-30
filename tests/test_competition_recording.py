from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import time

import numpy as np

from competition_ui.models import load_competition_ui_config
from competition_ui.recorder import CompetitionRecorder


ROOT = Path(__file__).resolve().parents[1]


class FakeWriter:
    instances = []
    opened = True

    def __init__(self, path, _fourcc, fps, resolution) -> None:
        self.path = Path(path)
        self.fps = fps
        self.resolution = resolution
        self.frames = []
        self.released = False
        if type(self).opened:
            self.path.write_bytes(b"fake-mp4")
        type(self).instances.append(self)

    def isOpened(self):
        return type(self).opened

    def write(self, image):
        self.frames.append(image.copy())

    def release(self):
        self.released = True


def config_for(tmp_path: Path, **changes):
    return replace(
        load_competition_ui_config(project_root=ROOT),
        recording_directory=tmp_path,
        minimum_free_space_mb=1,
        codec_candidates=("mp4v",),
        **changes,
    )


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


def test_timestamp_resampling_matches_real_elapsed_time(monkeypatch, tmp_path: Path) -> None:
    FakeWriter.instances.clear()
    FakeWriter.opened = True
    monkeypatch.setattr("competition_ui.recorder.cv2.VideoWriter", FakeWriter)
    monkeypatch.setattr("competition_ui.recorder.cv2.VideoWriter_fourcc", lambda *_: 1)
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr("competition_ui.recorder.time.monotonic", lambda: clock.value)
    recorder = CompetitionRecorder(config_for(tmp_path, recording_fps=10.0))
    recorder.start()
    recorder.request_start()
    frame = np.full((12, 16, 3), 50, dtype=np.uint8)

    recorder.submit_frame(1, 10.0, frame)
    wait_until(lambda: recorder.status()["written_frames"] == 1)
    clock.value = 0.2
    recorder.submit_frame(2, 10.2, frame + 1)
    wait_until(lambda: recorder.status()["written_frames"] == 2)
    clock.value = 1.0
    recorder.submit_frame(3, 11.0, frame + 2)
    wait_until(lambda: recorder.status()["written_frames"] == 10)
    recorder.request_stop()
    wait_until(lambda: recorder.status()["state"] == "IDLE")
    recorder.shutdown()

    movie = next(tmp_path.glob("*.mp4"))
    metadata = json.loads(movie.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["source_frame_count"] == 3
    assert metadata["written_frames"] == 10
    assert metadata["duplicated_frames"] == 7
    assert metadata["dropped_frames"] == 0
    assert metadata["first_frame_id"] == 1 and metadata["last_frame_id"] == 3
    assert metadata["duration_s"] == 1.0
    assert metadata["completed"] is True
    assert not list(tmp_path.glob(".*.tmp.mp4"))


def test_repeated_start_and_stop_are_idempotent(monkeypatch, tmp_path: Path) -> None:
    FakeWriter.opened = True
    monkeypatch.setattr("competition_ui.recorder.cv2.VideoWriter", FakeWriter)
    monkeypatch.setattr("competition_ui.recorder.cv2.VideoWriter_fourcc", lambda *_: 1)
    recorder = CompetitionRecorder(config_for(tmp_path))
    recorder.start()
    first = recorder.request_start()
    second = recorder.request_start()
    assert first["file_name"] == second["file_name"]
    assert recorder.request_stop()["state"] == "STOPPING"
    assert recorder.request_stop()["result"] == "ALREADY_STOPPED"
    wait_until(lambda: recorder.status()["state"] == "IDLE")
    recorder.shutdown()
    assert not list(tmp_path.glob("*.mp4")), "zero-frame session must not create a fake MP4"


def test_disk_shortage_refuses_recording(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "competition_ui.recorder.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=100, used=100, free=0),
    )
    recorder = CompetitionRecorder(config_for(tmp_path))
    recorder.start()
    status = recorder.request_start()
    recorder.shutdown()
    assert status["state"] == "ERROR"
    assert "磁盘空间不足" in status["error"]


def test_all_codecs_failing_produces_error_and_no_movie(monkeypatch, tmp_path: Path) -> None:
    FakeWriter.opened = False
    monkeypatch.setattr("competition_ui.recorder.cv2.VideoWriter", FakeWriter)
    monkeypatch.setattr("competition_ui.recorder.cv2.VideoWriter_fourcc", lambda *_: 1)
    recorder = CompetitionRecorder(config_for(tmp_path))
    recorder.start()
    recorder.request_start()
    recorder.submit_frame(1, 1.0, np.zeros((12, 16, 3), dtype=np.uint8))
    wait_until(lambda: recorder.status()["state"] == "ERROR")
    status = recorder.status()
    recorder.shutdown()
    assert "codec" in status["error"]
    assert not list(tmp_path.glob("*.mp4"))


def test_recording_list_ignores_hidden_files_and_symlinks(tmp_path: Path) -> None:
    recorder = CompetitionRecorder(config_for(tmp_path))
    movie = tmp_path / "H_valid.mp4"
    movie.write_bytes(b"video")
    movie.with_suffix(".json").write_text(
        json.dumps({
            "duration_s": 2.5,
            "written_frames": 75,
            "resolution": [640, 480],
            "container_fps": 30,
            "codec": "mp4v",
            "completed": True,
        }),
        encoding="utf-8",
    )
    (tmp_path / ".H_partial.mp4").write_bytes(b"partial")
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"outside")
    try:
        (tmp_path / "H_link.mp4").symlink_to(outside)
    except OSError:
        pass
    recordings = recorder.list_recordings()
    assert [item["file_name"] for item in recordings] == ["H_valid.mp4"]
    assert recordings[0]["duration_s"] == 2.5
    assert recordings[0]["resolution"] == [640, 480]
    assert recordings[0]["fps"] == 30
    assert recordings[0]["codec"] == "mp4v"
    assert recordings[0]["completed"] is True
