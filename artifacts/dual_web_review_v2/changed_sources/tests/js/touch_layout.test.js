"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const WEB_ROOT = path.join(ROOT, "web_debug", "static");
const browserExecutable = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || chromium.executablePath();
const HAS_BROWSER = fs.existsSync(browserExecutable);
const baseControls = {
  brightness: {supported: true, writable: true, type: "int", minimum: -64, maximum: 64, step: 1, requested: 0, actual: 0, mismatch: false},
  contrast: {supported: true, writable: true, type: "int", minimum: 0, maximum: 64, step: 1, requested: 16, actual: 16, mismatch: false},
  sharpness: {supported: true, writable: true, type: "int", minimum: 0, maximum: 10, step: 1, requested: 4, actual: 4, mismatch: false},
  gain: {supported: true, writable: true, type: "int", minimum: 0, maximum: 255, step: 1, requested: 7, actual: 7, mismatch: false},
  backlight_compensation: {supported: true, writable: true, type: "int", minimum: 0, maximum: 10, step: 1, requested: 0, actual: 0, mismatch: false},
  power_line_frequency: {supported: true, writable: true, type: "menu", minimum: 0, maximum: 2, step: 1, choices: [{value: 0, label: "Disabled"}, {value: 1, label: "50 Hz"}, {value: 2, label: "60 Hz"}], requested: 1, actual: 1, mismatch: false},
  white_balance_automatic: {supported: true, writable: true, type: "bool", minimum: 0, maximum: 1, step: 1, choices: [{value: 0, label: "Off"}, {value: 1, label: "On"}], requested: 1, actual: 1, mismatch: false},
  white_balance_temperature: {supported: true, writable: true, type: "int", minimum: 2500, maximum: 7000, step: 100, requested: 5000, actual: 5000, mismatch: false},
  exposure_auto: {supported: true, writable: true, type: "menu", minimum: 1, maximum: 3, step: 1, choices: [{value: 1, label: "Manual Mode"}, {value: 3, label: "Aperture Priority Mode"}], requested: 3, actual: 3, mismatch: false},
  exposure_absolute: {supported: true, writable: true, type: "int", minimum: 1, maximum: 1000, step: 1, requested: 100, actual: 100, mismatch: false},
  saturation: {supported: false, writable: false, type: "int", minimum: 0, maximum: 100, step: 1, requested: 36, actual: 36},
  gamma: {supported: true, writable: false, read_only: true, type: "int", minimum: 1, maximum: 500, step: 1, actual: 100},
  hue: {supported: true, writable: true, type: "int", minimum: null, maximum: null, step: null, actual: 0},
};

function json(response, value, status = 200) {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {"Content-Type": "application/json", "Content-Length": body.length});
  response.end(body);
}

function createServer({patchFailure = false, applyDelayMs = 0, controlOverrides = {}} = {}) {
  const state = {
    controls: JSON.parse(JSON.stringify(baseControls)),
    commands: {},
    patches: [],
    nextCommand: 1,
    competitionMode: false,
    visionOutputEnabled: false,
    serialOnline: true,
    mcuReady: true,
    positionTxHz: 0,
    ballXmm: null,
    uartState: "STOPPED",
  };
  Object.entries(controlOverrides).forEach(([name, override]) => {
    state.controls[name] = {...state.controls[name], ...override};
  });
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://127.0.0.1").pathname;
    if (pathname === "/api/status") {
      Object.values(state.commands).forEach((command) => {
        if (command.status === "QUEUED" && Date.now() >= command.appliedAt) {
          command.status = "APPLIED";
          delete command.appliedAt;
        }
      });
      json(response, {ok: true, status: {runtime_running: true, camera_online: true, mcu_ready: state.mcuReady, serial_online: state.serialOnline, position_tx_count: 3, position_tx_hz: state.positionTxHz, ball_x_mm: state.ballXmm, uart_state: state.uartState, detector: "steel_ball_yolo_ncnn", state: "LOCKED", fps: 30, competition_mode: state.competitionMode, vision_output_enabled: state.visionOutputEnabled, commands: state.commands, ui: {parameter_debounce_ms: 20}}});
      return;
    }
    if (pathname === "/api/config/camera") {
      if (request.method === "PATCH") {
        const chunks = [];
        request.on("data", (chunk) => chunks.push(chunk));
        request.on("end", () => {
          const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          const [name, value] = Object.entries(payload.controls)[0];
          state.patches.push([name, value]);
          if (patchFailure) {
            json(response, {ok: false, message: "模拟参数写入失败"}, 500);
            return;
          }
          state.controls[name].requested = value;
          state.controls[name].actual = value;
          const commandId = `command-${state.nextCommand++}`;
          state.commands[commandId] = applyDelayMs > 0
            ? {status: "QUEUED", appliedAt: Date.now() + applyDelayMs}
            : {status: "APPLIED"};
          json(response, {ok: true, command_id: commandId, status: "QUEUED"});
        });
        return;
      }
      json(response, {ok: true, camera: {controls: state.controls, modified: state.patches.length > 0, override_file_active: false}});
      return;
    }
    const fileName = pathname === "/" ? "index.html" : pathname.slice(1);
    const filePath = path.join(WEB_ROOT, fileName);
    if (!filePath.startsWith(WEB_ROOT) || !fs.existsSync(filePath)) {
      response.writeHead(404);
      response.end();
      return;
    }
    const extension = path.extname(filePath);
    const contentType = extension === ".css" ? "text/css" : extension === ".js" ? "application/javascript" : "text/html";
    response.writeHead(200, {"Content-Type": contentType});
    fs.createReadStream(filePath).pipe(response);
  });
  server.fixtureState = state;
  return server;
}

