"use strict";

const $ = (id) => document.getElementById(id);
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
let cameraControlsLoaded = false;
let cameraControlsState = Object.create(null);
let cameraProfileState = {modified: false, override_file_active: false};
let pollInterval = 250;
let parameterDebounce = 150;
let statusPolling = true;
let drawerOpen = false;
let drawerDrag = null;
let maintenanceTimer = null;
let maintenanceStarted = 0;
let maintenanceOpenedByHold = false;
let confirmResolver = null;
let cameraControlsRenderCount = 0;
let competitionModeEnabled = false;
let lastCalibrationValid = false;
const controlFailures = new Map();
const controlScheduler = new ControlUpdateScheduler({
  debounceMs: parameterDebounce,
  apply: applySingleControl,
  onApplied: async (name, _value, camera) => {
    if (!camera.controls?.[name]) throw new Error(`服务器同步结果缺少控制项 ${name}`);
    if (["white_balance_automatic", "exposure_auto", "focus_auto"].includes(name)) {
      Object.entries(camera.controls).forEach(([controlName, info]) => {
        cameraControlsState[controlName] = {...info};
      });
    } else {
      cameraControlsState[name] = {...camera.controls[name]};
    }
    controlFailures.delete(name);
    cameraProfileState = {
      modified: !!camera.modified,
      override_file_active: !!camera.override_file_active,
    };
    renderCameraControls();
    renderCurrentProfile();
  },
  onError: (name, error) => {
    controlFailures.set(name, error.message || String(error));
    renderCameraControls();
    toast(error.message);
  },
});
const CONTROL_GROUPS = [
  ["图像", ["brightness", "contrast", "saturation", "sharpness", "hue", "gamma"]],
  ["曝光", ["exposure_auto", "exposure_absolute", "gain", "backlight_compensation", "power_line_frequency"]],
  ["白平衡", ["white_balance_automatic", "white_balance_temperature"]],
];
async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    cache: "no-store",
    ...options,
  });
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.message || `HTTP ${response.status}`);
  return data;
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.add("show");
  setTimeout(() => $("toast").classList.remove("show"), 2600);
}

function text(id, value) {
  const element = $(id);
  if (element) element.textContent = value;
}

function setTag(id, label, active, warning = false) {
  const element = $(id);
  text(id, label);
  element.classList.toggle("offline", !active);
  element.classList.toggle("warning", warning);
}

function positionDeliveryState(status) {
  const requested = !!status.vision_output_enabled;
  const serialOnline = !!status.serial_online;
  const ready = !!status.mcu_ready;
  const rate = Number(status.position_tx_hz || 0);
  const uartState = String(status.uart_state || "").toUpperCase();
  if (!requested) {
    if (uartState.includes("STOP_REQUESTED") || uartState.includes("STOPPING")) return "正在停止";
    if (uartState.includes("STOPPED")) return "已停止";
    return "未请求位置下发";
  }
  if (!serialOnline) {
    if (/ERROR|FAULT|DISCONNECT|FAILED/.test(uartState)) return "UART故障";
    return "已请求位置下发，等待UART";
  }
  if (!ready) return "已请求位置下发，等待MCU READY";
  if (rate > 0) return "位置下发运行中";
  if (status.ball_x_mm === null || status.ball_x_mm === undefined) return "已启用但当前没有有效位置";
  return "位置流中断";
}

