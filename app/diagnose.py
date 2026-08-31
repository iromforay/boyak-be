"""진단 프로세스: 알약 사진 -> 모양/색상 예측.

교정/저장/개선 로직은 다루지 않는다. 순수하게 "지금 이 사진에서 무엇으로
보이는가"만 answer한다. 결과를 사람이 확인하고 교정하는 흐름은 review.py,
쌓인 교정 데이터로 개선 방향을 뽑는 것은 improve.py가 담당한다.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pill_analyze import analyze, save_detection_overlay  # noqa: E402

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def diagnose(image_path: str, save_overlay: bool = True) -> list:
    """이미지를 진단해 알약별 {shape, color, color_rgb, imprint} 리스트를 반환한다."""
    results = analyze(image_path, with_imprint=True)

    if save_overlay and results:
        overlay_path = str(Path(image_path).with_name(Path(image_path).stem + "_detected.png"))
        save_detection_overlay(image_path, results, overlay_path)

    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/diagnose.py <image_path>")
        sys.exit(1)

    path = sys.argv[1]
    if not Path(path).exists():
        print(f"File not found: {path}")
        sys.exit(1)

    results = diagnose(path)
    if not results:
        print("알약을 찾지 못했습니다.")
        sys.exit(0)

    for i, r in enumerate(results, 1):
        print(f"[알약 {i}] 모양={r['shape']} / 색상={r['color']} {r['color_rgb']}")

    overlay_path = str(Path(path).with_name(Path(path).stem + "_detected.png"))
    print(f"\n탐지 결과 이미지: {overlay_path}")