function intersects(first, second) {
  return first.left < second.right && first.right > second.left && first.top < second.bottom && first.bottom > second.top;
}

function launchBrowser() {
  return chromium.launch({headless: true, executablePath: browserExecutable});
}

test("vision output status is independent from UART online state", {timeout: 30000, skip: !HAS_BROWSER}, async (context) => {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await launchBrowser();
  context.after(async () => {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  });
  const page = await browser.newPage({viewport: {width: 800, height: 480}});
  await page.goto(`http://127.0.0.1:${server.address().port}/`, {waitUntil: "domcontentloaded"});
  await page.waitForFunction(() => document.querySelector("#competitionBadge").textContent.includes("调试识别"));
  assert.equal(await page.locator("#serialBadge").innerText(), "串口在线");
  assert.equal(await page.locator("#competitionBadge").innerText(), "调试识别");
  assert.equal(await page.locator("#positionTxBadge").innerText(), "已停止");

  server.fixtureState.competitionMode = true;
  server.fixtureState.visionOutputEnabled = true;
  server.fixtureState.positionTxHz = 20;
  server.fixtureState.ballXmm = 10;
  await page.waitForFunction(() => document.querySelector("#competitionBadge").textContent.includes("比赛识别有效"));
  assert.equal(await page.locator("#competitionBadge").innerText(), "比赛识别有效");
  await page.waitForFunction(() => document.querySelector("#positionTxBadge").textContent === "位置下发运行中");

  server.fixtureState.serialOnline = false;
  server.fixtureState.positionTxHz = 0;
  server.fixtureState.uartState = "DISCONNECTED";
  await page.waitForFunction(() => document.querySelector("#positionTxBadge").textContent === "UART故障");
});

async function dispatchPointer(page, targetSelector, type, options) {
  return page.evaluate(({targetSelector, type, options}) => {
    const target = targetSelector === "window" ? window : document.querySelector(targetSelector);
    const event = new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      isPrimary: true,
      button: 0,
      buttons: type === "pointerup" || type === "pointercancel" ? 0 : 1,
      ...options,
    });
    target.dispatchEvent(event);
    return event.defaultPrevented;
  }, {targetSelector, type, options});
}

