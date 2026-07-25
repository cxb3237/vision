(function (root, factory) {
  "use strict";
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  root.ControlUpdateScheduler = exported.ControlUpdateScheduler;
  root.VmcTxTracker = exported.VmcTxTracker;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  class ControlUpdateScheduler {
    constructor({debounceMs = 150, apply, onApplied = async () => {}, onError = () => {}}) {
      if (typeof apply !== "function") throw new TypeError("apply必须为函数");
      this.debounceMs = debounceMs;
      this.apply = apply;
      this.onApplied = onApplied;
      this.onError = onError;
      this.desired = new Map();
      this.timers = new Map();
      this.active = new Map();
      this.phases = new Map();
    }

    setDebounceMs(milliseconds) {
      this.debounceMs = Math.max(0, Number(milliseconds) || 0);
    }

    schedule(name, value) {
      const previous = this.desired.get(name);
      const version = (previous?.version || 0) + 1;
      this.desired.set(name, {value, version});
      this.phases.set(name, "DEBOUNCE");
      clearTimeout(this.timers.get(name));
      this.timers.set(name, setTimeout(() => {
        this.timers.delete(name);
        this._dispatch(name, version);
      }, this.debounceMs));
      return version;
    }

    desiredValue(name) {
      return this.desired.get(name)?.value;
    }

    desiredEntries() {
      return [...this.desired.entries()].map(([name, item]) => [name, item.value]);
    }

    phase(name) {
      return this.phases.get(name) || "IDLE";
    }

    hasPending() {
      return this.timers.size > 0 || this.active.size > 0 || this.desired.size > 0;
    }

    async _dispatch(name, version) {
      clearTimeout(this.timers.get(name));
      this.timers.delete(name);
      const desired = this.desired.get(name);
      if (!desired || desired.version !== version) return;
      if (this.active.has(name)) return;

      const activeRecord = {value: desired.value, version};
      this.phases.set(name, "SENT");
      const promise = Promise.resolve().then(async () => {
        try {
          const result = await this.apply(name, activeRecord.value, version);
          const latest = this.desired.get(name);
          if (latest?.version === version) {
            this.phases.set(name, "COMPLETED");
            await this.onApplied(name, activeRecord.value, result, version);
            if (this.desired.get(name)?.version === version) {
              this.desired.delete(name);
              this.phases.delete(name);
            }
          }
        } catch (error) {
          if (this.desired.get(name)?.version === version) {
            this.desired.delete(name);
            this.phases.delete(name);
          }
          this.onError(name, error, version);
        } finally {
          this.active.delete(name);
          const latest = this.desired.get(name);
          if (latest && latest.version !== version) {
            this._dispatch(name, latest.version);
          }
        }
      });
      this.active.set(name, {...activeRecord, promise});
      await promise;
    }

    async flushDebounced() {
      const waiting = [...this.timers.keys()];
      for (const name of waiting) {
        clearTimeout(this.timers.get(name));
        this.timers.delete(name);
        const desired = this.desired.get(name);
        if (desired) this._dispatch(name, desired.version);
      }
      await this.waitForIdle();
    }

    cancelDebounced() {
      for (const name of [...this.timers.keys()]) {
        clearTimeout(this.timers.get(name));
        this.timers.delete(name);
      }
      for (const [name, desired] of [...this.desired.entries()]) {
        const active = this.active.get(name);
        if (active) {
          if (desired.version !== active.version) {
            this.desired.set(name, {value: active.value, version: active.version});
          }
          this.phases.set(name, "SENT");
        } else if (this.phases.get(name) === "DEBOUNCE") {
          this.desired.delete(name);
          this.phases.delete(name);
        }
      }
    }

    async waitForIdle() {
      while (this.active.size > 0) {
        await Promise.allSettled([...this.active.values()].map((item) => item.promise));
      }
    }

    reset() {
      this.cancelDebounced();
      if (this.active.size > 0) throw new Error("仍有已发送的参数请求，不能重置调度器");
      this.desired.clear();
      this.phases.clear();
    }
  }

  class VmcTxTracker {
    constructor(activeWindowMs = 1500) {
      this.activeWindowMs = activeWindowMs;
      this.lastCount = null;
      this.lastIncreaseAt = null;
    }

    update(serialOnline, rawCount, now = Date.now()) {
      const count = Number(rawCount || 0);
      if (!serialOnline) {
        this.lastCount = count;
        this.lastIncreaseAt = null;
        return "OFFLINE";
      }
      if (this.lastCount === null || count < this.lastCount) {
        this.lastCount = count;
        this.lastIncreaseAt = null;
        return "IDLE";
      }
      if (count > this.lastCount) {
        this.lastIncreaseAt = now;
      }
      this.lastCount = count;
      return this.lastIncreaseAt !== null && now - this.lastIncreaseAt <= this.activeWindowMs
        ? "ACTIVE"
        : "IDLE";
    }
  }

  return {ControlUpdateScheduler, VmcTxTracker};
});
