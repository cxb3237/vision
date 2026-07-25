"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const WEB_ROOT = path.join(ROOT, "touch_ui_web");
const controls = {
  brightness: {supported: true, minimum: -64, maximum: 64, step: 1, requested: 0, actual: 0, mismatch: false},
  contrast: {supported: true, minimum: 0, maximum: 64, step: 1, requested: 16, actual: 16, mismatch: false},
  gain: {supported: true, minimum: 0, maximum: 255, step: 1, requested: 7, actual: 7, mismatch: false},
};

function json(response, value) {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(200, {"Content-Type": "application/json", "Content-Length": body.length});
  response.end(body);
}

function createServer() {
  return http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://127.0.0.1").pathname;
    if (pathname === "/api/status") {
      json(response, {ok: true, status: {runtime_running: true, camera_online: true, serial_online: true, vmc_tx_count: 3, detector: "digit", state: "LOCKED", fps: 30, commands: {}, ui: {parameter_debounce_ms: 20}}});
      return;
    }
    if (pathname === "/api/config/camera") {
      json(response, {ok: true, camera: {controls, modified: false, override_file_active: false}});
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
}

function intersects(first, second) {
  return first.left < second.right && first.right > second.left && first.top < second.bottom && first.bottom > second.top;
}

test("drawer handle and panel geometry never overlap protected content", {timeout: 30000}, async (context) => {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({headless: true});
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
    {width: 1280, height: 720},
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
      };
    });
    assert.ok(closed.protected.every((item) => !intersects(closed.handle, item)), `closed overlap at ${viewport.width}x${viewport.height}`);
    assert.deepEqual(closed.scroll, [viewport.height, viewport.height, viewport.height, viewport.height]);

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
