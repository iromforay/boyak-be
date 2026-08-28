"""알약 사진에서 모양/색상/각인을 추출한다.

## 지원 범위 (검증된 사용 조건)
- 알약 2~6개 정도를 서로 겹치지 않게 손바닥/테이블에 놓고 찍은 사진.
- 알약끼리 살짝 인접해도 되지만, 무더기로 쌓이거나 절반 이상 가려지면 안 됨.

## 지원하지 않는 범위 (20장 실사진 검증 결과: 개수 정확도 10%, 평균 오차 7.5개)
- 알약 10개 이상이 쌓이거나 겹친 무더기 사진 -> 실제보다 훨씬 적게 인식됨.
- 알약과 배경/피부 색 대비가 낮은 사진 (예: 흰 알약 + 회색 테이블) -> 탐지 자체가 실패할 수 있음.
- 이런 경우 결과를 신뢰하지 말고, 알약을 펼쳐서 다시 촬영할 것을 사용자에게 안내해야 한다.
"""

import io
import sys
from pathlib import Path

import cv2
import numpy as np
import pytesseract

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# Windows에 기본 설치되는 경로. 다르게 설치했다면 이 값을 수정한다.
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if Path(TESSERACT_CMD).exists():
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

NAMED_COLORS = {
    "white": (255, 255, 255),
    "ivory": (240, 234, 214),
    "yellow": (255, 221, 51),
    "orange": (255, 140, 40),
    "pink": (255, 175, 190),
    "red": (210, 40, 40),
    "brown": (120, 75, 45),
    "purple": (140, 80, 170),
    "blue": (60, 100, 200),
    "green": (70, 150, 80),
    "gray": (150, 150, 150),
    "black": (30, 30, 30),
}


_FASTSAM_MODEL = None


def _get_fastsam_model():
    global _FASTSAM_MODEL
    if _FASTSAM_MODEL is None:
        from ultralytics import FastSAM

        _FASTSAM_MODEL = FastSAM("FastSAM-s.pt")
    return _FASTSAM_MODEL


def _instance_masks(image: np.ndarray):
    """FastSAM으로 이미지 내 모든 객체를 인스턴스 단위로 분리한다.

    임계값 기반 방식과 달리 배경/조명 조건에 크게 좌우되지 않고,
    손 위에 놓인 알약처럼 색이 비슷해도 별개 객체로 분리해낸다.
    """
    model = _get_fastsam_model()
    results = model(image, device="cpu", retina_masks=True, imgsz=1024, conf=0.45, iou=0.5, verbose=False)

    h_img, w_img = image.shape[:2]
    masks = results[0].masks
    if masks is None:
        return

    for mask_tensor in masks.data:
        mask = mask_tensor.cpu().numpy()
        if mask.shape != (h_img, w_img):
            mask = cv2.resize(mask, (w_img, h_img), interpolation=cv2.INTER_NEAREST)
        mask = (mask * 255).astype(np.uint8)
        yield _smooth_mask(mask)