function renderStatus(status) {
  competitionModeEnabled = !!status.competition_mode;
  pollInterval = status.ui?.status_poll_interval_ms || pollInterval;
  parameterDebounce = status.ui?.parameter_debounce_ms ?? parameterDebounce;
  controlScheduler.setDebounceMs(parameterDebounce);
  const visionFps = Number(status.vision_fps ?? status.fps ?? 0);
  const cameraFps = Number(status.camera_fps ?? 0);
  const previewFps = Number(status.preview_fps ?? 0);
  const captureToResult = Number(status.capture_to_result_ms ?? 0);
  const captureToResultP95 = Number(status.capture_to_result_p95_ms ?? 0);
  text("fps", visionFps.toFixed(1));
  text("cameraFps", cameraFps.toFixed(1));
  text("cameraFpsDetail", cameraFps.toFixed(1));
  text("previewFps", previewFps.toFixed(1));
  text("visionFpsDetail", visionFps.toFixed(1));
  text("pipelineFps", `${cameraFps.toFixed(1)} / ${visionFps.toFixed(1)} / ${previewFps.toFixed(1)} FPS`);
  text("pipelineLatency", `${captureToResult.toFixed(1)} / ${captureToResultP95.toFixed(1)} ms`);
  text(
    "pipelineDrops",
    `${Number(status.vision_skipped_camera_frames || 0)} / ${Number(status.preview_overwritten_count || 0)}`,
  );
  const positionTxCount = Number(status.position_tx_count ?? 0);
  const positionTxHz = Number(status.position_tx_hz ?? 0);
  const invalidTxHz = Number(status.invalid_tx_hz ?? 0);
  text("txCount", `${positionTxCount} · POS ${positionTxHz.toFixed(1)} Hz · INVALID ${invalidTxHz.toFixed(1)} Hz`);
  const calibrated = !!status.ball_position_calibrated;
  lastCalibrationValid = calibrated;
  const calibrationError = status.ball_position_calibration_error || "";
  const xMillimetres = status.ball_x_mm;
  const xPixels = status.ball_x_px;
  text(
    "ballPosition",
    !calibrated
      ? "-- mm"
      : (xMillimetres === null || xMillimetres === undefined
        ? "-- mm"
        : `${Number(xMillimetres) > 0 ? "+" : ""}${Number(xMillimetres)} mm`),
  );
  text("ballPixelPosition", xPixels === null || xPixels === undefined ? "—" : xPixels);
  text("mappingState", calibrated ? "已标定" : (calibrationError || "未标定"));
  text("calibrationError", calibrationError || "--");
  text("leftEndpoint", status.left_endpoint_px ?? "--");
  text("rightEndpoint", status.right_endpoint_px ?? "--");
  text("servoSide", status.servo_side ?? "--");
  const latestFrameAge = status.latest_frame_age_s;
  text("latestFrameAge", latestFrameAge === null || latestFrameAge === undefined ? "--" : `${Number(latestFrameAge).toFixed(3)} s`);
  const detected = xPixels !== null && xPixels !== undefined;
  text("ballDetected", detected ? "已识别" : "未识别");
  text("ballConfidence", `${(Number(status.confidence || 0) / 10).toFixed(1)}%`);
  text("uartState", status.uart_state || "串口未打开");
  text("uartPortState", status.uart_port_open ? "已打开" : "未打开");
  text("mcuReadyState", status.mcu_ready ? "是" : "否");
  text("positionTxRate", `${positionTxHz.toFixed(1)} Hz`);
  text("invalidTxRate", `${invalidTxHz.toFixed(1)} Hz`);
  const mcuFields = status.mcu_status || {};
  const mcuContainer = $("mcuFields");
  mcuContainer.replaceChildren();
  const mcuDescriptions = {
    S: ["控制状态", ""], F: ["故障码", ""], EN: ["闭环使能", ""],
    X: ["位置", " mm"], V: ["速度", " mm/s"], E: ["误差", " mm"],
    RQ: ["请求倾角", ""], AP: ["实际倾角", ""], PW: ["脉宽", " μs"],
    AGE: ["数据帧龄", " ms"], ST: ["稳定状态", ""],
    AC: ["接受帧数", ""], RJ: ["拒绝帧数", ""],
  };
  ["S", "F", "EN", "X", "V", "E", "RQ", "AP", "PW", "AGE", "ST", "AC", "RJ"].forEach((name) => {
    const wrapper = document.createElement("div");
    const label = document.createElement("dt");
    const value = document.createElement("dd");
    label.textContent = `${name} ${mcuDescriptions[name][0]}`;
    value.textContent = mcuFields[name] === null || mcuFields[name] === undefined
      ? "--"
      : `${mcuFields[name]}${mcuDescriptions[name][1]}`;
    wrapper.append(label, value);
    mcuContainer.append(wrapper);
  });
  text("keyS", mcuFields.S ?? "--");
  text("keyF", mcuFields.F ?? "--");
  text("keyEN", mcuFields.EN ?? "--");
  text("keyAGE", mcuFields.AGE ?? "--");
  text("keyAC", mcuFields.AC ?? "--");
  text("keyRJ", mcuFields.RJ ?? "--");
  text("lastSentPosition", status.last_sent_position_mm === null || status.last_sent_position_mm === undefined ? "--" : `${status.last_sent_position_mm} mm`);
  text("lastError", status.last_uart_error || status.detector_error || status.last_error || "无错误");
  const steelBallPanel = $("steelBallStatus");
  if (steelBallPanel) {
    steelBallPanel.hidden = false;
    text("steelBallBackend", "YOLO-NCNN");
    text("steelBallModel", status.model_loaded ? "已加载" : "未加载");
    text(
      "steelBallTiming",
      `${Number(status.inference_ms || 0).toFixed(1)} / ${Number(status.inference_median_ms || 0).toFixed(1)} / ${Number(status.inference_p95_ms || 0).toFixed(1)} ms`,
    );
    text("steelBallE2E", `${captureToResult.toFixed(1)} / ${captureToResultP95.toFixed(1)} ms`);
  }
  setTag("runningBadge", status.runtime_running ? "服务运行中" : "服务已停止", !!status.runtime_running);
  setTag("cameraBadge", status.camera_online ? "摄像头在线" : "摄像头离线", !!status.camera_online);
  setTag("serialBadge", status.serial_online ? "串口在线" : "串口离线", !!status.serial_online);
  setTag("mcuBadge", status.mcu_ready ? "MCU已就绪" : "MCU未就绪", !!status.mcu_ready, !status.mcu_ready);
  const deliveryState = positionDeliveryState(status);
  text("positionTxBadge", deliveryState);
  $("positionTxBadge").classList.toggle("running", deliveryState === "位置下发运行中");
  $("positionTxBadge").classList.toggle("fault", deliveryState === "UART故障");
  const dirty = !!status.runtime_modified || controlScheduler.hasPending();
  setTag("dirtyBadge", dirty ? "配置已修改" : "配置未修改", !dirty, dirty);
  const visionOutputEnabled = !!status.vision_output_enabled;
  setTag(
    "competitionBadge",
    visionOutputEnabled ? "比赛识别有效" : "调试识别",
    true,
    visionOutputEnabled,
  );
  text("enterCompetition", competitionModeEnabled ? "停止位置下发" : "启用位置下发");
  $("enterCompetition").disabled = !competitionModeEnabled && !calibrated;
  $("enterCompetition").title = (!competitionModeEnabled && !calibrated)
    ? (calibrationError || "未标定，禁止启用位置下发")
    : "";
}