test("drawer handle and panel geometry never overlap protected content", {timeout: 30000, skip: !HAS_BROWSER}, async (context) => {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await launchBrowser();
  context.after(async () => {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  });
  const page = await browser.newPage();
  page.setDefaultTimeout(5000);
  page.on("pageerror", (error) => context.diagnostic(`page error: ${error.message}`));
  const port = server.address().port;

  for (const viewport of [
    {width: 800, height: 480},
    {width: 1024, height: 600},
    {width: 1280, height: 720},
    {width: 1280, height: 800},
    {width: 1920, height: 1080},
    {width: 720, height: 1280},
  ]) {
    await page.setViewportSize(viewport);
    await page.goto(`http://127.0.0.1:${port}/`, {waitUntil: "domcontentloaded"});
    const closed = await page.evaluate(() => {
      const rect = (element) => {
        const value = element.getBoundingClientRect();
        return {left: value.left, right: value.right, top: value.top, bottom: value.bottom};
      };
      return {
        handle: rect(document.querySelector("#drawerHandle")),
        protected: [...document.querySelectorAll(".status-tags > *, .telemetry-lines dt, .telemetry-lines dd, #lastError, #normalDock button")].map(rect),
        scroll: [document.documentElement.scrollHeight, document.documentElement.clientHeight, document.body.scrollHeight, document.body.clientHeight],
        actions: [...document.querySelectorAll(".persistent-actions button")].map(rect),
      };
    });
    assert.ok(closed.protected.every((item) => !intersects(closed.handle, item)), `closed overlap at ${viewport.width}x${viewport.height}`);
    assert.deepEqual(closed.scroll, [viewport.height, viewport.height, viewport.height, viewport.height]);
    assert.ok(closed.actions.every((item) => item.top >= 0 && item.bottom <= viewport.height));

    await page.getByRole("button", {name: "打开摄像头参数"}).click();
    await page.locator("[data-control=brightness]").waitFor({state: "visible"});
    const opened = await page.evaluate(() => {
      const rect = (element) => {
        const value = element.getBoundingClientRect();
        return {left: value.left, right: value.right, top: value.top, bottom: value.bottom};
      };
      return {
        handle: rect(document.querySelector("#drawerHandle")),
        controls: [...document.querySelectorAll("#cameraPanel button, #cameraPanel input")].map(rect),
        panel: rect(document.querySelector("#cameraPanel")),
        preview: rect(document.querySelector("#previewPane")),
        scroll: [document.documentElement.scrollHeight, document.documentElement.clientHeight, document.body.scrollHeight, document.body.clientHeight],
      };
    });
    assert.ok(opened.controls.every((item) => !intersects(opened.handle, item)), `open overlap at ${viewport.width}x${viewport.height}`);
    assert.equal(intersects(opened.panel, opened.preview), false);
    assert.deepEqual(opened.scroll, [viewport.height, viewport.height, viewport.height, viewport.height]);
  }
});

