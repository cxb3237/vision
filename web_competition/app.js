"use strict";

const byId = (id) => document.getElementById(id);
let pollTimer = null;
let toastTimer = null;
let lastStatus = null;
let backendOnline = false;
let cameraOnline = false;
let streamOnline = false;
let streamState = "connecting";
let mediaWorkerAlive = true;

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
  return `${Math.round(bytes / 1024)} KB`;
}

function showToast(message) {
  clearTimeout(toastTimer);
  byId("toast").textContent = message;
  byId("toast").hidden = false;
  toastTimer = setTimeout(() => { byId("toast").hidden = true; }, 2800);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  const payload = await response.json().catch(() => ({ok: false, message: "响应格式错误"}));
  if (!response.ok || payload.ok === false) throw new Error(payload.message || `请求失败 ${response.status}`);
  return payload;
}

function updatePreviewNotice() {
  const notice = byId("offlineNotice");
  let title = "";
  let detail = "";
  if (!backendOnline) {
    title = "比赛后端未连接";
    detail = "正在自动重试，请确认视觉主服务仍在运行。";
  } else if (!mediaWorkerAlive) {
    title = "比赛媒体服务故障";
    detail = "视觉识别仍可运行，请检查服务日志。";
  } else if (!cameraOnline) {
    title = "摄像头离线";
    detail = "后端在线，正在等待摄像头恢复。";
  } else if (!streamOnline) {
    title = streamState === "connecting" ? "视频流正在连接" : "视频流正在重连";
    detail = "页面会自动恢复，无需手动刷新。";
  }
  notice.hidden = !title;
  byId("offlineNoticeTitle").textContent = title;
  byId("offlineNoticeDetail").textContent = detail;
}

function renderStatus(status) {
  lastStatus = status;
  backendOnline = true;
  cameraOnline = !!status.camera_online;
  mediaWorkerAlive = status.media_worker_alive !== false;
  const recording = status.recording || {};
  const finalizing = recording.state === "STOPPING";
  const recordingActive = recording.state === "STARTING" || recording.state === "RECORDING";
  byId("backendStatus").textContent = "后端已连接";
  byId("cameraStatus").textContent = cameraOnline ? "在线" : "离线";
  byId("streamStatus").textContent = streamOnline ? "视频流正常" : (streamState === "connecting" ? "正在连接" : "正在重连");
  byId("recordingStatus").textContent = finalizing
    ? "正在完成录像"
    : (recordingActive ? "正在录像" : (recording.state === "ERROR" ? "录像后端故障" : "未录像"));
  byId("recordingTimer").textContent = formatDuration(recording.duration_s);
  byId("freeSpace").textContent = formatBytes(status.storage?.free_bytes);
  byId("currentFile").textContent = recording.file_name || "尚未开始录像";
  byId("detailFile").textContent = recording.file_name || "--";
  byId("writtenFrames").textContent = String(recording.written_frames || 0);
  byId("droppedFrames").textContent = String(recording.dropped_frames || 0);
  byId("actualFps").textContent = Number(recording.actual_fps || 0).toFixed(1);
  byId("recordingOverlay").hidden = !recordingActive;
  byId("startRecording").disabled = recordingActive || finalizing || !cameraOnline || !mediaWorkerAlive;
  byId("stopRecording").disabled = !recordingActive || finalizing;
  updatePreviewNotice();
  if (finalizing) recordingFinalizer.start();
}

function renderBackendUnavailable() {
  backendOnline = false;
  cameraOnline = false;
  byId("backendStatus").textContent = "后端未连接";
  byId("cameraStatus").textContent = "未知";
  byId("recordingStatus").textContent = "状态未知";
  byId("startRecording").disabled = true;
  byId("stopRecording").disabled = true;
  updatePreviewNotice();
}

async function pollStatus() {
  clearTimeout(pollTimer);
  let interval = lastStatus?.ui?.status_poll_interval_ms || 500;
  try {
    const payload = await request("/api/status");
    renderStatus(payload.status);
    interval = payload.status.ui?.status_poll_interval_ms || interval;
  } catch (_error) {
    renderBackendUnavailable();
    interval = 1000;
  }
  pollTimer = setTimeout(pollStatus, interval);
}

