(function (root, factory) {
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  else root.MjpegReconnectController = exported.MjpegReconnectController;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  class MjpegReconnectController {
    constructor(image, options = {}) {
      if (!image || typeof image.addEventListener !== "function") {
        throw new TypeError("image element is required");
      }
      this.image = image;
      this.url = options.url || "/api/preview.mjpg";
      this.onStateChange = options.onStateChange || (() => {});
      this.setTimeoutFn = options.setTimeoutFn || setTimeout;
      this.clearTimeoutFn = options.clearTimeoutFn || clearTimeout;
      this.now = options.now || Date.now;
      this.documentObject = options.documentObject === undefined
        ? (typeof document === "undefined" ? null : document)
        : options.documentObject;
      this.retryDelays = [1000, 2000, 4000, 5000];
      this.retryIndex = 0;
      this.retryTimer = null;
      this.running = false;
      this.streamOnline = false;
      this._loaded = () => this.markLoaded();
      this._failed = () => this.markFailed();
      this._visibilityChanged = () => {
        if (this.running && this.documentObject?.visibilityState === "visible" && !this.streamOnline) {
          this.connectNow();
        }
      };
    }

    _emit(state) {
      this.onStateChange(state, this.streamOnline);
    }

    _clearRetry() {
      if (this.retryTimer !== null) {
        this.clearTimeoutFn(this.retryTimer);
        this.retryTimer = null;
      }
    }

    start() {
      if (this.running) return;
      this.running = true;
      this.image.addEventListener("load", this._loaded);
      this.image.addEventListener("error", this._failed);
      this.documentObject?.addEventListener("visibilitychange", this._visibilityChanged);
      this._emit("connecting");
      this.connectNow();
    }

    scheduleReconnect() {
      if (!this.running || this.retryTimer !== null) return;
      const delay = this.retryDelays[Math.min(this.retryIndex, this.retryDelays.length - 1)];
      this.retryIndex += 1;
      this.retryTimer = this.setTimeoutFn(() => {
        this.retryTimer = null;
        this.connectNow();
      }, delay);
    }

    connectNow() {
      if (!this.running) return;
      this._clearRetry();
      this.streamOnline = false;
      this._emit(this.retryIndex ? "reconnecting" : "connecting");
      const separator = this.url.includes("?") ? "&" : "?";
      this.image.src = `${this.url}${separator}ts=${this.now()}`;
    }

    markLoaded() {
      if (!this.running) return;
      this._clearRetry();
      this.retryIndex = 0;
      this.streamOnline = true;
      this._emit("online");
    }

    markFailed() {
      if (!this.running) return;
      this.streamOnline = false;
      this._emit("reconnecting");
      this.scheduleReconnect();
    }

    stop() {
      if (!this.running) return;
      this.running = false;
      this.streamOnline = false;
      this._clearRetry();
      this.image.removeEventListener("load", this._loaded);
      this.image.removeEventListener("error", this._failed);
      this.documentObject?.removeEventListener("visibilitychange", this._visibilityChanged);
      this.image.removeAttribute?.("src");
      this._emit("stopped");
    }
  }

  return {MjpegReconnectController};
}));
