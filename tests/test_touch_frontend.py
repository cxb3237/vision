"""触摸前端结构和恢复同步契约的静态回归测试。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_web_is_steel_ball_only_and_never_uses_browser_serial() -> None:
    html = (ROOT / "touch_ui_web/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "touch_ui_web/app.js").read_text(encoding="utf-8")
    server = (ROOT / "touch_ui/server.py").read_text(encoding="utf-8")
    combined = html + javascript
    assert "steelBallStatus" in html
    assert "ballPixelPosition" in html
    assert "mcuStatus" in html and "lastSentPosition" in html
    assert "navigator.serial" not in combined
    assert "/api/detector" not in server
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


def test_compact_footer_has_three_short_single_line_actions() -> None:
    assert 'grid-template-columns: repeat(3, minmax(0, 1fr))' in CSS
    assert '>保存</button>' in HTML and '>上次</button>' in HTML and '>基准</button>' in HTML
    assert 'aria-label="保存现场参数" title="保存现场参数"' in HTML
    assert 'aria-label="恢复上次有效参数" title="恢复上次有效参数"' in HTML
    assert 'aria-label="恢复启动基准参数" title="恢复启动基准参数"' in HTML
    assert HTML.index('id="operationResult"') < HTML.index('class="drawer-footer"')
    assert '.drawer-footer button' in CSS and 'white-space: nowrap' in CSS


def test_touch_range_uses_direction_lock_without_blocking_vertical_scroll() -> None:
    assert 'touch-action: pan-y' in CSS
    assert 'classifyPointerGesture(' in JS
    assert 'gesture.mode === "horizontal"' in JS
    assert 'setPointerCapture(event.pointerId)' in JS
    assert 'window.addEventListener("pointermove", onPointerMove, {passive: false})' in JS
    assert 'window.addEventListener("pointercancel", onPointerCancel)' in JS


def test_camera_controls_are_compact_chinese_and_writable_only() -> None:
    assert 'controlIsWritable(name, info)' in JS
    assert 'controlDependencyHidden(name, cameraControlsState)' in JS
    assert 'controlDisplayName(name)' in JS
    assert 'setDiagnostic("正在应用")' in JS
    assert 'setDiagnostic("应用失败", true)' in JS
    assert '"图像"' in JS and '"曝光"' in JS and '"白平衡"' in JS
    assert '.control-inputs input[type="range"]' in CSS and 'height: 32px' in CSS
    assert 'setInterval(() =>' in JS and '}, 150)' in JS


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


def test_applied_control_renders_once_and_preserves_scroll_position() -> None:
    assert "setTimeout(renderCameraControls" not in JS
    assert '["DEBOUNCE", "SENT"].includes(phase)' in JS
    assert 'controlScheduler.phase(name) !== "IDLE"' not in JS
    assert "cameraControlsRenderCount += 1" in JS
    assert "const previousScrollTop = container.scrollTop" in JS
    assert "container.scrollTop = Math.min(" in JS


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


def test_performance_panel_prefers_vision_fps_with_legacy_fallback() -> None:
    assert "status.vision_fps ?? status.fps ?? 0" in JS
    assert 'id="cameraFps"' in HTML
    assert 'id="previewFps"' in HTML
    assert 'id="pipelineFps"' in HTML
    assert 'id="pipelineLatency"' in HTML
    assert 'id="pipelineDrops"' in HTML


def test_frontend_displays_calibration_error_and_real_uart_rates() -> None:
    assert "ball_position_calibration_error" in JS
    assert "position_tx_hz" in JS
    assert "invalid_tx_hz" in JS


def test_performance_panel_keeps_ncnn_and_end_to_end_latency_distinct() -> None:
    assert "status.inference_ms" in JS
    assert "status.inference_median_ms" in JS
    assert "status.inference_p95_ms" in JS
    assert "status.capture_to_result_ms" in JS
    assert "status.capture_to_result_p95_ms" in JS
    assert "status.preview_overwritten_count" in JS
