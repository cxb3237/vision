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
const vmcTxTracker = new VmcTxTracker(1500);
const controlScheduler = new ControlUpdateScheduler({
  debounceMs: parameterDebounce,
  apply: applySingleControl,
  onApplied: async (name, _value, camera) => {
    const info = camera.controls?.[name];
    if (!info) throw new Error(`服务器同步结果缺少控制项 ${name}`);
    cameraControlsState[name] = {...info};
    cameraProfileState = {
      modified: !!camera.modified,
      override_file_active: !!camera.override_file_active,
    };
    renderCameraControls();
    renderCurrentProfile();
  },
  onError: (_name, error) => {
    renderCameraControls();
    toast(error.message);
  },
});
const CONTROL_GROUPS = [
  ["自动与白平衡", ["white_balance_automatic", "white_balance_temperature"]],
  ["曝光与对焦", ["exposure_auto", "exposure_absolute", "focus_auto", "focus_absolute", "gain"]],
  ["画面调整", ["brightness", "contrast", "saturation", "hue", "gamma", "sharpness", "backlight_compensation", "power_line_frequency"]],
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

function renderStatus(status) {
  pollInterval = status.ui?.status_poll_interval_ms || pollInterval;
  parameterDebounce = status.ui?.parameter_debounce_ms ?? parameterDebounce;
  controlScheduler.setDebounceMs(parameterDebounce);
  text("fps", Number(status.fps || 0).toFixed(1));
  text("txCount", status.vmc_tx_count || 0);
  text("targetClass", status.target_class || "—");
  text("detector", status.detector || "—");
  text("confidence", status.confidence || 0);
  text("center", `${status.center_x ?? -1}, ${status.center_y ?? -1}`);
  text("mode", status.mode || "—");
  text("lastError", status.last_error || "无错误");
  text("overlayDetector", status.detector || "—");
  text("overlayTarget", status.target_class || "—");
  text("overlayState", status.state || "NONE");
  text("cDetector", status.detector || "—");
  text("cClass", status.target_class || "—");
  text("cState", status.state || "NONE");
  text("cConfidence", status.confidence || 0);
  text("cFps", Number(status.fps || 0).toFixed(1));
  text("cLinks", `${status.camera_online ? "ON" : "OFF"} / ${status.serial_online ? "ON" : "OFF"}`);
  setTag("runningBadge", status.runtime_running ? "RUNNING" : "STOPPED", !!status.runtime_running);
  setTag("cameraBadge", status.camera_online ? "CAM ONLINE" : "CAM OFFLINE", !!status.camera_online);
  setTag("serialBadge", status.serial_online ? "UART ONLINE" : "UART OFFLINE", !!status.serial_online);
  const vmcState = vmcTxTracker.update(!!status.serial_online, status.vmc_tx_count, Date.now());
  setTag(
    "vmcBadge",
    `VMC TX ${vmcState}`,
    vmcState === "ACTIVE",
    vmcState === "IDLE",
  );
  setTag("lockBadge", status.state || "NONE", status.state === "LOCKED", status.state !== "LOCKED");
  const dirty = !!status.runtime_modified || controlScheduler.hasPending();
  setTag("dirtyBadge", dirty ? "DIRTY" : "CLEAN", !dirty, dirty);
  setTag("competitionBadge", status.competition_mode ? "COMPETITION" : "DEBUG", true, !!status.competition_mode);
  $("normalDock").hidden = !!status.competition_mode;
  $("competitionDock").hidden = !status.competition_mode;
  document.querySelectorAll("[data-detector]").forEach((button) => {
    button.classList.toggle("active", button.dataset.detector === status.detector);
  });
}

async function pollStatus() {
  if (!statusPolling) return;
  try {
    const data = await request("/api/status");
    renderStatus(data.status);
  } catch (error) {
    text("lastError", error.message);
  } finally {
    if (statusPolling) setTimeout(pollStatus, pollInterval);
  }
}

function controlDisabledReason(name, controls) {
  if (name === "white_balance_temperature" && controls.white_balance_automatic?.actual !== 0) {
    return "自动白平衡开启，手动色温已禁用";
  }
  if (name === "exposure_absolute" && ![null, undefined, 1].includes(controls.exposure_auto?.actual)) {
    return "自动曝光开启，手动曝光已禁用";
  }
  if (name === "focus_absolute" && ![null, undefined, 0].includes(controls.focus_auto?.actual)) {
    return "自动对焦开启，手动焦距已禁用";
  }
  return "";
}

function queueControlUpdate(name, value, input, requestedLabel) {
  input.value = String(value);
  requestedLabel.textContent = `requested: ${value}`;
  controlScheduler.schedule(name, value);
  setTag("dirtyBadge", "DIRTY", false, true);
}

async function applySingleControl(name, value) {
  const queued = await request("/api/config/camera", {
    method: "PATCH",
    body: JSON.stringify({controls: {[name]: value}}),
  });
  await waitForCommand(queued.command_id, `设置 ${name}`);
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
  title.textContent = name;
  const requested = document.createElement("span");
  requested.className = "requested";
  requested.dataset.role = "requested";
  const desiredValue = controlScheduler.desiredValue(name);
  requested.textContent = `requested: ${desiredValue ?? info.requested ?? "—"}`;
  head.append(title, requested);

  const inputs = document.createElement("div");
  inputs.className = "control-inputs";
  const minus = document.createElement("button");
  minus.type = "button";
  minus.textContent = "−";
  minus.setAttribute("aria-label", `${name}减少`);
  const input = document.createElement("input");
  input.type = "range";
  input.dataset.controlInput = name;
  input.min = info.minimum ?? 0;
  input.max = info.maximum ?? 1;
  input.step = info.step || 1;
  input.value = desiredValue ?? info.requested ?? info.actual ?? input.min;
  const plus = document.createElement("button");
  plus.type = "button";
  plus.textContent = "+";
  plus.setAttribute("aria-label", `${name}增加`);
  const disabledReason = controlDisabledReason(name, controls);
  const disabled = !info.supported || !!disabledReason;
  input.disabled = minus.disabled = plus.disabled = disabled;
  const changeBy = (direction) => {
    const next = Math.min(
      Number(input.max),
      Math.max(Number(input.min), Number(input.value) + direction * Number(input.step || 1)),
    );
    queueControlUpdate(name, next, input, requested);
  };
  minus.addEventListener("click", () => changeBy(-1));
  plus.addEventListener("click", () => changeBy(1));
  input.addEventListener("input", () => queueControlUpdate(name, Number(input.value), input, requested));
  inputs.append(minus, input, plus);

  const foot = document.createElement("div");
  foot.className = "control-foot";
  const actual = document.createElement("span");
  actual.dataset.role = "actual";
  actual.textContent = `actual: ${info.actual ?? "—"} · ${info.minimum ?? "—"}..${info.maximum ?? "—"}`;
  const badges = document.createElement("span");
  badges.className = "control-badges";
  const supportBadge = document.createElement("span");
  supportBadge.className = `mini-badge${info.supported ? "" : " bad"}`;
  supportBadge.textContent = info.supported ? "SUPPORTED" : "UNSUPPORTED";
  badges.append(supportBadge);
  if (info.mismatch) {
    const mismatch = document.createElement("span");
    mismatch.className = "mini-badge bad";
    mismatch.textContent = "MISMATCH";
    badges.append(mismatch);
  }
  if (info.last_success === false) {
    const failed = document.createElement("span");
    failed.className = "mini-badge bad";
    failed.textContent = "FAILED";
    badges.append(failed);
  }
  foot.append(actual, badges);
  row.append(head, inputs, foot);
  const errorText = disabledReason || info.error;
  if (errorText) {
    const error = document.createElement("div");
    error.className = "control-error";
    error.textContent = errorText;
    row.append(error);
  }
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
  container.replaceChildren();
  const rendered = new Set();
  CONTROL_GROUPS.forEach(([label, names]) => {
    const available = names.filter((name) => cameraControlsState[name]);
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
  Object.entries(cameraControlsState).forEach(([name, info]) => {
    if (!rendered.has(name)) container.append(buildControl(name, info, cameraControlsState));
  });
}

function replaceCameraControlState(camera, {resetScheduler = false} = {}) {
  if (resetScheduler) controlScheduler.reset();
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

document.querySelectorAll("[data-detector]").forEach((button) => {
  button.addEventListener("click", () => runCommand(
    "/api/detector/select",
    `切换到${button.textContent}`,
    {body: {detector: button.dataset.detector}},
  ));
});
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
$("enterCompetition").addEventListener("click", () => runCommand("/api/competition/enter", "进入比赛模式"));

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
$("exitKiosk").addEventListener("click", async () => {
  if (!await confirmDanger("退出全屏界面", "确认关闭当前kiosk浏览器？视觉程序将继续运行。")) return;
  try {
    await request("/api/kiosk/exit", {method: "POST", body: "{}"});
    toast("正在退出全屏界面");
  } catch (error) { toast(`退出失败：${error.message}`); }
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
  vmcTxTracker,
  waitForCommand,
};

pollStatus();
