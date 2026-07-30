"""Safely inspect, validate and switch the active steel-ball model profile."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import sys
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config_loader import load_steel_ball_ncnn_config
from tools.validate_steel_ball_models import (
    CORE_MODEL_FILES,
    EXPECTED_MODEL_PATHS,
    PROFILE_PATHS,
    print_result,
    validate_models,
    validate_profile,
)


ACTIVE_CONFIG = PROJECT_ROOT / "config/steel_ball_ncnn.yaml"
BACKUP_DIR = PROJECT_ROOT / "runtime_backups"
RESTART_COMMAND = "sudo systemctl restart vision-touch.service"


def active_profile() -> tuple[str, Path]:
    config = load_steel_ball_ncnn_config(ACTIVE_CONFIG)
    model_path = Path(config.model_path).resolve()
    for profile, expected_path in EXPECTED_MODEL_PATHS.items():
        if model_path == expected_path.resolve():
            return profile, model_path
    return "custom", model_path


def show_status() -> int:
    try:
        profile, model_path = active_profile()
    except Exception as exc:
        print(f"status: FAIL - {type(exc).__name__}: {exc}")
        return 1

    print(f"active_model_path: {model_path}")
    print(f"active_profile: {profile}")
    healthy = True
    for name in CORE_MODEL_FILES:
        path = model_path / name
        size = path.stat().st_size if path.is_file() else 0
        state = "OK" if size > 0 else "MISSING_OR_EMPTY"
        print(f"  {name}: {state} ({size} bytes)")
        healthy = healthy and size > 0
    return 0 if healthy else 1


def atomic_activate(profile: str) -> int:
    result = validate_profile(profile, load_runtime=True)
    print_result(result)
    if result.error is not None or not result.loaded:
        print(f"未切换：{profile} 模型验证失败。")
        return 1

    source = PROFILE_PATHS[profile]
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = BACKUP_DIR / f"steel_ball_ncnn_before_{profile}_{timestamp}.yaml"
    if ACTIVE_CONFIG.is_file():
        shutil.copy2(ACTIVE_CONFIG, backup)

    temporary = ACTIVE_CONFIG.with_name(
        f".{ACTIVE_CONFIG.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, ACTIVE_CONFIG)
    finally:
        if temporary.exists():
            temporary.unlink()

    current_profile, current_path = active_profile()
    if current_profile != profile:
        print(f"切换后校验失败：当前配置为 {current_profile} ({current_path})")
        return 1

    print(f"配置备份: {backup}")
    if profile == "candidate":
        print("警告：当前已切换到候选模型，请仅用于受控 A/B 验证。")
    else:
        print("当前已切换到 baseline 模型。")
    print("工具未重启任何服务。部署到树莓派后请手动执行：")
    print(RESTART_COMMAND)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("status", "baseline", "candidate", "validate")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    command = build_parser().parse_args(argv).command
    if command == "status":
        return show_status()
    if command == "validate":
        return 0 if validate_models(load_runtime=True) else 1
    return atomic_activate(command)


if __name__ == "__main__":
    raise SystemExit(main())