test("touch range distinguishes vertical scrolling from horizontal adjustment", {timeout: 30000, skip: !HAS_BROWSER}, async (context) => {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await launchBrowser();
  context.after(async () => {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  });
  const page = await browser.newPage({viewport: {width: 800, height: 480}});
  const port = server.address().port;
  await page.goto(`http://127.0.0.1:${port}/`, {waitUntil: "domcontentloaded"});
  await page.getByRole("button", {name: "打开摄像头参数"}).click();
  const range = page.locator('[data-control-input="brightness"]');
  await range.waitFor({state: "visible"});
  const box = await range.boundingBox();
  const center = {x: box.x + box.width / 2, y: box.y + box.height / 2};
  const initialValue = await range.inputValue();

  await dispatchPointer(page, '[data-control-input="brightness"]', "pointerdown", {pointerId: 11, pointerType: "touch", clientX: center.x, clientY: center.y});
  const verticalPrevented = await dispatchPointer(page, "window", "pointermove", {pointerId: 11, pointerType: "touch", clientX: center.x + 4, clientY: center.y + 30});
  await dispatchPointer(page, "window", "pointerup", {pointerId: 11, pointerType: "touch", clientX: center.x + 4, clientY: center.y + 30});
  await page.waitForTimeout(80);
  assert.equal(verticalPrevented, false);
  assert.equal(await range.inputValue(), initialValue);
  assert.equal(server.fixtureState.patches.length, 0);

  await dispatchPointer(page, '[data-control-input="brightness"]', "pointerdown", {pointerId: 12, pointerType: "touch", clientX: center.x, clientY: center.y});
  await dispatchPointer(page, "window", "pointermove", {pointerId: 12, pointerType: "touch", clientX: center.x + 8, clientY: center.y + 8});
  await dispatchPointer(page, "window", "pointerup", {pointerId: 12, pointerType: "touch", clientX: center.x + 8, clientY: center.y + 8});
  assert.equal(await range.inputValue(), initialValue);
  assert.equal(server.fixtureState.patches.length, 0);

  await dispatchPointer(page, '[data-control-input="brightness"]', "pointerdown", {pointerId: 13, pointerType: "touch", clientX: center.x, clientY: center.y});
  await dispatchPointer(page, "window", "pointermove", {pointerId: 13, pointerType: "touch", clientX: center.x + 30, clientY: center.y + 4});
  assert.ok(await page.locator('[data-control="brightness"]').evaluate((row) => row.classList.contains("adjusting")));
  assert.notEqual(await range.inputValue(), initialValue);
  await dispatchPointer(page, "window", "pointerup", {pointerId: 13, pointerType: "touch", clientX: center.x + 30, clientY: center.y + 4});
  await page.waitForFunction(() => window.__visionTouchTest.controlScheduler.phase("brightness") === "IDLE");
  assert.equal(server.fixtureState.patches.filter(([name]) => name === "brightness").length, 1);

  const confirmedValue = await page.locator('[data-control-input="brightness"]').inputValue();
  const currentBox = await page.locator('[data-control-input="brightness"]').boundingBox();
  const patchCountBeforeCancel = server.fixtureState.patches.length;
  await dispatchPointer(page, '[data-control-input="brightness"]', "pointerdown", {pointerId: 14, pointerType: "touch", clientX: currentBox.x + currentBox.width / 2, clientY: currentBox.y + 16});
  await dispatchPointer(page, "window", "pointermove", {pointerId: 14, pointerType: "touch", clientX: currentBox.x + 10, clientY: currentBox.y + 16});
  await dispatchPointer(page, "window", "pointercancel", {pointerId: 14, pointerType: "touch", clientX: currentBox.x + 10, clientY: currentBox.y + 16});
  await page.waitForTimeout(80);
  assert.equal(await page.locator('[data-control-input="brightness"]').inputValue(), confirmedValue);
  assert.equal(server.fixtureState.patches.length, patchCountBeforeCancel);

  const patchCountBeforeTap = server.fixtureState.patches.length;
  await dispatchPointer(page, '[data-control-input="brightness"]', "pointerdown", {pointerId: 15, pointerType: "touch", clientX: currentBox.x + 5, clientY: currentBox.y + 16});
  await dispatchPointer(page, "window", "pointerup", {pointerId: 15, pointerType: "touch", clientX: currentBox.x + 5, clientY: currentBox.y + 16});
  await page.waitForTimeout(80);
  assert.equal(await page.locator('[data-control-input="brightness"]').inputValue(), confirmedValue);
  assert.equal(server.fixtureState.patches.length, patchCountBeforeTap);
});

test("successful apply clears pending diagnostic with one render", {timeout: 30000, skip: !HAS_BROWSER}, async (context) => {
  const server = createServer({applyDelayMs: 220});
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await launchBrowser();
  context.after(async () => {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  });
  const page = await browser.newPage({viewport: {width: 800, height: 480}});
  await page.goto(`http://127.0.0.1:${server.address().port}/`, {waitUntil: "domcontentloaded"});
  await page.getByRole("button", {name: "打开摄像头参数"}).click();
  await page.locator('[data-control="brightness"]').waitFor({state: "visible"});
  const initial = Number(await page.locator('[data-control="brightness"] [data-role="value"]').innerText());
  const renderBefore = await page.evaluate(() => window.__visionTouchTest.getCameraControlsRenderCount());

  await page.getByRole("button", {name: "亮度减少"}).click();
  await page.waitForFunction(() => ["DEBOUNCE", "SENT"].includes(window.__visionTouchTest.controlScheduler.phase("brightness")));
  const applying = await page.locator('[data-control="brightness"]').evaluate((row) => ({
    pending: row.classList.contains("pending"),
    diagnostic: row.querySelector('[data-role="diagnostic"]').textContent,
    hidden: row.querySelector('[data-role="diagnostic"]').hidden,
  }));
  assert.deepEqual(applying, {pending: true, diagnostic: "正在应用", hidden: false});

  await page.waitForFunction(() => window.__visionTouchTest.controlScheduler.phase("brightness") === "IDLE");
  const completed = await page.locator('[data-control="brightness"]').evaluate((row) => ({
    pending: row.classList.contains("pending"),
    diagnostic: row.querySelector('[data-role="diagnostic"]').textContent,
    hidden: row.querySelector('[data-role="diagnostic"]').hidden,
    value: row.querySelector('[data-role="value"]').textContent,
  }));
  assert.equal(completed.value, String(initial - 1));
  assert.equal(completed.pending, false);
  assert.ok(completed.hidden || completed.diagnostic === "");
  assert.doesNotMatch(await page.locator("#cameraControls").innerText(), /正在应用/);
  const renderAfter = await page.evaluate(() => window.__visionTouchTest.getCameraControlsRenderCount());
  assert.equal(renderAfter, renderBefore + 1);
});

