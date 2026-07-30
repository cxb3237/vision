(function (root, factory) {
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  else root.RecordingFinalizeController = exported.RecordingFinalizeController;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  class RecordingFinalizeController {
    constructor(options) {
      this.requestStatus = options.requestStatus;
      this.onStatus = options.onStatus || (() => {});
      this.refreshRecordings = options.refreshRecordings;
      this.onMessage = options.onMessage || (() => {});
      this.intervalMs = options.intervalMs || 300;
      this.timeoutMs = options.timeoutMs || 15000;
      this.setTimeoutFn = options.setTimeoutFn || setTimeout;
      this.clearTimeoutFn = options.clearTimeoutFn || clearTimeout;
      this.now = options.now || Date.now;
      this.timer = null;
      this.running = false;
      this.deadline = 0;
    }

    start() {
      if (this.running) return false;
      this.running = true;
      this.deadline = this.now() + this.timeoutMs;
      this.onMessage("正在完成录像");
      this._poll();
      return true;
    }

    _schedule() {
      if (!this.running || this.timer !== null) return;
      this.timer = this.setTimeoutFn(() => {
        this.timer = null;
        this._poll();
      }, this.intervalMs);
    }

    async _poll() {
      if (!this.running) return;
      try {
        const payload = await this.requestStatus();
        const status = payload.status || payload;
        this.onStatus(status);
        const recording = status.recording || {};
        if (recording.state === "IDLE") {
          this.stop();
          await this.refreshRecordings();
          this.onMessage("录像已完成");
          return;
        }
        if (recording.state === "ERROR") {
          this.stop();
          await this.refreshRecordings();
          this.onMessage(`录像完成异常：${recording.error || "未知错误"}`);
          return;
        }
      } catch (_error) {
        // The normal status poll owns the backend-offline display. Keep waiting.
      }
      if (this.now() >= this.deadline) {
        this.stop();
        this.onMessage("等待录像完成超时，请稍后刷新录像列表");
        return;
      }
      this._schedule();
    }

    stop() {
      this.running = false;
      if (this.timer !== null) {
        this.clearTimeoutFn(this.timer);
        this.timer = null;
      }
    }
  }

  return {RecordingFinalizeController};
}));