function renderBackendUnavailable(message) {
  setTag("runningBadge", "后端不可用", false, true);
  setTag("cameraBadge", "摄像头未知", false, true);
  setTag("serialBadge", "串口未知", false, true);
  setTag("mcuBadge", "MCU状态未知", false, true);
  $("enterCompetition").disabled = true;
  text("lastError", `视觉主服务未连接：${message}`);
}

async function pollStatus() {
  if (!statusPolling) return;
  try {
    const data = await request("/api/status");
    renderStatus(data.status);
  } catch (error) {
    renderBackendUnavailable(error.message);
  } finally {
    if (statusPolling) setTimeout(pollStatus, pollInterval);
  }
}

async function pollEvents() {
  try {
    const data = await request("/debug/events");
    const container = $("eventLog");
    container.replaceChildren();
    const events = Array.isArray(data.events) ? data.events.slice(-20).reverse() : [];
    (events.length ? events : [{timestamp: "--", level: "INFO", message: "暂无事件"}]).forEach((item) => {
      const row = document.createElement("li");
      row.textContent = `${item.timestamp} ${item.level} ${item.message}`;
      container.append(row);
    });
  } catch (_error) {
    // The visual backend may serve this same static page internally without the debug proxy.
  } finally {
    setTimeout(pollEvents, 2000);
  }
}

function effectiveControlValue(name, info) {
  const desired = controlScheduler.desiredValue(name);
  if (desired !== undefined) return desired;
  return info.requested ?? info.actual ?? info.minimum;
}

