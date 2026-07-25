"""使用Node内置测试运行器验证前端异步状态机，无需浏览器或硬件。"""

from pathlib import Path
import os
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("当前环境没有Node.js，跳过纯JavaScript状态机测试")
    return node


def _run_node_test(node: str, path: Path) -> None:
    completed = subprocess.run(
        [node, "--test", str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_control_scheduler_javascript_suite() -> None:
    _run_node_test(_node(), ROOT / "tests/js/control_scheduler.test.js")


def test_touch_layout_geometry_with_playwright() -> None:
    if os.environ.get("RUN_TOUCH_PLAYWRIGHT_TESTS") != "1":
        pytest.skip("浏览器几何测试需设置RUN_TOUCH_PLAYWRIGHT_TESTS=1显式启用")
    node = _node()
    available = subprocess.run(
        [node, "-e", "require.resolve('playwright')"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if available.returncode != 0:
        pytest.skip("当前Node.js环境没有Playwright，跳过浏览器几何测试")
    _run_node_test(node, ROOT / "tests/js/touch_layout.test.js")