function codecCanUsuallyPlayInBrowser(codec) {
  return ["avc1", "h264"].includes(String(codec || "").toLowerCase());
}

function recordingRow(item) {
  const row = document.createElement("article");
  row.className = "recording-row";
  const meta = document.createElement("div");
  meta.className = "meta";
  const name = document.createElement("strong");
  name.textContent = item.file_name;
  const detail = document.createElement("small");
  const resolution = Array.isArray(item.resolution) ? item.resolution.join("×") : "未知分辨率";
  detail.textContent = `${formatDuration(item.duration_s)} · ${resolution} · ${Number(item.fps || 0).toFixed(1)} FPS · ${item.codec || "未知编码"} · ${formatBytes(item.size_bytes)} · ${item.completed ? "完整" : "未完成"}`;
  meta.append(name, detail);
  const play = document.createElement("button");
  play.type = "button";
  play.textContent = "回放";
  play.addEventListener("click", () => {
    byId("playback").src = item.play_url;
    byId("playback").hidden = false;
    byId("playback").play().catch(() => showToast("浏览器无法直接播放，请尝试下载录像"));
  });
  const download = document.createElement("a");
  download.textContent = "下载";
  download.href = item.download_url;
  download.download = item.file_name;
  row.append(meta, play, download);
  if (!codecCanUsuallyPlayInBrowser(item.codec)) {
    const warning = document.createElement("p");
    warning.className = "codec-warning";
    warning.textContent = "当前编码可能不被部分平板浏览器直接播放，可使用下载功能检查。";
    row.append(warning);
  }
  return row;
}

async function loadRecordings() {
  const list = byId("recordingsList");
  list.textContent = "正在读取…";
  try {
    const payload = await request("/api/recordings");
    list.replaceChildren();
    if (!payload.recordings.length) {
      const empty = document.createElement("p");
      empty.className = "empty-list";
      empty.textContent = "暂无录像";
      list.append(empty);
    } else {
      payload.recordings.forEach((item) => list.append(recordingRow(item)));
    }
  } catch (error) {
    list.textContent = `录像列表读取失败：${error.message}`;
  }
}

const recordingFinalizer = new RecordingFinalizeController({
  requestStatus: () => request("/api/status"),
  onStatus: renderStatus,
  refreshRecordings: loadRecordings,
  onMessage: showToast,
  intervalMs: 300,
  timeoutMs: 15000,
});

async function setRecording(start) {
  const button = byId(start ? "startRecording" : "stopRecording");
  button.disabled = true;
  try {
    const payload = await request(`/api/recording/${start ? "start" : "stop"}`, {
      method: "POST",
      body: "{}",
    });
    if (payload.recording) renderStatus({...lastStatus, recording: payload.recording});
    if (start) showToast("录像已启动");
    else recordingFinalizer.start();
  } catch (error) {
    showToast(error.message);
  } finally {
    pollStatus();
  }
}

const streamController = new MjpegReconnectController(byId("preview"), {
  url: "/api/preview.mjpg",
  onStateChange: (state, online) => {
    streamState = state;
    streamOnline = online;
    byId("streamStatus").textContent = online ? "视频流正常" : (state === "connecting" ? "正在连接" : "正在重连");
    updatePreviewNotice();
  },
});

byId("startRecording").addEventListener("click", () => setRecording(true));
byId("stopRecording").addEventListener("click", () => setRecording(false));
byId("showRecordings").addEventListener("click", () => {
  byId("recordingsPanel").hidden = false;
  loadRecordings();
});
byId("closeRecordings").addEventListener("click", () => {
  byId("playback").pause();
  byId("recordingsPanel").hidden = true;
});
byId("refreshRecordings").addEventListener("click", loadRecordings);

function stopPageWorkers() {
  clearTimeout(pollTimer);
  clearTimeout(toastTimer);
  recordingFinalizer.stop();
  streamController.stop();
}

window.addEventListener("pagehide", stopPageWorkers, {once: true});
window.addEventListener("beforeunload", stopPageWorkers, {once: true});
window.__competitionTest = {
  updatePreviewNotice,
  streamController,
  recordingFinalizer,
  codecCanUsuallyPlayInBrowser,
};

streamController.start();
pollStatus();