def _smooth_mask(mask):
    """마스크 경계의 잔주름을 다듬어 모양 판별이 흔들리지 않게 한다."""
    blurred = cv2.GaussianBlur(mask, (9, 9), 0)
    _, smoothed = cv2.threshold(blurred, 127, 255, cv2.THRESH_BINARY)
    smoothed = cv2.morphologyEx(smoothed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return smoothed


def _passes_pill_filter(contour, img_shape, margin=4):
    h_img, w_img = img_shape[:2]
    area = cv2.contourArea(contour)
    if area < 0.004 * w_img * h_img or area > 0.35 * w_img * h_img:
        return False

    x, y, w, h = cv2.boundingRect(contour)
    if x <= margin or y <= margin or x + w >= w_img - margin or y + h >= h_img - margin:
        return False  # 이미지 테두리에 닿음 -> 손/팔/배경 일부일 가능성

    hull = cv2.convexHull(contour)
    hull_area = cv2.contourArea(hull)
    if hull_area == 0:
        return False
    solidity = area / hull_area
    if solidity < 0.85:
        return False  # 오목한 부분이 많음(손가락, 반지 등) -> 알약이 아닐 가능성

    aspect_ratio = max(w, h) / max(min(w, h), 1e-6)
    if aspect_ratio > 5:
        return False

    return True


def _mask_iou(mask_a, mask_b):
    inter = np.count_nonzero(mask_a & mask_b)
    union = np.count_nonzero(mask_a | mask_b)
    return inter / union if union > 0 else 0


LAST_RUN_STATS = {"raw_instances": 0, "kept_pills": 0}


def segment_pills(image: np.ndarray):
    """FastSAM 인스턴스 마스크 중 알약처럼 생긴 것만 골라낸다."""
    found_any = False
    raw_count = 0
    candidates = []
    for mask in _instance_masks(image):
        found_any = True
        raw_count += 1
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        if _passes_pill_filter(contour, image.shape):
            candidates.append((contour, mask))

    LAST_RUN_STATS["raw_instances"] = raw_count
    LAST_RUN_STATS["kept_pills"] = len(candidates)

    if not found_any:
        raise ValueError("이미지에서 객체를 분리하지 못했습니다.")
    if not candidates:
        raise ValueError("알약처럼 보이는 영역을 찾지 못했습니다.")

    # 같은 알약을 서로 다른 마스크가 중복으로 잡은 경우, 실제 픽셀 겹침(IoU)이
    # 큰 후보 중 면적이 가장 큰 것만 남긴다. (bbox는 붙어있는 알약끼리도
    # 겹쳐 보이므로 실제 마스크 기준으로 판단해야 한다)
    candidates.sort(key=lambda cm: cv2.contourArea(cm[0]), reverse=True)
    kept_masks = []
    for contour, mask in candidates:
        if any(_mask_iou(mask > 0, km > 0) > 0.3 for km in kept_masks):
            continue
        kept_masks.append(mask)
        yield contour


def build_mask(image_shape, contour):
    mask = np.zeros(image_shape[:2], dtype=np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, thickness=cv2.FILLED)
    return mask


def _ideal_ellipse_circularity(aspect_ratio: float) -> float:
    """종횡비가 aspect_ratio인 매끈한 타원의 이론적 원형도.

    타원은 길쭉할수록 잡음이 전혀 없어도 원형도가 1보다 낮아진다(원만 1.0).
    따라서 실제 컨투어의 원형도를 이 이론값과 비교해야, "길쭉해서 낮은 것"과
    "다각형이라 낮은 것"을 구분할 수 있다. (Ramanujan 둘레 근사 사용)
    """
    a, b = aspect_ratio, 1.0
    perimeter = np.pi * (3 * (a + b) - np.sqrt((3 * a + b) * (a + 3 * b)))
    area = np.pi * a * b
    return 4 * np.pi * area / (perimeter**2)


def classify_shape(contour) -> str:
    area = cv2.contourArea(contour)
    raw_perimeter = cv2.arcLength(contour, True)
    if raw_perimeter == 0:
        return "unknown"

    rect = cv2.minAreaRect(contour)
    (_, _), (w, h), _ = rect
    aspect_ratio = max(w, h) / max(min(w, h), 1e-6)

    if aspect_ratio >= 2.4:
        return "capsule"

    # 마스크 경계 잡음은 둘레를 크게 부풀려 원형도를 낮추므로, 볼록 껍질(convex
    # hull) 둘레로 계산해 잡음의 영향을 줄인다.
    hull_perimeter = cv2.arcLength(cv2.convexHull(contour), True)
    circularity = 4 * np.pi * area / (hull_perimeter**2)

    # 같은 종횡비의 매끈한 타원 대비 상대 원형도. 길쭉함 자체로 인한 원형도
    # 저하를 보정하므로, 값이 1에 가까우면 매끈한 원/타원, 많이 낮으면 실제
    # 다각형(오각형, 육각형 등)일 가능성이 높다.
    relative_circularity = circularity / _ideal_ellipse_circularity(aspect_ratio)

    if relative_circularity >= 0.82:
        # 그림자가 마스크를 한쪽으로 살짝 늘리는 경우가 흔해서, 완전한 원도
        # 종횡비가 1.0보다 다소 크게 측정된다. 여유를 두고 판단한다.
        return "round" if aspect_ratio < 1.25 else "oval"

    epsilon = 0.035 * raw_perimeter
    approx = cv2.approxPolyDP(contour, epsilon, True)
    vertices = len(approx)

    if vertices == 3:
        return "triangle"
    if vertices == 4:
        return "diamond" if aspect_ratio > 1.15 else "square"
    if vertices == 5:
        return "pentagon"
    if vertices >= 6:
        return "hexagon"
    return "oval" if aspect_ratio >= 1.15 else "round"


def _closest_named_color(bgr):
    b, g, r = bgr
    best_name, best_dist = None, float("inf")
    for name, (cr, cg, cb) in NAMED_COLORS.items():
        dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if dist < best_dist:
            best_name, best_dist = name, dist
    return best_name


def detect_color(image: np.ndarray, mask: np.ndarray):
    # 가장자리는 안티에일리어싱/그림자가 섞이기 쉬우므로 마스크를 살짝 침식해
    # 알약 안쪽 픽셀만 사용한다.
    eroded = cv2.erode(mask, np.ones((5, 5), np.uint8), iterations=1)
    if cv2.countNonZero(eroded) == 0:
        eroded = mask

    pixels = image[eroded == 255]
    if pixels.size == 0:
        raise ValueError("색상을 추출할 픽셀이 없습니다.")

    # 반사광(하이라이트)과 그림자는 알약 고유의 색이 아니라 조명 때문에
    # 생긴다. 채도가 있고(S) 너무 밝거나 어둡지 않은(V) "본연의 색" 픽셀만
    # 남겨서 흰 반사광 때문에 색이 실제보다 밝게/무채색으로 나오는 것을 막는다.
    hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    s, v = hsv[:, 1], hsv[:, 2]
    body_mask = (s > 35) & (v > 60) & (v < 245)

    body_pixels = pixels[body_mask] if np.count_nonzero(body_mask) > 0.05 * len(pixels) else pixels

    pixels_f32 = np.float32(body_pixels)
    k = min(2, len(pixels_f32))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
    _, labels, centers = cv2.kmeans(pixels_f32, k, None, criteria, 5, cv2.KMEANS_RANDOM_CENTERS)

    counts = np.bincount(labels.flatten())
    dominant_bgr = centers[np.argmax(counts)]
    name = _closest_named_color(dominant_bgr)
    rgb = (int(dominant_bgr[2]), int(dominant_bgr[1]), int(dominant_bgr[0]))
    return name, rgb


def extract_imprint(image: np.ndarray, contour) -> str:
    x, y, w, h = cv2.boundingRect(contour)
    pad = int(0.05 * max(w, h))
    x0, y0 = max(x - pad, 0), max(y - pad, 0)
    x1, y1 = min(x + w + pad, image.shape[1]), min(y + h + pad, image.shape[0])
    crop = image[y0:y1, x0:x1]

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    scaled = cv2.resize(enhanced, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

    config = "--psm 11 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    text = pytesseract.image_to_string(scaled, config=config)
    cleaned = " ".join(text.split())
    return cleaned


def analyze(image_path: str, with_imprint: bool = True) -> list:
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"이미지를 읽을 수 없습니다: {image_path}")

    results = []
    for contour in segment_pills(image):
        mask = build_mask(image.shape, contour)
        shape = classify_shape(contour)
        color_name, color_rgb = detect_color(image, mask)
        entry = {"shape": shape, "color": color_name, "color_rgb": color_rgb, "contour": contour}
        if with_imprint:
            imprint = extract_imprint(image, contour)
            entry["imprint"] = imprint or "(인식 실패 - 흐리거나 대비가 낮음)"
        results.append(entry)
    return results


def diagnose_from_mask(image: np.ndarray, mask: np.ndarray, with_imprint: bool = True) -> dict:
    """사람이 직접 칠한(교정한) 마스크로 모양/색상/각인을 즉시 재계산한다.

    FastSAM 세그멘테이션 없이, 이미 위치가 확정된 마스크만으로 CV 단계
    (classify_shape, detect_color, extract_imprint)만 다시 돌린다. 휴먼 교정
    직후라 경계가 가장 정확해서, 각인 OCR도 여기서 하는 게 가장 신뢰도가 높다.
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("마스크에서 영역을 찾지 못했습니다.")
    contour = max(contours, key=cv2.contourArea)

    shape = classify_shape(contour)
    color_name, color_rgb = detect_color(image, mask)
    result = {"shape": shape, "color": color_name, "color_rgb": color_rgb}
    if with_imprint:
        imprint = extract_imprint(image, contour)
        result["imprint"] = imprint or ""
    return result


def save_detection_overlay(image_path: str, results: list, output_path: str) -> None:
    """탐지된 알약마다 윤곽선과 번호/모양/색을 그려서 이미지로 저장한다."""
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"이미지를 읽을 수 없습니다: {image_path}")

    overlay = image.copy()
    thickness = max(2, round(min(image.shape[:2]) / 400))
    font_scale = max(0.6, min(image.shape[:2]) / 900)

    for i, r in enumerate(results, 1):
        contour = r["contour"]
        x, y, w, h = cv2.boundingRect(contour)
        cv2.drawContours(overlay, [contour], -1, (0, 0, 255), thickness)
        label = f"{i}: {r['shape']}/{r['color']}"
        label_y = y - 10 if y > 30 else y + h + 30
        cv2.putText(
            overlay, label, (x, label_y), cv2.FONT_HERSHEY_SIMPLEX,
            font_scale, (0, 0, 255), thickness,
        )

    cv2.imwrite(output_path, overlay)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/pill_analyze.py <image_path>")
        sys.exit(1)

    path = sys.argv[1]
    if not Path(path).exists():
        print(f"File not found: {path}")
        sys.exit(1)

    results = analyze(path)
    for i, result in enumerate(results, 1):
        print(f"[알약 {i}]")
        print(f"  모양(shape) : {result['shape']}")
        print(f"  색상(color) : {result['color']} {result['color_rgb']}")
        print(f"  각인(imprint): {result['imprint']}")

    overlay_path = str(Path(path).with_name(Path(path).stem + "_detected.png"))
    save_detection_overlay(path, results, overlay_path)
    print(f"\n탐지 결과 이미지 저장됨: {overlay_path}")

    if len(results) > 6:
        print(
            "\n[주의] 알약이 6개를 초과해 인식되었습니다. 이 도구는 알약 2~6개를 "
            "서로 겹치지 않게 놓은 사진에서만 검증되었습니다. 알약 수가 많거나 "
            "무더기로 쌓인 사진은 실제 개수와 다를 수 있으니 결과를 그대로 믿지 마세요."
        )
    elif LAST_RUN_STATS["raw_instances"] >= 15 and LAST_RUN_STATS["kept_pills"] <= 3:
        print(
            "\n[주의] 이미지에서 객체가 많이 감지됐지만(겹침/무더기 가능성) 알약으로 "
            "판별된 것은 소수입니다. 알약이 겹쳐 있다면 펼쳐서 다시 촬영하는 것을 "
            "권장합니다 (이 도구는 알약 2~6개를 겹치지 않게 놓은 사진에서만 검증됨)."
        )
# .\venv\Scripts\python.exe src\pill_analyze.py C:\me\medi\pic\test_batch\pill_10.jpeg