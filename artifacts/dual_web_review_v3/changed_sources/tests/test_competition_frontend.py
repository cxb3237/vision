from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_competition_page_has_preview_recording_list_playback_and_download() -> None:
    html = (ROOT / "web_competition/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "web_competition/app.js").read_text(encoding="utf-8")
    css = (ROOT / "web_competition/style.css").read_text(encoding="utf-8")
    combined = html + javascript
    for required in (
        "/api/preview.mjpg",
        "startRecording",
        "stopRecording",
        "recordingsList",
        "playback",
        "download_url",
        "writtenFrames",
        "droppedFrames",
        "actualFps",
        "freeSpace",
        "比赛后端未连接",
    ):
        assert required in combined
    assert ".action-bar" in css
    assert "grid-template-columns: repeat(3" in css
    assert "streamStatus" in html
    assert "offlineNoticeTitle" in html
    assert "mjpeg_reconnect.js" in html
    assert "recording_finalize.js" in html


def test_competition_page_does_not_expose_uart_position_or_system_controls() -> None:
    combined = "\n".join(
        (ROOT / f"web_competition/{name}").read_text(encoding="utf-8")
        for name in ("index.html", "app.js", "style.css")
    ).lower()
    for forbidden in (
        "navigator.serial",
        "uart",
        "位置下发",
        "曝光",
        "ncnn",
        "/api/runtime/stop",
        "/api/shell",
    ):
        assert forbidden not in combined


def test_debug_page_uses_fixed_bottom_actions_and_no_raw_mcu_json() -> None:
    html = (ROOT / "web_debug/static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "web_debug/static/style.css").read_text(encoding="utf-8")
    javascript = (ROOT / "web_debug/static/app.js").read_text(encoding="utf-8")
    assert html.index('id="normalDock"') < html.index('class="persistent-actions"')
    assert "grid-template-rows: 64px auto minmax(0, 1fr) 60px" in css
    assert ".persistent-actions" in css
    assert 'id="showCamera"' in html and 'id="enterCompetition"' in html
    assert 'id="mcuStatus"' not in html
    assert "JSON.stringify(status.mcu_status" not in javascript
    assert "<details class=\"advanced-info\">" in html


def test_debug_status_logic_requires_request_uart_ready_and_positive_rate() -> None:
    javascript = (ROOT / "web_debug/static/app.js").read_text(encoding="utf-8")
    assert "function positionDeliveryState(status)" in javascript
    assert "!requested" in javascript
    assert "!serialOnline" in javascript
    assert "!ready" in javascript
    assert "rate > 0" in javascript
    assert 'return "位置下发运行中"' in javascript
    assert 'return "已启用但当前没有有效位置"' in javascript
    assert 'return "位置流中断"' in javascript


def test_competition_online_states_and_media_failure_are_not_conflated() -> None:
    javascript = (ROOT / "web_competition/app.js").read_text(encoding="utf-8")
    for state in ("backendOnline", "cameraOnline", "streamOnline", "mediaWorkerAlive"):
        assert state in javascript
    for message in (
        "比赛后端未连接",
        "摄像头离线",
        "视频流正在连接",
        "视频流正在重连",
        "视频流正常",
        "比赛媒体服务故障",
        "录像后端故障",
    ):
        assert message in javascript
    assert 'byId("offlineNotice").hidden = true' not in javascript


def test_recording_finalize_waits_for_backend_state_and_shows_codec_details() -> None:
    javascript = (ROOT / "web_competition/app.js").read_text(encoding="utf-8")
    finalize = (ROOT / "web_competition/recording_finalize.js").read_text(encoding="utf-8")
    assert "setTimeout(loadRecordings, 400)" not in javascript
    assert "RecordingFinalizeController" in javascript
    assert 'recording.state === "STOPPING"' in javascript
    assert 'recording.state === "IDLE"' in finalize
    assert 'recording.state === "ERROR"' in finalize
    assert "timeoutMs: 15000" in javascript
    for field in ("resolution", "item.fps", "item.codec", "item.completed"):
        assert field in javascript
    assert "当前编码可能不被部分平板浏览器直接播放" in javascript


def test_debug_page_has_independent_backend_camera_and_stream_notice() -> None:
    html = (ROOT / "web_debug/static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "web_debug/static/app.js").read_text(encoding="utf-8")
    assert "debugStreamNotice" in html
    assert "mjpeg_reconnect.js" in html
    for message in ("视觉后端未连接", "摄像头离线", "调试视频流正在连接", "调试视频流重连中"):
        assert message in javascript


def test_mcu_fields_have_chinese_meanings_and_units() -> None:
    javascript = (ROOT / "web_debug/static/app.js").read_text(encoding="utf-8")
    for label in (
        "控制状态", "故障码", "闭环使能", "位置", " mm", "速度", " mm/s",
        "误差", "请求倾角", "实际倾角", "脉宽", " μs", "数据帧龄", " ms",
        "稳定状态", "接受帧数", "拒绝帧数",
    ):
        assert label in javascript
