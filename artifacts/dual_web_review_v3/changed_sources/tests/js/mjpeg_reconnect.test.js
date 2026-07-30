"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const implementations = [
  ["比赛网页", require(path.join(ROOT, "web_competition/mjpeg_reconnect.js"))],
  ["调试网页", require(path.join(ROOT, "web_debug/static/mjpeg_reconnect.js"))],
];

class FakeImage {
  constructor() {
    this.listeners = new Map();
    this.sources = [];
  }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  removeEventListener(name, callback) {
    if (this.listeners.get(name) === callback) this.listeners.delete(name);
  }
  set src(value) { this.sources.push(value); }
  get src() { return this.sources.at(-1); }
  removeAttribute(name) { if (name === "src") this.sources.push(""); }
  emit(name) { this.listeners.get(name)?.(); }
}

function fixture(Controller) {
  const image = new FakeImage();
  const timers = [];
  const cleared = [];
  const states = [];
  let timestamp = 100;
  const controller = new Controller(image, {
    setTimeoutFn(callback, delay) {
      const timer = {callback, delay, cleared: false};
      timers.push(timer);
      return timer;
    },
    clearTimeoutFn(timer) { timer.cleared = true; cleared.push(timer); },
    now: () => timestamp++,
    documentObject: null,
    onStateChange: (state, online) => states.push([state, online]),
  });
  return {controller, image, timers, cleared, states};
}

for (const [label, module] of implementations) {
  test(`${label}首次失败按1/2/4/5秒退避且只有一个timer`, () => {
    const item = fixture(module.MjpegReconnectController);
    item.controller.start();
    assert.equal(item.image.sources.length, 1);
    assert.match(item.image.src, /^\/api\/preview\.mjpg\?ts=100$/);
    const expected = [1000, 2000, 4000, 5000, 5000];
    expected.forEach((delay, index) => {
      item.image.emit("error");
      item.image.emit("error");
      const active = item.timers.filter((timer) => !timer.cleared && timer !== null).at(-1);
      assert.equal(active.delay, delay);
      assert.equal(item.controller.retryTimer, active);
      active.callback();
      assert.equal(item.image.sources.length, index + 2);
    });
    assert.ok(Math.max(...item.timers.map((timer) => timer.delay)) <= 5000);
  });

  test(`${label}load清除timer并恢复1秒退避`, () => {
    const item = fixture(module.MjpegReconnectController);
    item.controller.start();
    item.image.emit("error");
    const firstTimer = item.controller.retryTimer;
    item.image.emit("load");
    assert.equal(item.controller.streamOnline, true);
    assert.equal(item.controller.retryIndex, 0);
    assert.equal(item.controller.retryTimer, null);
    assert.equal(firstTimer.cleared, true);
    item.image.emit("error");
    assert.equal(item.controller.retryTimer.delay, 1000);
  });

  test(`${label}start幂等且stop清理连接、timer和监听器`, () => {
    const item = fixture(module.MjpegReconnectController);
    item.controller.start();
    item.controller.start();
    assert.equal(item.image.sources.length, 1);
    item.image.emit("error");
    const timer = item.controller.retryTimer;
    item.controller.stop();
    assert.equal(timer.cleared, true);
    assert.equal(item.controller.retryTimer, null);
    assert.equal(item.image.listeners.size, 0);
    timer.callback();
    assert.equal(item.image.sources.filter(Boolean).length, 1);
  });
}
