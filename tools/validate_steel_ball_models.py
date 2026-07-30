"""Validate both steel-ball NCNN profiles with the production runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config_loader import load_steel_ball_ncnn_config
from inference.steel_ball_ncnn_runtime import SteelBallNcnnRuntime


PROFILE_PATHS = {
    "baseline": PROJECT_ROOT / "config/model_profiles/steel_ball_baseline.yaml",
    "candidate": PROJECT_ROOT / "config/model_profiles/steel_ball_candidate.yaml",
}
EXPECTED_MODEL_PATHS = {
    "baseline": PROJECT_ROOT / "models/steel_ball/best_ncnn_model",
    "candidate": PROJECT_ROOT / "models/steel_ball/candidate_ncnn_model",
}
CORE_MODEL_FILES = (
    "model.ncnn.param",
    "model.ncnn.bin",
    "metadata.yaml",
    "model_ncnn.py",
)


@dataclass(frozen=True)
class ValidationResult:
    profile: str
    model_path: Path
    class_names: dict[int, str]
    input_size: tuple[int, int] | None
    input_names: tuple[str, ...]
    output_names: tuple[str, ...]
    loaded: bool
    error: str | None = None


def validate_profile(profile: str, *, load_runtime: bool = True) -> ValidationResult:
    """Validate one profile and always release any initialized NCNN runtime."""

    if profile not in PROFILE_PATHS:
        raise ValueError(f"unknown model profile: {profile}")

    model_path = EXPECTED_MODEL_PATHS[profile].resolve()
    runtime: SteelBallNcnnRuntime | None = None
    try:
        config = load_steel_ball_ncnn_config(PROFILE_PATHS[profile])
        configured_path = Path(config.model_path).resolve()
        if configured_path != model_path:
            raise ValueError(
                f"profile model_path mismatch: expected {model_path}, got {configured_path}"
            )

        missing = [
            name
            for name in CORE_MODEL_FILES
            if not (model_path / name).is_file()
            or (model_path / name).stat().st_size <= 0
        ]
        if missing:
            raise ValueError("missing or empty model file(s): " + ", ".join(missing))

        runtime = SteelBallNcnnRuntime(
            model_dir=config.model_path,
            imgsz=config.imgsz,
            conf_threshold=config.conf_threshold,
            iou_threshold=config.iou_threshold,
            max_det=config.max_det,
            num_threads=config.num_threads,
        )
        if load_runtime:
            runtime.load()
        else:
            # Static validation deliberately uses the production runtime's own
            # metadata and graph checks without allocating an NCNN network.
            paths = runtime._model_paths()
            from inference.steel_ball_ncnn_runtime import (
                _parse_param_graph,
                _parse_wrapper_nodes,
                _read_metadata,
            )

            metadata, class_names = _read_metadata(paths["metadata"])
            input_names, output_names = _parse_param_graph(paths["param"])
            wrapper_inputs, wrapper_outputs = _parse_wrapper_nodes(paths["wrapper"])
            if (input_names, output_names) != (wrapper_inputs, wrapper_outputs):
                raise ValueError(
                    "generated wrapper node names do not match the NCNN graph"
                )
            runtime.metadata = metadata
            runtime.class_names = class_names
            runtime.input_names = input_names
            runtime.output_names = output_names

        raw_size = runtime.metadata.get("imgsz")
        input_size = (
            (int(raw_size[0]), int(raw_size[1]))
            if isinstance(raw_size, (list, tuple)) and len(raw_size) == 2
            else None
        )
        return ValidationResult(
            profile=profile,
            model_path=model_path,
            class_names=dict(runtime.class_names),
            input_size=input_size,
            input_names=tuple(runtime.input_names),
            output_names=tuple(runtime.output_names),
            loaded=runtime.is_loaded if load_runtime else True,
        )
    except Exception as exc:
        return ValidationResult(
            profile=profile,
            model_path=model_path,
            class_names={},
            input_size=None,
            input_names=(),
            output_names=(),
            loaded=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if runtime is not None:
            runtime.close()


def print_result(result: ValidationResult) -> None:
    status = "PASS" if result.error is None and result.loaded else "FAIL"
    print(f"[{result.profile}] {status}")
    print(f"  model_path: {result.model_path}")
    print(f"  class_names: {result.class_names}")
    print(f"  input_size: {result.input_size}")
    print(f"  input_nodes: {list(result.input_names)}")
    print(f"  output_nodes: {list(result.output_names)}")
    print(f"  runtime_loaded: {result.loaded}")
    if result.error is not None:
        print(f"  reason: {result.error}")


def validate_models(*, load_runtime: bool = True) -> bool:
    results = [
        validate_profile(profile, load_runtime=load_runtime)
        for profile in ("baseline", "candidate")
    ]
    for result in results:
        print_result(result)
    passed = all(result.error is None and result.loaded for result in results)
    print("ALL_MODELS: PASS" if passed else "ALL_MODELS: FAIL")
    return passed


def main() -> int:
    return 0 if validate_models(load_runtime=True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
