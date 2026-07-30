# Node 测试审计

`touch_layout.test.js` 在顶层安全捕获 `require("playwright")` 失败；缺少模块时不会中止 Node 测试进程，而是为浏览器布局用例提供清晰的 `Playwright 未安装：MODULE_NOT_FOUND` 跳过原因。模块存在但 Chromium 未安装时同样明确跳过。

基础命令：`node --test tests/js/*.test.js`。

可选浏览器依赖：`npm install --save-dev playwright`，然后运行 `npx playwright install chromium`。Playwright 不是树莓派生产运行依赖。
