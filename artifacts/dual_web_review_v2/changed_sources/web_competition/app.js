"use strict";

const byId = (id) => document.getElementById(id);
let pollTimer = null;
let toastTimer = null;
let lastStatus = null;

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
  toastTimer = setTimeout(() => { byId("toast").hidden = true; }, 2400);
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

function renderStatus(status) {
  lastStatus = status;
  const recording = status.recording || {};
  const active = !!recording.active;
  byId("backendStatus").textContent = "服务已连接";
  byId("cameraStatus").textContent = status.camera_online ? "在线" : "离线";
  byId("recordingStatus").textContent = active ? "录像中" : (recording.error ? "录像异常" : "未录像");
  byId("recordingTimer").textContent = formatDuration(recording.duration_s);
  byId("freeSpace").textContent = formatBytes(status.storage?.free_bytes);
  byId("currentFile").textContent = recording.file_name || "尚未开始录像";
  byId("detailFile").textContent = recording.file_name || "--";
  byId("writtenFrames").textContent = String(recording.written_frames || 0);
  byId("droppedFrames").textContent = String(recording.dropped_frames || 0);
  byId("actualFps").textContent = Number(recording.actual_fps || 0).toFixed(1);
  byId("recordingOverlay").hidden = !active;
  byId("startRecording").disabled = active || !status.camera_online;
  byId("stopRecording").disabled = !active;
  byId("offlineNotice").hidden = true;
}

function renderOffline() {
  byId("backendStatus").textContent = "后端不可用";
  byId("cameraStatus").textContent = "未知";
  byId("offlineNotice").hidden = false;
  byId("startRecording").disabled = true;
  byId("stopRecording").disabled = true;
}

async function pollStatus() {
  clearTimeout(pollTimer);
  let interval = lastStatus?.ui?.status_poll_interval_ms || 500;
  try {
    const payload = await request("/api/status");
    renderStatus(payload.status);
    interval = payload.status.ui?.status_poll_interval_ms || interval;
  } catch (_) {
    renderOffline();
    interval = 1000;
  }
  pollTimer = setTimeout(pollStatus, interval);
}

async function setRecording(start) {
  const button = byId(start ? "startRecording" : "stopRecording");
  button.disabled = true;
  try {
    const payload = await request(`/api/recording/${start ? "start" : "stop"}`, {
      method: "POST",
      body: "{}",
    });
    showToast(start ? "录像已启动" : "录像正在停止");
    if (payload.recording) renderStatus({...lastStatus, recording: payload.recording});
    if (!start) setTimeout(loadRecordings, 400);
  } catch (error) {
    showToast(error.message);
  } finally {
    pollStatus();
  }
}

function recordingRow(item) {
  const row = document.createElement("article");
  row.className = "recording-row";
  const meta = document.createElement("div");
  meta.className = "meta";
  const name = document.createElement("strong");
  name.textContent = item.file_name;
  const detail = document.createElement("small");
  detail.textContent = `${formatDuration(item.duration_s)} · ${formatBytes(item.size_bytes)} · 丢帧 ${item.dropped_frames || 0}`;
  meta.append(name, detail);
  const play = document.createElement("button");
  play.type = "button";
  play.textContent = "回放";
  play.addEventListener("click", () => {
    byId("playback").src = item.play_url;
    byId("playback").hidden = false;
    byId("playback").play().catch(() => {});
  });
  const download = document.createElement("a");
  download.textContent = "下载";
  download.href = item.download_url;
  download.download = item.file_name;
  row.append(meta, play, download);
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
byId("preview").addEventListener("error", () => { byId("offlineNotice").hidden = false; });

pollStatus();
