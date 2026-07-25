"use strict";

const $ = (id) => document.getElementById(id);
let cameraControlsLoaded = false;
let debounceTimers = new Map();
let exitTimer = null;
let exitStarted = 0;
let pollInterval = 250;
let parameterDebounce = 150;
let exitHold = 3000;

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

function text(id, value) { $(id).textContent = value; }

function renderStatus(s) {
  pollInterval = s.ui?.status_poll_interval_ms || pollInterval;
  parameterDebounce = s.ui?.parameter_debounce_ms ?? parameterDebounce;
  exitHold = s.ui?.exit_competition_hold_ms || exitHold;
  text("runningBadge", s.runtime_running ? "RUNNING" : "STOPPED");
  $("runningBadge").classList.toggle("offline", !s.runtime_running);
  text("cameraOnline", s.camera_online ? "ON" : "OFF");
  text("serialOnline", s.serial_online ? "ON" : "OFF");
  text("fps", Number(s.fps || 0).toFixed(1));
  text("txCount", s.vmc_tx_count || 0);
  text("targetClass", s.target_class || "—");
  text("detector", s.detector || "—");
  text("state", s.state || "NONE");
  text("confidence", s.confidence || 0);
  text("center", `${s.center_x ?? -1}, ${s.center_y ?? -1}`);
  text("error", `${s.error_x || 0}, ${s.error_y || 0}`);
  text("mode", s.mode || "—");
  text("modified", s.runtime_modified ? "已修改" : "基准/已保存");
  text("lastError", s.last_error || "无错误");
  text("cDetector", s.detector || "—");
  text("cClass", s.target_class || "—");
  text("cState", s.state || "NONE");
  text("cConfidence", s.confidence || 0);
  text("cFps", Number(s.fps || 0).toFixed(1));
  text("cLinks", `${s.camera_online ? "ON" : "OFF"} / ${s.serial_online ? "ON" : "OFF"}`);
  $("debugView").hidden = !!s.competition_mode;
  $("competitionView").hidden = !s.competition_mode;
  document.querySelectorAll("[data-detector]").forEach((button) => {
    button.classList.toggle("active", button.dataset.detector === s.detector);
  });
}

async function pollStatus() {
  try {
    const data = await request("/api/status");
    renderStatus(data.status);
  } catch (error) {
    text("lastError", error.message);
  } finally {
    setTimeout(pollStatus, pollInterval);
  }
}

function buildControl(name, info) {
  const row = document.createElement("div");
  row.className = "control-row";
  const title = document.createElement("div");
  const outcome = info.last_success === null || info.last_success === undefined ? "未设置" : (info.last_success ? "成功" : "失败");
  title.innerHTML = `<strong>${name}</strong><div class="control-meta">requested=${info.requested ?? "—"} actual=${info.actual ?? "—"}<br>range=${info.minimum ?? "—"}..${info.maximum ?? "—"} step=${info.step ?? "—"} · ${info.supported ? "支持" : "不支持"} · ${outcome}<br>${info.error || (info.mismatch ? "实际值与请求值不同" : "")}</div>`;
  const input = document.createElement("input");
  input.type = "range";
  input.min = info.minimum ?? 0;
  input.max = info.maximum ?? 1;
  input.step = info.step || 1;
  input.value = info.actual ?? info.requested ?? input.min;
  input.disabled = !info.supported || !!info.uiDisabled;
  const value = document.createElement("div");
  value.className = "control-value";
  value.textContent = input.value;
  input.addEventListener("input", () => {
    value.textContent = input.value;
    clearTimeout(debounceTimers.get(name));
    debounceTimers.set(name, setTimeout(() => updateControl(name, Number(input.value)), parameterDebounce));
  });
  row.append(title, input, value);
  return row;
}

async function loadCameraControls() {
  try {
    const data = await request("/api/config/camera");
    const container = $("cameraControls");
    container.replaceChildren();
    const controls = data.camera.controls || {};
    if (controls.white_balance_automatic?.actual !== 0 && controls.white_balance_temperature) {
      controls.white_balance_temperature = {...controls.white_balance_temperature, uiDisabled: true, error: "自动白平衡开启，手动色温已禁用"};
    }
    if (![null, undefined, 1].includes(controls.exposure_auto?.actual) && controls.exposure_absolute) {
      controls.exposure_absolute = {...controls.exposure_absolute, uiDisabled: true, error: "自动曝光开启，手动曝光已禁用"};
    }
    Object.entries(controls).forEach(([name, info]) => container.append(buildControl(name, info)));
    cameraControlsLoaded = true;
  } catch (error) { toast(error.message); }
}

async function updateControl(name, value) {
  try {
    const data = await request("/api/config/camera", {method: "PATCH", body: JSON.stringify({controls: {[name]: value}})});
    toast(`命令已排队 ${data.command_id.slice(0, 8)}`);
    setTimeout(loadCameraControls, 400);
  } catch (error) { toast(error.message); }
}

async function post(path, body = {}) {
  try {
    const data = await request(path, {method: "POST", body: JSON.stringify(body)});
    toast(`命令已排队 ${data.command_id.slice(0, 8)}`);
  } catch (error) { toast(error.message); }
}

document.querySelectorAll("[data-detector]").forEach((button) => button.addEventListener("click", () => post("/api/detector/select", {detector: button.dataset.detector})));
$("showCamera").addEventListener("click", () => { $("cameraPanel").classList.add("open"); if (!cameraControlsLoaded) loadCameraControls(); });
$("closeCamera").addEventListener("click", () => $("cameraPanel").classList.remove("open"));
$("saveRuntime").addEventListener("click", () => post("/api/runtime/save"));
$("restoreLast").addEventListener("click", () => post("/api/runtime/restore-last-good"));
$("restoreBaseline").addEventListener("click", () => post("/api/runtime/restore-baseline"));
$("enterCompetition").addEventListener("click", () => post("/api/competition/enter"));

function startExitHold(event) {
  event.preventDefault();
  exitStarted = performance.now();
  exitTimer = setInterval(() => {
    const elapsed = performance.now() - exitStarted;
    $("holdProgress").style.width = `${Math.min(100, elapsed / exitHold * 100)}%`;
    if (elapsed >= exitHold) {
      cancelExitHold();
      post("/api/competition/exit");
    }
  }, 50);
}
function cancelExitHold() { clearInterval(exitTimer); exitTimer = null; $("holdProgress").style.width = "0"; }
$("exitCompetition").addEventListener("pointerdown", startExitHold);
$("exitCompetition").addEventListener("pointerup", cancelExitHold);
$("exitCompetition").addEventListener("pointercancel", cancelExitHold);
$("exitCompetition").addEventListener("pointerleave", cancelExitHold);

pollStatus();