function controlDependencyHidden(name, controls) {
  const dependency = {
    white_balance_temperature: "white_balance_automatic",
    exposure_absolute: "exposure_auto",
    focus_absolute: "focus_auto",
  }[name];
  if (!dependency || !controls[dependency]) return false;
  const autoInfo = controls[dependency];
  return automaticModeEnabled(dependency, effectiveControlValue(dependency, autoInfo), autoInfo);
}

function makeSliderModel(info) {
  const choices = normalizedChoices(info);
  if (choices.length) {
    const values = choices.map((choice) => choice.value);
    return {
      minimum: 0,
      maximum: values.length - 1,
      step: 1,
      toPosition(value) {
        const exact = values.indexOf(Number(value));
        if (exact >= 0) return exact;
        return values.reduce(
          (best, candidate, index) => Math.abs(candidate - Number(value)) < best.distance
            ? {index, distance: Math.abs(candidate - Number(value))}
            : best,
          {index: 0, distance: Infinity},
        ).index;
      },
      fromPosition(position) {
        const index = quantizeControlValue(position, 0, values.length - 1, 1);
        return values[index];
      },
    };
  }
  return {
    minimum: Number(info.minimum),
    maximum: Number(info.maximum),
    step: Number(info.step),
    toPosition(value) {
      return quantizeControlValue(value, this.minimum, this.maximum, this.step);
    },
    fromPosition(position) {
      return quantizeControlValue(position, this.minimum, this.maximum, this.step);
    },
  };
}

function installRepeatingButton(button, previewByOneStep, commitPreview) {
  let holdTimer = null;
  let repeatTimer = null;
  let repeated = false;

  const stopTimers = () => {
    clearTimeout(holdTimer);
    clearInterval(repeatTimer);
    holdTimer = null;
    repeatTimer = null;
  };
  button.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    stopTimers();
    repeated = false;
    holdTimer = setTimeout(() => {
      repeated = true;
      if (!previewByOneStep()) return;
      repeatTimer = setInterval(() => {
        if (!previewByOneStep()) stopTimers();
      }, 150);
    }, 400);
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach((eventName) => {
    button.addEventListener(eventName, () => {
      stopTimers();
      if (repeated) commitPreview();
    });
  });
  button.addEventListener("click", (event) => {
    if (repeated) {
      event.preventDefault();
      repeated = false;
      return;
    }
    if (previewByOneStep()) commitPreview();
  });
}

function installTouchRange(input, row, model, initialRawValue, setVisualValue, commitValue) {
  let gesture = null;
  let suppressTouchClickUntil = 0;

  const releaseCapture = (pointerId) => {
    try {
      if (input.hasPointerCapture(pointerId)) input.releasePointerCapture(pointerId);
    } catch (_error) {
      // pointercancel后浏览器可能已自动释放。
    }
  };
  const cleanup = (pointerId) => {
    releaseCapture(pointerId);
    row.classList.remove("adjusting");
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
    window.removeEventListener("pointercancel", onPointerCancel);
    gesture = null;
  };
  const valueAt = (clientX) => {
    const rect = input.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / Math.max(1, rect.width)));
    return model.fromPosition(model.minimum + ratio * (model.maximum - model.minimum));
  };
  const onPointerMove = (event) => {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    if (gesture.mode === "pending") {
      gesture.mode = classifyPointerGesture(
        event.clientX - gesture.startX,
        event.clientY - gesture.startY,
      );
      if (gesture.mode === "horizontal") {
        row.classList.add("adjusting");
        try { input.setPointerCapture(event.pointerId); } catch (_error) { /* best effort */ }
      }
    }
    if (gesture.mode === "horizontal") {
      event.preventDefault();
      gesture.previewRaw = valueAt(event.clientX);
      setVisualValue(gesture.previewRaw);
    } else {
      setVisualValue(gesture.startRaw);
    }
  };
  const onPointerUp = (event) => {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    const completed = gesture;
    suppressTouchClickUntil = performance.now() + 500;
    if (completed.mode === "horizontal") {
      event.preventDefault();
      completed.previewRaw = valueAt(event.clientX);
      setVisualValue(completed.previewRaw);
      commitValue(completed.previewRaw);
    } else {
      setVisualValue(completed.startRaw);
    }
    cleanup(event.pointerId);
  };
  const onPointerCancel = (event) => {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    setVisualValue(gesture.startRaw);
    suppressTouchClickUntil = performance.now() + 500;
    cleanup(event.pointerId);
  };

  input.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || !["touch", "pen"].includes(event.pointerType)) return;
    gesture = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startRaw: initialRawValue(),
      previewRaw: initialRawValue(),
      mode: "pending",
    };
    window.addEventListener("pointermove", onPointerMove, {passive: false});
    window.addEventListener("pointerup", onPointerUp, {passive: false});
    window.addEventListener("pointercancel", onPointerCancel);
  });
  input.addEventListener("input", (event) => {
    if (!gesture) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    setVisualValue(gesture.mode === "horizontal" ? gesture.previewRaw : gesture.startRaw);
  });
  input.addEventListener("click", (event) => {
    if (performance.now() >= suppressTouchClickUntil) return;
    event.preventDefault();
    setVisualValue(initialRawValue());
  }, true);
  return () => gesture;
}

