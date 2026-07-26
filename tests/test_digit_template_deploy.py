"""数字模板部署检查不依赖摄像头或GUI。"""

from pathlib import Path

import cv2
import numpy as np

from tools.check_digit_templates import find_missing_digit_templates, main


def test_digit_template_check_requires_decodable_png_per_class(tmp_path: Path) -> None:
    for digit in range(10):
        directory = tmp_path / str(digit)
        directory.mkdir()
        if digit != 4:
            assert cv2.imwrite(str(directory / "template.png"), np.zeros((8, 8), np.uint8))
    (tmp_path / "4" / "broken.png").write_bytes(b"not a png")
    assert find_missing_digit_templates(tmp_path) == [4]
    assert main(["--detector", "digit", "--template-root", str(tmp_path)]) == 1
    assert main(["--detector", "color", "--template-root", str(tmp_path)]) == 0
