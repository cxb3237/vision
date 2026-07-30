"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const {RecordingFinalizeController} = require(
  path.resolve(__dirname, "../../web_competition/recording_finalize.js"),
);

const flush = () => new Promise((resolve) => setImmediate(resolve));

function fixture(states, options = {}) {
  const timers = [];
  const messages = [];
  const rendered = [];
  let refreshes = 0;
  let now = 0;
  const controller = new RecordingFinalizeController({
    requestStatus: async () => ({status: {recording: states.shift()}}),
    onStatus: (status) => rendered.push(status.recording.state),
    refreshRecordings: async () => { refreshes += 1; },
    onMessage: (message) => messages.push(message),
    intervalMs: 300,
    timeoutMs: 15000,
    setTimeoutFn(callback, delay) {
      const timer = {callback, delay, cleared: false};
      timers.push(timer);
      return timer;
    },
    clearTimeoutFn: (timer) => { timer.cleared = true; },
    now: () => options.now?.() ?? now,
  });
  return {controller, timers, messages, rendered, get refreshes() { return refreshes; }, setNow: (value) => { now = value; }};
}

test("停止后保持FINALIZING，直到IDLE才刷新录像列表", async () => {
  const item = fixture([{state: "STOPPING"}, {state: "IDLE"}]);
  assert.equal(item.controller.start(), true);
  assert.equal(item.controller.start(), false);
  await flush();
  assert.deepEqual(item.rendered, ["STOPPING"]);
  assert.equal(item.refreshes, 0);
  assert.equal(item.timers.length, 1);
  assert.equal(item.timers[0].delay, 300);
  item.timers[0].callback();
  await flush();
  assert.deepEqual(item.rendered, ["STOPPING", "IDLE"]);
  assert.equal(item.refreshes, 1);
  assert.match(item.messages.at(-1), /录像已完成/);
});

test("录像ERROR时显示错误并刷新列表", async () => {
  const item = fixture([{state: "ERROR", error: "codec failed"}]);
  item.controller.start();
  await flush();
  assert.equal(item.refreshes, 1);
  assert.match(item.messages.at(-1), /codec failed/);
  assert.equal(item.controller.running, false);
});

test("录像完成等待超过15秒时给出明确提示且不提前刷新", async () => {
  let now = 0;
  const item = fixture([{state: "STOPPING"}, {state: "STOPPING"}], {now: () => now});
  item.controller.start();
  await flush();
  assert.equal(item.refreshes, 0);
  now = 15001;
  item.timers[0].callback();
  await flush();
  assert.equal(item.refreshes, 0);
  assert.match(item.messages.at(-1), /超时/);
  assert.equal(item.controller.running, false);
});