async function applySingleControl(name, value) {
  const queued = await request("/api/config/camera", {
    method: "PATCH",
    body: JSON.stringify({controls: {[name]: value}}),
  });
  await waitForCommand(queued.command_id, `设置${controlDisplayName(name)}`);
  const synchronized = await request("/api/config/camera");
  return synchronized.camera;
}

function buildControl(name, info, controls) {
  const row = document.createElement("article");
  row.className = "control-row";
  row.dataset.control = name;

  const head = document.createElement("div");
  head.className = "control-head";
  const title = document.createElement("strong");
  const displayName = controlDisplayName(name);
  title.textContent = displayName;
  const valueLabel = document.createElement("span");
  valueLabel.className = "control-value";
  valueLabel.dataset.role = "value";
  let confirmedRawValue = effectiveControlValue(name, info);
  let displayedRawValue = confirmedRawValue;
  valueLabel.textContent = formatControlValue(name, confirmedRawValue, info);
  head.append(title, valueLabel);

  const inputs = document.createElement("div");
  inputs.className = "control-inputs";
  const minus = document.createElement("button");
  minus.type = "button";
  minus.textContent = "−";
  minus.setAttribute("aria-label", `${displayName}减少`);
  const input = document.createElement("input");
  input.type = "range";
  input.dataset.controlInput = name;
  input.setAttribute("aria-label", `${displayName}滑动调整`);
  const slider = makeSliderModel(info);
  input.min = slider.minimum;
  input.max = slider.maximum;
  input.step = slider.step;
  input.value = slider.toPosition(confirmedRawValue);
  const plus = document.createElement("button");
  plus.type = "button";
  plus.textContent = "+";
  plus.setAttribute("aria-label", `${displayName}增加`);

  const diagnostic = document.createElement("div");
  diagnostic.className = "control-diagnostic";
  diagnostic.dataset.role = "diagnostic";
  const setDiagnostic = (message, failed = false) => {
    diagnostic.textContent = message;
    diagnostic.hidden = !message;
    diagnostic.classList.toggle("failed", failed);
  };
  const setVisualValue = (rawValue) => {
    displayedRawValue = rawValue;
    input.value = slider.toPosition(rawValue);
    valueLabel.textContent = formatControlValue(name, rawValue, info);
  };
  const commitValue = (rawValue) => {
    const normalized = slider.fromPosition(slider.toPosition(rawValue));
    if (normalized === confirmedRawValue && controlScheduler.desiredValue(name) === undefined) {
      setVisualValue(normalized);
      return false;
    }
    confirmedRawValue = normalized;
    displayedRawValue = normalized;
    controlFailures.delete(name);
    setVisualValue(normalized);
    setDiagnostic("正在应用");
    row.classList.add("pending");
    controlScheduler.schedule(name, normalized);
    setTag("dirtyBadge", "DIRTY", false, true);
    return true;
  };
  const changeBy = (direction) => {
    const currentPosition = slider.toPosition(displayedRawValue);
    const nextPosition = quantizeControlValue(
      currentPosition + direction * slider.step,
      slider.minimum,
      slider.maximum,
      slider.step,
    );
    if (nextPosition === currentPosition) return false;
    setVisualValue(slider.fromPosition(nextPosition));
    return true;
  };
  installRepeatingButton(minus, () => changeBy(-1), () => commitValue(displayedRawValue));
  installRepeatingButton(plus, () => changeBy(1), () => commitValue(displayedRawValue));
  const activeTouchGesture = installTouchRange(
    input,
    row,
    slider,
    () => confirmedRawValue,
    setVisualValue,
    commitValue,
  );
  input.addEventListener("input", () => {
    if (!activeTouchGesture()) commitValue(slider.fromPosition(Number(input.value)));
  });
  inputs.append(minus, input, plus);
  const failure = controlFailures.get(name);
  const desired = controlScheduler.desiredValue(name) ?? info.requested;
  const phase = controlScheduler.phase(name);
  if (failure || info.last_success === false) {
    setDiagnostic("应用失败", true);
  } else if (["DEBOUNCE", "SENT"].includes(phase)) {
    setDiagnostic("正在应用");
    row.classList.add("pending");
  } else if (info.mismatch || (
    desired !== null && desired !== undefined && info.actual !== null && info.actual !== undefined
    && Number(desired) !== Number(info.actual)
  )) {
    setDiagnostic(
      `设置值：${formatControlValue(name, desired, info)}　实际值：${formatControlValue(name, info.actual, info)}`,
    );
  } else {
    setDiagnostic("");
    row.classList.remove("pending");
  }
  row.append(head, inputs, diagnostic);
  return row;
}

