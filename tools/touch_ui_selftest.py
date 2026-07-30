"""触摸界面第一版的无硬件自检。"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time

import numpy as np

from touch_ui.api import TouchAPI
from touch_ui.frame_stream import LatestFrameStream
from touch_ui.models import CommandType, load_touch_ui_config
from touch_ui.runtime_config import RuntimeConfigStore
from touch_ui.state_store import StateStore


ROOT = Path(__file__).resolve().parents[1]


class _Runtime:
    def __init__(self) -> None:
        self.store = StateStore(
            {
                "runtime_running": True,
                "competition_mode": False,
                "vision_output_enabled": False,
                "detector": "steel_ball_yolo_ncnn",
            }
        )
        self.commands = []
        self.camera = {
            "controls": {
                "brightness": {
                    "supported": True,
                    "minimum": 0,
                    "maximum": 100,
                    "actual": 10,
                }
            }
        }

    def get_status_snapshot(self):
        return self.store.snapshot()

    def get_runtime_config_snapshot(self):
        return self.camera

    def submit_command(self, command_type, payload=None):
        self.commands.append((CommandType(command_type), dict(payload or {})))
        return f"selftest-{len(self.commands)}"


def run_selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="touch-ui-selftest-") as directory:
        temporary = Path(directory)
        config = load_touch_ui_config(
            ROOT / "config/touch_ui.yaml", project_root=temporary
        )
        assert config.runtime_directory.is_dir()

        runtime = _Runtime()
        api = TouchAPI(runtime)
        assert api.health()[1]["ok"]
        assert api.status()[1]["status"]["detector"] == "steel_ball_yolo_ncnn"
        response = api.patch_camera({"controls": {"brightness": 25}})
        assert response[0] == 202
        assert runtime.commands[-1][0] == CommandType.SET_CAMERA_CONTROL

        persistence = RuntimeConfigStore(config)
        persistence.save_camera_override({"brightness": 25})
        assert persistence.load_camera_override() == {"brightness": 25}
        persistence.save_ui_state(True)
        assert persistence.load_ui_state()["competition_mode"] is False
        assert persistence.restore_baseline()

        stream = LatestFrameStream(max_fps=20, jpeg_quality=75, max_width=320)
        stream.start()
        stream.submit_frame(np.full((60, 100, 3), 120, np.uint8))
        deadline = time.monotonic() + 1.0
        while stream.encoded_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stream.get_latest_jpeg(placeholder=False)
        stream.stop()

        runtime.store.update(competition_mode=True, vision_output_enabled=True)
        assert api.patch_camera({"controls": {"brightness": 30}})[1][
            "error_code"
        ] == "COMPETITION_MODE"


def main() -> int:
    try:
        run_selftest()
    except Exception as exc:
        print(f"Touch UI selftest: FAIL - {exc}", file=sys.stderr)
        return 1
    print("Touch UI selftest: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
