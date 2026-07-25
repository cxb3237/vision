"""触摸前端结构和恢复同步契约的静态回归测试。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "touch_ui_web/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "touch_ui_web/style.css").read_text(encoding="utf-8")
JS = (ROOT / "touch_ui_web/app.js").read_text(encoding="utf-8")


def test_page_is_fixed_viewport_with_non_overlapping_side_dock() -> None:
    assert "height: 100dvh" in CSS
    assert "html, body" in CSS and "overflow: hidden" in CSS
    assert "grid-template-columns: minmax(0, 1fr) var(--dock-width)" in CSS
    assert "clamp(280px, 35vw, 420px)" in CSS
    assert HTML.index('id="previewPane"') < HTML.index('id="sideDock"')
    assert HTML.index('id="cameraPanel"') > HTML.index('id="sideDock"')


def test_camera_parameters_have_the_only_independent_scroll_area() -> None:
    assert 'id="cameraControls" class="camera-controls-scroll"' in HTML
    assert ".camera-controls-scroll" in CSS and "overflow-y: auto" in CSS
    assert ".camera-drawer" in CSS and "inset: 64px 0 0" in CSS
    assert HTML.index('id="drawerHandle"') < HTML.index('id="maintenanceButton"')


def test_drawer_supports_click_drag_snap_and_pointer_cancel() -> None:
    for event in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
        assert event in JS
    assert "drag.width * 0.4" in JS
    assert "setDrawerOpen(!drag.wasOpen)" in JS
    assert "finishDrawerDrag(event, true)" in JS
    assert "user-select: none" in CSS and "transition: transform 200ms" in CSS


def test_restore_and_save_wait_then_replace_full_camera_state() -> None:
    assert 'runCommand("/api/runtime/restore-baseline"' in JS
    assert 'runCommand("/api/runtime/restore-last-good"' in JS
    assert 'runCommand("/api/runtime/save"' in JS
    assert "await waitForCommand(queued.command_id" in JS
    assert "if (refreshCamera) await loadCameraControls({resetScheduler: true})" in JS
    assert "Object.fromEntries" in JS and "container.replaceChildren()" in JS
    assert "controlScheduler.cancelDebounced()" in JS
    assert "controlScheduler.flushDebounced()" in JS
    for control in (
        "gain",
        "white_balance_automatic",
        "white_balance_temperature",
        "exposure_auto",
        "exposure_absolute",
    ):
        assert control in JS


def test_restore_failure_does_not_reload_or_claim_success() -> None:
    assert 'command?.status === "FAILED"' in JS
    success_index = JS.index("if (refreshCamera) await loadCameraControls({resetScheduler: true})")
    catch_index = JS.index("} catch (error) {", success_index)
    assert success_index < catch_index


def test_maintenance_requires_long_press_and_danger_confirmation() -> None:
    assert 'id="maintenanceButton"' in HTML
    assert "performance.now() - maintenanceStarted) / 2000" in JS
    assert 'maintenanceButton").addEventListener("pointerdown"' in JS
    assert "confirmDanger(" in JS
    assert 'request("/api/runtime/stop"' in JS
    assert 'request("/api/kiosk/exit"' in JS


def test_portrait_layout_keeps_preview_above_dock() -> None:
    assert "@media (orientation: portrait), (max-width: 700px)" in CSS
    assert "grid-template-rows: minmax(0, 56dvh) minmax(0, 44dvh)" in CSS