function renderCurrentProfile() {
  const profile = cameraProfileState.modified
    ? "现场参数（未保存）"
    : (cameraProfileState.override_file_active ? "上次有效参数" : "启动基准");
  text("currentProfile", profile);
}

function renderCameraControls() {
  const container = $("cameraControls");
  const previousScrollTop = container.scrollTop;
  cameraControlsRenderCount += 1;
  container.replaceChildren();
  const visible = Object.entries(cameraControlsState).filter(([name, info]) => (
    controlIsWritable(name, info) && !controlDependencyHidden(name, cameraControlsState)
  ));
  const visibleNames = new Set(visible.map(([name]) => name));
  const rendered = new Set();
  CONTROL_GROUPS.forEach(([label, names]) => {
    const available = names.filter((name) => visibleNames.has(name));
    if (!available.length) return;
    const group = document.createElement("section");
    group.className = "control-group";
    const heading = document.createElement("h3");
    heading.textContent = label;
    group.append(heading);
    available.forEach((name) => {
      rendered.add(name);
      group.append(buildControl(name, cameraControlsState[name], cameraControlsState));
    });
    container.append(group);
  });
  const other = visible.filter(([name]) => !rendered.has(name));
  if (other.length) {
    const group = document.createElement("section");
    group.className = "control-group other-controls";
    const heading = document.createElement("h3");
    heading.textContent = "其他参数";
    group.append(heading);
    other.forEach(([name, info]) => group.append(buildControl(name, info, cameraControlsState)));
    container.append(group);
  }
  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "controls-empty";
    empty.textContent = "当前摄像头没有可调参数";
    container.append(empty);
  }
  container.scrollTop = Math.min(
    previousScrollTop,
    Math.max(0, container.scrollHeight - container.clientHeight),
  );
}

function replaceCameraControlState(camera, {resetScheduler = false} = {}) {
  if (resetScheduler) {
    controlScheduler.reset();
    controlFailures.clear();
  }
  const source = camera.controls || {};
  cameraControlsState = Object.fromEntries(
    Object.entries(source).map(([name, info]) => [name, {...info}]),
  );
  cameraProfileState = {
    modified: !!camera.modified,
    override_file_active: !!camera.override_file_active,
  };
  renderCameraControls();
  renderCurrentProfile();
  cameraControlsLoaded = true;
}

async function loadCameraControls(options = {}) {
  try {
    const data = await request("/api/config/camera");
    replaceCameraControlState(data.camera, options);
    return data.camera;
  } catch (error) {
    console.error("前后端摄像头参数状态同步失败", error);
    throw error;
  }
}

