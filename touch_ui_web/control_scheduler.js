(function (root, factory) {
  "use strict";
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  Object.assign(root, exported);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CONTROL_LABELS = Object.freeze({
    brightness: "亮度",
    contrast: "对比度",
    saturation: "饱和度",
    hue: "色调",
    sharpness: "锐度",
    gamma: "伽马",
    gain: "增益",
    backlight_compensation: "逆光补偿",
    white_balance_automatic: "自动白平衡",
    white_balance_temperature: "白平衡色温",
    exposure_auto: "自动曝光",
    exposure_absolute: "曝光时间",
    power_line_frequency: "抗频闪频率",
    focus_auto: "自动对焦",
    focus_absolute: "对焦位置",
  });
  const WRITABLE_CONTROL_KEYS = new Set(Object.keys(CONTROL_LABELS));
  const VALID_CONTROL_TYPES = new Set(["int", "integer", "integer64", "bool", "boolean", "menu"]);
  const TOKEN_LABELS = Object.freeze({
    auto: "自动", automatic: "自动", balance: "平衡", compensation: "补偿",
    temperature: "色温", absolute: "数值", frequency: "频率", focus: "对焦",
    exposure: "曝光", white: "白", backlight: "逆光", power: "电源", line: "线路",
  });

  function normalizedChoices(info = {}) {
    const source = info.choices;
    const choices = [];
    if (Array.isArray(source)) {
      source.forEach((item) => {
        if (item && typeof item === "object" && Number.isFinite(Number(item.value))) {
          choices.push({value: Number(item.value), label: String(item.label ?? item.name ?? item.value)});
        }
      });
    } else if (source && typeof source === "object") {
      Object.entries(source).forEach(([value, label]) => {
        if (Number.isFinite(Number(value))) choices.push({value: Number(value), label: String(label)});
      });
    }
    if (!choices.length && ["bool", "boolean"].includes(String(info.type || "").toLowerCase())) {
      choices.push({value: 0, label: "Off"}, {value: 1, label: "On"});
    }
    const unique = new Map();
    choices.forEach((choice) => unique.set(choice.value, choice));
    return [...unique.values()].sort((first, second) => first.value - second.value);
  }

  function controlDisplayName(name) {
    if (CONTROL_LABELS[name]) return CONTROL_LABELS[name];
    const tokens = String(name).split("_");
    if (tokens.length && tokens.every((token) => TOKEN_LABELS[token])) {
      return tokens.map((token) => TOKEN_LABELS[token]).join("");
    }
    return "其他参数";
  }

  function translateChoiceLabel(label, value) {
    const original = String(label ?? "").trim();
    const normalized = original.toLowerCase().replace(/[_-]+/g, " ").replace(/\s+/g, " ");
    const exact = {
      true: "开", false: "关", on: "开", off: "关", enabled: "开", disabled: "关闭",
      auto: "自动", automatic: "自动", manual: "手动", "50hz": "50赫兹", "50 hz": "50赫兹",
      "60hz": "60赫兹", "60 hz": "60赫兹", "manual mode": "手动",
      "auto mode": "自动", "automatic mode": "自动", "aperture priority mode": "光圈优先",
      "shutter priority mode": "快门优先",
    };
    if (exact[normalized]) return exact[normalized];
    if (/^50\s*hz\b/.test(normalized)) return "50赫兹";
    if (/^60\s*hz\b/.test(normalized)) return "60赫兹";
    if (/manual/.test(normalized)) return "手动";
    if (/aperture/.test(normalized)) return "光圈优先";
    if (/shutter/.test(normalized)) return "快门优先";
    if (/auto/.test(normalized)) return "自动";
    if (/^[\u3400-\u9fff]/.test(original)) return original;
    return `选项${value}`;
  }

  function formatControlValue(name, value, info = {}) {
    if (value === null || value === undefined || value === "") return "—";
    const choices = normalizedChoices(info);
    const choice = choices.find((item) => item.value === Number(value));
    if (choice) return translateChoiceLabel(choice.label, choice.value);
    if (["white_balance_automatic", "focus_auto"].includes(name)) {
      return Number(value) === 0 ? "关" : "开";
    }
    return String(value);
  }

  function controlIsWritable(name, info = {}) {
    if (info.supported !== true || info.read_only === true || info.writable === false) return false;
    if (!WRITABLE_CONTROL_KEYS.has(name) && info.setter_supported !== true) return false;
    const type = String(info.type || "int").toLowerCase();
    if (!VALID_CONTROL_TYPES.has(type)) return false;
    const minimum = Number(info.minimum);
    const maximum = Number(info.maximum);
    const step = Number(info.step);
    if (!Number.isFinite(minimum) || !Number.isFinite(maximum) || minimum >= maximum) return false;
    if (!Number.isFinite(step) || step <= 0) return false;
    if (type === "menu" && normalizedChoices(info).length < 2) return false;
    return true;
  }

  function automaticModeEnabled(name, value, info = {}) {
    const choice = normalizedChoices(info).find((item) => item.value === Number(value));
    const semantics = String(choice?.label ?? "").toLowerCase();
    if (/manual|disabled|\boff\b|false/.test(semantics)) return false;
    if (/auto|automatic|aperture|shutter|continuous|\bon\b|true/.test(semantics)) return true;
    if (name === "exposure_auto") return Number(value) !== 1;
    return Number(value) !== 0;
  }

  function classifyPointerGesture(deltaX, deltaY) {
    const horizontal = Math.abs(Number(deltaX) || 0);
    const vertical = Math.abs(Number(deltaY) || 0);
    if (vertical >= 10 && vertical > horizontal * 1.25) return "vertical";
    if (horizontal >= 12 && horizontal > vertical * 1.25) return "horizontal";
    return "pending";
  }

  function decimalPlaces(value) {
    const text = String(value);
    if (/e-/i.test(text)) return Number(text.split(/e-/i)[1]) || 0;
    return (text.split(".")[1] || "").length;
  }

  function quantizeControlValue(value, minimum, maximum, step) {
    const low = Number(minimum);
    const high = Number(maximum);
    const increment = Number(step);
    if (![value, low, high, increment].every((item) => Number.isFinite(Number(item))) || increment <= 0) {
      throw new TypeError("控制范围和step必须是有效数字");
    }
    const digits = Math.min(8, Math.max(decimalPlaces(low), decimalPlaces(high), decimalPlaces(increment)));
    const scale = 10 ** digits;
    const lowUnits = Math.round(low * scale);
    const highUnits = Math.round(high * scale);
    const stepUnits = Math.max(1, Math.round(increment * scale));
    const valueUnits = Math.round(Number(value) * scale);
    const snapped = lowUnits + Math.round((valueUnits - lowUnits) / stepUnits) * stepUnits;
    const clamped = Math.min(highUnits, Math.max(lowUnits, snapped));
    return Number((clamped / scale).toFixed(digits));
  }

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

  return {
    CONTROL_LABELS,
    ControlUpdateScheduler,
    VmcTxTracker,
    automaticModeEnabled,
    classifyPointerGesture,
    controlDisplayName,
    controlIsWritable,
    formatControlValue,
    normalizedChoices,
    quantizeControlValue,
    translateChoiceLabel,
  };
});