test("failed apply keeps the Chinese failure diagnostic", {timeout: 30000, skip: !HAS_BROWSER}, async (context) => {
  const server = createServer({patchFailure: true});
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await launchBrowser();
  context.after(async () => {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  });
  const page = await browser.newPage({viewport: {width: 800, height: 480}});
  await page.goto(`http://127.0.0.1:${server.address().port}/`, {waitUntil: "domcontentloaded"});
  await page.getByRole("button", {name: "打开摄像头参数"}).click();
  await page.getByRole("button", {name: "亮度减少"}).click();
  await page.waitForFunction(() => window.__visionTouchTest.controlScheduler.phase("brightness") === "IDLE");
  const failed = await page.locator('[data-control="brightness"]').evaluate((row) => ({
    pending: row.classList.contains("pending"),
    diagnostic: row.querySelector('[data-role="diagnostic"]').textContent,
    hidden: row.querySelector('[data-role="diagnostic"]').hidden,
  }));
  assert.equal(failed.diagnostic, "应用失败");
  assert.equal(failed.hidden, false);
  assert.equal(failed.pending, false);
});

test("requested and actual mismatch shows values without applying state", {timeout: 30000, skip: !HAS_BROWSER}, async (context) => {
  const server = createServer({
    controlOverrides: {brightness: {requested: 20, actual: 18, mismatch: true}},
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await launchBrowser();
  context.after(async () => {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  });
  const page = await browser.newPage({viewport: {width: 800, height: 480}});
  await page.goto(`http://127.0.0.1:${server.address().port}/`, {waitUntil: "domcontentloaded"});
  await page.getByRole("button", {name: "打开摄像头参数"}).click();
  const mismatch = await page.locator('[data-control="brightness"]').evaluate((row) => ({
    pending: row.classList.contains("pending"),
    diagnostic: row.querySelector('[data-role="diagnostic"]').textContent,
  }));
  assert.deepEqual(mismatch, {pending: false, diagnostic: "设置值：20　实际值：18"});
  assert.doesNotMatch(await page.locator("#cameraControls").innerText(), /正在应用/);
});

test("compact controls are Chinese, writable-only and preserve mouse/button behavior", {timeout: 30000, skip: !HAS_BROWSER}, async (context) => {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await launchBrowser();
  context.after(async () => {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  });
  const page = await browser.newPage({viewport: {width: 800, height: 480}});
  const port = server.address().port;
  await page.goto(`http://127.0.0.1:${port}/`, {waitUntil: "domcontentloaded"});
  await page.getByRole("button", {name: "打开摄像头参数"}).click();
  await page.locator('[data-control="brightness"]').waitFor({state: "visible"});

  const panelText = await page.locator("#cameraControls").innerText();
  assert.match(panelText, /图像[\s\S]*亮度[\s\S]*对比度[\s\S]*锐度/);
  assert.match(panelText, /曝光[\s\S]*自动曝光[\s\S]*增益[\s\S]*抗频闪频率/);
  assert.match(panelText, /白平衡[\s\S]*自动白平衡/);
  assert.doesNotMatch(panelText, /brightness|contrast|white_balance|SUPPORTED|requested|actual/i);
  assert.equal(await page.locator('[data-control="saturation"]').count(), 0);
  assert.equal(await page.locator('[data-control="gamma"]').count(), 0);
  assert.equal(await page.locator('[data-control="hue"]').count(), 0);
  assert.equal(await page.locator('[data-control="white_balance_temperature"]').count(), 0);
  assert.equal(await page.locator('[data-control="exposure_absolute"]').count(), 0);

  const brightnessValue = page.locator('[data-control="brightness"] [data-role="value"]');
  const initial = Number(await brightnessValue.innerText());
  const renderCountBeforeApply = await page.evaluate(() => window.__visionTouchTest.getCameraControlsRenderCount());
  await page.getByRole("button", {name: "亮度减少"}).click();
  await page.waitForFunction(() => window.__visionTouchTest.controlScheduler.phase("brightness") === "IDLE");
  assert.equal(Number(await brightnessValue.innerText()), initial - 1);
  const renderCountAfterApply = await page.evaluate(() => window.__visionTouchTest.getCameraControlsRenderCount());
  assert.equal(renderCountAfterApply, renderCountBeforeApply + 1);
  await page.getByRole("button", {name: "亮度增加"}).click();
  await page.waitForFunction(() => window.__visionTouchTest.controlScheduler.phase("brightness") === "IDLE");
  assert.equal(Number(await brightnessValue.innerText()), initial);

  const range = page.locator('[data-control-input="brightness"]');
  const box = await range.boundingBox();
  await page.waitForTimeout(520);
  await page.mouse.click(box.x + box.width * 0.8, box.y + box.height / 2);
  await page.waitForFunction(() => window.__visionTouchTest.controlScheduler.phase("brightness") === "IDLE");
  assert.ok(Number(await brightnessValue.innerText()) > initial);

  const beforeHold = Number(await brightnessValue.innerText());
  const plus = page.getByRole("button", {name: "亮度增加"});
  const plusBox = await plus.boundingBox();
  await page.mouse.move(plusBox.x + plusBox.width / 2, plusBox.y + plusBox.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(720);
  await page.mouse.up();
  await page.waitForFunction(() => window.__visionTouchTest.controlScheduler.phase("brightness") === "IDLE");
  const afterHold = Number(await brightnessValue.innerText());
  assert.ok(afterHold >= beforeHold + 2);
  await page.waitForTimeout(350);
  assert.equal(Number(await brightnessValue.innerText()), afterHold);

  await page.getByRole("button", {name: "自动白平衡减少"}).click();
  await page.locator('[data-control="white_balance_temperature"]').waitFor({state: "visible"});
  assert.deepEqual(server.fixtureState.patches.find(([name]) => name === "white_balance_automatic"), ["white_balance_automatic", 0]);
  await page.getByRole("button", {name: "自动曝光减少"}).click();
  await page.locator('[data-control="exposure_absolute"]').waitFor({state: "visible"});
  assert.deepEqual(server.fixtureState.patches.find(([name]) => name === "exposure_auto"), ["exposure_auto", 1]);

  await page.evaluate(() => { document.querySelector("#cameraControls").scrollTop = 0; });
  const geometry = await page.evaluate(() => ({
    document: [document.documentElement.scrollHeight, document.documentElement.clientHeight],
    body: [document.body.scrollHeight, document.body.clientHeight],
    controls: [document.querySelector("#cameraControls").scrollHeight, document.querySelector("#cameraControls").clientHeight],
    overflowY: getComputedStyle(document.querySelector("#cameraControls")).overflowY,
    fullyVisibleRows: (() => {
      const viewport = document.querySelector("#cameraControls").getBoundingClientRect();
      return [...document.querySelectorAll("#cameraControls .control-row")].filter((row) => {
        const rect = row.getBoundingClientRect();
        return rect.top >= viewport.top && rect.bottom <= viewport.bottom;
      }).length;
    })(),
    footer: (() => {
      const footer = document.querySelector(".drawer-footer");
      const buttons = [...footer.querySelectorAll("button")];
      return {
        width: [footer.scrollWidth, footer.clientWidth],
        buttons: buttons.map((button) => {
          const rect = button.getBoundingClientRect();
          return {
            top: rect.top,
            bottom: rect.bottom,
            textHeight: [button.scrollHeight, button.clientHeight],
            whiteSpace: getComputedStyle(button).whiteSpace,
          };
        }),
      };
    })(),
  }));
  assert.deepEqual(geometry.document, [480, 480]);
  assert.deepEqual(geometry.body, [480, 480]);
  assert.equal(geometry.overflowY, "auto");
  assert.ok(geometry.controls[0] > geometry.controls[1]);
  assert.ok(geometry.controls[1] >= 240, `cameraControls height ${geometry.controls[1]}px`);
  assert.ok(geometry.fullyVisibleRows >= 3, JSON.stringify(geometry));
  assert.ok(geometry.footer.buttons.every((button) => Math.abs(button.top - geometry.footer.buttons[0].top) < 1));
  assert.ok(geometry.footer.buttons.every((button) => button.bottom - button.top >= 48));
  assert.ok(geometry.footer.buttons.every((button) => button.whiteSpace === "nowrap" && button.textHeight[0] <= button.textHeight[1]));
  assert.ok(geometry.footer.width[0] <= geometry.footer.width[1]);
});