async function waitForCommand(commandId, label, timeoutMilliseconds = 12000) {
  const deadline = performance.now() + timeoutMilliseconds;
  while (performance.now() < deadline) {
    const data = await request("/api/status");
    renderStatus(data.status);
    const command = data.status.commands?.[commandId];
    if (command?.status === "APPLIED") return command;
    if (command?.status === "FAILED") throw new Error(command.message || `${label}失败`);
    await sleep(100);
  }
  throw new Error(`${label}等待超时`);
}

async function runCommand(path, label, {refreshCamera = false, body = {}} = {}) {
  text("operationResult", `${label}中…`);
  toast(`${label}中…`);
  try {
    const data = await request(path, {method: "POST", body: JSON.stringify(body)});
    await waitForCommand(data.command_id, label);
    if (refreshCamera) await loadCameraControls({resetScheduler: true});
    text("operationResult", `${label}成功`);
    toast(`${label}成功`);
    return true;
  } catch (error) {
    text("operationResult", `${label}失败：${error.message}`);
    toast(`${label}失败：${error.message}`);
    return false;
  }
}

function setDrawerOpen(open) {
  drawerOpen = !!open;
  $("sideDock").classList.toggle("drawer-open", drawerOpen);
  $("cameraPanel").setAttribute("aria-hidden", String(!drawerOpen));
  $("drawerHandle").setAttribute("aria-expanded", String(drawerOpen));
  $("drawerHandle").setAttribute("aria-label", drawerOpen ? "关闭摄像头参数" : "打开摄像头参数");
  $("cameraPanel").style.transform = "";
  if (drawerOpen && !cameraControlsLoaded) loadCameraControls().catch((error) => toast(error.message));
}

function beginDrawerDrag(event) {
  event.preventDefault();
  const width = $("sideDock").getBoundingClientRect().width;
  drawerDrag = {pointerId: event.pointerId, startX: event.clientX, width, wasOpen: drawerOpen, moved: false};
  $("drawerHandle").setPointerCapture(event.pointerId);
  $("sideDock").classList.add("dragging");
}

function moveDrawer(event) {
  if (!drawerDrag || event.pointerId !== drawerDrag.pointerId) return;
  event.preventDefault();
  const delta = event.clientX - drawerDrag.startX;
  if (Math.abs(delta) > 4) drawerDrag.moved = true;
  const starting = drawerDrag.wasOpen ? 0 : drawerDrag.width;
  const translate = Math.max(0, Math.min(drawerDrag.width, starting + delta));
  $("cameraPanel").style.transform = `translateX(${translate}px)`;
}

function finishDrawerDrag(event, cancelled = false) {
  if (!drawerDrag || event.pointerId !== drawerDrag.pointerId) return;
  const drag = drawerDrag;
  drawerDrag = null;
  $("sideDock").classList.remove("dragging");
  if ($("drawerHandle").hasPointerCapture(event.pointerId)) $("drawerHandle").releasePointerCapture(event.pointerId);
  if (cancelled) {
    setDrawerOpen(drag.wasOpen);
    return;
  }
  const delta = event.clientX - drag.startX;
  if (!drag.moved) {
    setDrawerOpen(!drag.wasOpen);
  } else if (drag.wasOpen) {
    setDrawerOpen(!(delta >= drag.width * 0.4));
  } else {
    setDrawerOpen(-delta >= drag.width * 0.4);
  }
}

function openMaintenance() {
  maintenanceOpenedByHold = true;
  $("maintenanceMenu").hidden = false;
}

function cancelMaintenanceHold() {
  clearInterval(maintenanceTimer);
  maintenanceTimer = null;
  $("maintenanceButton").style.setProperty("--hold-progress", "0%");
}

function beginMaintenanceHold(event) {
  event.preventDefault();
  maintenanceOpenedByHold = false;
  maintenanceStarted = performance.now();
  $("maintenanceButton").setPointerCapture(event.pointerId);
  maintenanceTimer = setInterval(() => {
    const ratio = Math.min(1, (performance.now() - maintenanceStarted) / 2000);
    $("maintenanceButton").style.setProperty("--hold-progress", `${ratio * 100}%`);
    if (ratio >= 1) {
      cancelMaintenanceHold();
      openMaintenance();
    }
  }, 40);
}

