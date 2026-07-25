"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  ControlUpdateScheduler,
  VmcTxTracker,
} = require("../../touch_ui_web/control_scheduler.js");

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitUntil(predicate, timeout = 1000) {
  const deadline = Date.now() + timeout;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("等待测试条件超时");
    await delay(2);
  }
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

test("不同参数的debounce和已发送请求互不取消", async () => {
  const backend = {brightness: 0, contrast: 0};
  const brightnessGate = deferred();
  const calls = [];
  const scheduler = new ControlUpdateScheduler({
    debounceMs: 5,
    apply: async (name, value) => {
      calls.push([name, value]);
      if (name === "brightness") await brightnessGate.promise;
      backend[name] = value;
      return {...backend};
    },
  });

  scheduler.schedule("brightness", 20);
  await waitUntil(() => calls.some(([name]) => name === "brightness"));
  assert.equal(scheduler.phase("brightness"), "SENT");
  scheduler.schedule("contrast", 16);
  await waitUntil(() => calls.some(([name]) => name === "contrast"));
  brightnessGate.resolve();
  await scheduler.waitForIdle();

  assert.deepEqual(calls, [["brightness", 20], ["contrast", 16]]);
  assert.deepEqual(backend, {brightness: 20, contrast: 16});
});

test("同一参数快速变化只发送最新值", async () => {
  const calls = [];
  const backend = {brightness: 0};
  const scheduler = new ControlUpdateScheduler({
    debounceMs: 50,
    apply: async (name, value) => {
      calls.push([name, value]);
      backend[name] = value;
      return {...backend};
    },
  });
  scheduler.schedule("brightness", 1);
  scheduler.schedule("brightness", 2);
  scheduler.schedule("brightness", 3);
  assert.equal(scheduler.phase("brightness"), "DEBOUNCE");
  await scheduler.flushDebounced();
  assert.deepEqual(calls, [["brightness", 3]]);
  assert.equal(backend.brightness, 3);
});

test("已发送旧值的延迟响应不能覆盖更新目标", async () => {
  const firstGate = deferred();
  const calls = [];
  const rendered = [];
  const scheduler = new ControlUpdateScheduler({
    debounceMs: 5,
    apply: async (_name, value) => {
      calls.push(value);
      if (value === 1) await firstGate.promise;
      return {actual: value};
    },
    onApplied: async (_name, value) => rendered.push(value),
  });
  scheduler.schedule("brightness", 1);
  await waitUntil(() => calls.length === 1);
  scheduler.schedule("brightness", 2);
  scheduler.schedule("brightness", 3);
  firstGate.resolve();
  await waitUntil(() => calls.length === 2);
  await scheduler.waitForIdle();
  assert.deepEqual(calls, [1, 3]);
  assert.deepEqual(rendered, [3]);
});

test("恢复基准会取消尚未发送的编辑", async () => {
  const calls = [];
  const backend = {brightness: 0};
  const ui = {brightness: 20};
  const scheduler = new ControlUpdateScheduler({
    debounceMs: 100,
    apply: async (name, value) => {
      calls.push([name, value]);
      backend[name] = value;
    },
  });
  scheduler.schedule("brightness", 20);
  scheduler.cancelDebounced();
  await scheduler.waitForIdle();
  backend.brightness = 0;
  ui.brightness = backend.brightness;
  scheduler.reset();
  assert.deepEqual(calls, []);
  assert.equal(backend.brightness, 0);
  assert.equal(ui.brightness, 0);
  assert.equal(scheduler.hasPending(), false);
});

test("已完成等待同步阶段可区分", async () => {
  const syncGate = deferred();
  const scheduler = new ControlUpdateScheduler({
    debounceMs: 0,
    apply: async () => ({actual: 9}),
    onApplied: async () => { await syncGate.promise; },
  });
  scheduler.schedule("gain", 9);
  await waitUntil(() => scheduler.phase("gain") === "COMPLETED");
  syncGate.resolve();
  await scheduler.waitForIdle();
  assert.equal(scheduler.phase("gain"), "IDLE");
});

test("VMC发送状态按在线和最近增长时间判断", () => {
  const tracker = new VmcTxTracker(1500);
  assert.equal(tracker.update(false, 10, 1000), "OFFLINE");
  assert.equal(tracker.update(true, 10, 1100), "IDLE");
  assert.equal(tracker.update(true, 11, 1200), "ACTIVE");
  assert.equal(tracker.update(true, 11, 2699), "ACTIVE");
  assert.equal(tracker.update(true, 11, 2701), "IDLE");
  assert.equal(tracker.update(true, 2, 2800), "IDLE");
  assert.equal(tracker.update(true, 3, 2900), "ACTIVE");
  assert.equal(tracker.update(false, 3, 3000), "OFFLINE");
});
