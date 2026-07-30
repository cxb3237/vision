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
        "比赛后端不可用",
    ):
        assert required in combined
    assert ".action-bar" in css
    assert "grid-template-columns: repeat(3" in css


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


def test_mcu_fields_have_chinese_meanings_and_units() -> None:
    javascript = (ROOT / "web_debug/static/app.js").read_text(encoding="utf-8")
    for label in (
        "控制状态", "故障码", "闭环使能", "位置", " mm", "速度", " mm/s",
        "误差", "请求倾角", "实际倾角", "脉宽", " μs", "数据帧龄", " ms",
        "稳定状态", "接受帧数", "拒绝帧数",
    ):
        assert label in javascript