function endMaintenanceHold(event) {
  if (maintenanceTimer) {
    cancelMaintenanceHold();
    toast("请长按维护按钮2秒");
  }
  if ($("maintenanceButton").hasPointerCapture(event.pointerId)) $("maintenanceButton").releasePointerCapture(event.pointerId);
}

function confirmDanger(title, message) {
  text("confirmTitle", title);
  text("confirmMessage", message);
  $("confirmDialog").hidden = false;
  return new Promise((resolve) => { confirmResolver = resolve; });
}

function finishConfirmation(accepted) {
  $("confirmDialog").hidden = true;
  if (confirmResolver) confirmResolver(accepted);
  confirmResolver = null;
}

$("showCamera").addEventListener("click", () => setDrawerOpen(true));
$("closeCamera").addEventListener("click", () => setDrawerOpen(false));
$("drawerHandle").addEventListener("pointerdown", beginDrawerDrag);
$("drawerHandle").addEventListener("pointermove", moveDrawer);
$("drawerHandle").addEventListener("pointerup", (event) => finishDrawerDrag(event, false));
$("drawerHandle").addEventListener("pointercancel", (event) => finishDrawerDrag(event, true));
$("saveRuntime").addEventListener("click", async () => {
  await controlScheduler.flushDebounced();
  await runCommand("/api/runtime/save", "保存现场参数", {refreshCamera: true});
});
$("restoreLast").addEventListener("click", async () => {
  controlScheduler.cancelDebounced();
  await controlScheduler.waitForIdle();
  await runCommand("/api/runtime/restore-last-good", "恢复上次有效参数", {refreshCamera: true});
});
$("restoreBaseline").addEventListener("click", async () => {
  controlScheduler.cancelDebounced();
  await controlScheduler.waitForIdle();
  await runCommand("/api/runtime/restore-baseline", "恢复基准参数", {refreshCamera: true});
});
$("enterCompetition").addEventListener("click", async () => {
  if (!competitionModeEnabled && !lastCalibrationValid) {
    toast("未标定，禁止启用位置下发");
    return;
  }
  const action = competitionModeEnabled ? "停止位置下发" : "启用位置下发";
  if (!await confirmDanger(action, `确认${action}？`)) return;
  await runCommand(
    competitionModeEnabled ? "/api/competition/exit" : "/api/competition/enter",
    action,
  );
});

$("maintenanceButton").addEventListener("pointerdown", beginMaintenanceHold);
$("maintenanceButton").addEventListener("pointerup", endMaintenanceHold);
$("maintenanceButton").addEventListener("pointercancel", endMaintenanceHold);
$("closeMaintenance").addEventListener("click", () => { $("maintenanceMenu").hidden = true; });
$("reloadPage").addEventListener("click", () => window.location.reload());
$("maintenanceExitCompetition").addEventListener("click", async () => {
  if (!await confirmDanger("退出比赛模式", "确认恢复现场调试功能？")) return;
  $("maintenanceMenu").hidden = true;
  await runCommand("/api/competition/exit", "退出比赛模式");
});
$("stopRuntime").addEventListener("click", async () => {
  if (!await confirmDanger("停止视觉程序", "将安全停止摄像头、串口和Web服务，确认继续？")) return;
  try {
    await request("/api/runtime/stop", {method: "POST", body: "{}"});
    statusPolling = false;
    $("maintenanceMenu").hidden = true;
    $("stoppedOverlay").hidden = false;
  } catch (error) { toast(`停止失败：${error.message}`); }
});
$("confirmCancel").addEventListener("click", () => finishConfirmation(false));
$("confirmAccept").addEventListener("click", () => finishConfirmation(true));

window.__visionTouchTest = {
  setDrawerOpen,
  finishDrawerDrag,
  replaceCameraControlState,
  controlScheduler,
  waitForCommand,
  getCameraControlsRenderCount: () => cameraControlsRenderCount,
  positionDeliveryState,
};

pollStatus();
pollEvents();
