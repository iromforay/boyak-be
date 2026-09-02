"""이미지/마스크에서 알약 윤곽선을 찾고, 모양 분류에 쓸 특징 벡터로 바꾼다.

app/pill_analyze.py의 classify_shape()가 쓰는 기하학적 값(종횡비, 상대
원형도, 면적/bbox 비율, 꼭짓점 수)을 그대로 재사용하고, 여기에 Hu moments(7개,
회전/크기/이동에 불변이라 모양 분류에 잘 맞음)를 더해 분류기가 스스로 경계를
찾을 재료를 넉넉히 준다. 학습(train_shape_classifier.py)과 서버 추론
(infer_shape.py) 양쪽에서 같은 특징을 써야 하므로 이 모듈 하나로 통일한다.
"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pill_analyze import segment_pills  # noqa: E402

FEATURE_NAMES = [
    "aspect_ratio",
    "relative_circularity",
    "area_bbox_ratio",
    "vertices",
    *[f"hu{i}" for i in range(7)],
]


def _ideal_ellipse_circularity(aspect_ratio: float) -> float:
    a, b = aspect_ratio, 1.0
    perimeter = np.pi * (3 * (a + b) - np.sqrt((3 * a + b) * (a + 3 * b)))
    area = np.pi * a * b
    return 4 * np.pi * area / (perimeter**2)


def contour_from_mask(mask: np.ndarray):
    """이미 이진화된 마스크(사용자 교정 마스크 등)에서 가장 큰 컨투어를 뽑는다."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    return max(contours, key=cv2.contourArea)


def _background_color(image: np.ndarray) -> np.ndarray:
    """MFDS 참고사진은 항상 네 모서리가 배경(측정용 격자)이라, 그 색을 대표값으로 쓴다."""
    h, w = image.shape[:2]
    size = max(1, min(h, w) // 20)
    patches = [
        image[:size, :size],
        image[:size, -size:],
        image[-size:, :size],
        image[-size:, -size:],
    ]
    return np.mean(np.concatenate([p.reshape(-1, 3) for p in patches]), axis=0)


def contours_from_reference_image(image: np.ndarray, bg_color_distance_threshold: float = 25.0) -> list:
    """MFDS 참고사진에서 알약 컨투어를 전부 뽑는다.

    처음엔 Otsu 이진화(배경 단색 가정)로 직접 분리했는데, 실제 참고사진을
    열어보니 그 가정이 깨져 있었다 - 측정용 격자 배경 + 같은 약의 사진이
    2장(앞면/뒷면) 나란히 붙어있는 형식이라, 41%는 컨투어를 아예 못 찾고
    나머지도 격자선 때문에 컨투어가 반쪽만 잡히는 경우가 있었다(실제 이미지로
    확인).

    대신 실사용자 사진에도 쓰는 app.pill_analyze.segment_pills()(FastSAM
    기반)를 그대로 재사용한다 - 배경 가정에 의존하지 않고 "물체"를 직접
    찾아내므로 격자 배경/다중 객체에 훨씬 강하다. 참고사진에 알약이 2장
    있으면 둘 다 검출되는데, 같은 물리적 알약의 앞/뒷면이라 모양은 동일하므로
    둘 다 유효한 학습 샘플로 쓴다(호출부에서 각각 별도 샘플로 추가).

    다만 격자 배경의 칸 하나하나가 닫힌 사각형처럼 보여서 FastSAM이 이걸
    별개 물체로 오검출하는 걸 실측으로 확인했다(모양/색 필터를 다 통과함 -
    격자 칸도 그럴듯한 사각형이라). 알약은 배경과 색이 뚜렷이 다르지만 격자
    칸은 배경색 그대로이므로, 후보 영역의 평균 색이 배경색과 비슷하면
    (L2 거리 threshold 미만) 버린다.
    """
    bg_color = _background_color(image)
    contours = []
    for contour in segment_pills(image):
        mask = mask_from_contour(contour, image.shape)
        mean_color = cv2.mean(image, mask=mask)[:3]
        if np.linalg.norm(np.array(mean_color) - bg_color) < bg_color_distance_threshold:
            continue
        contours.append(contour)
    return contours


def mask_from_contour(contour, image_shape) -> np.ndarray:
    mask = np.zeros(image_shape[:2], dtype=np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, thickness=cv2.FILLED)
    return mask


def augment_mask(mask: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """세그멘테이션 잡음을 흉내낸 마스크 변형을 만든다 (학습셋 증강용).

    회전/크기 변형은 안 쓴다 - Hu moments를 비롯한 지금 특징들이 이미
    회전/크기/이동에 불변이라 그런 변형은 피처 벡터를 거의 안 바꾼다(붙여도
    새 정보가 안 생김). 대신 실제 사진에서 흔한 실패 요인인 "경계가 살짝
    거칠거나 뭉개지는" 상황(침식/팽창 + 블러 재이진화)을 흉내내서, 분류기가
    그런 잡음에도 안정적으로 판정하도록 학습시킨다.
    """
    kernel = np.ones((3, 3), np.uint8)
    out = mask.copy()
    for _ in range(int(rng.integers(1, 3))):
        if rng.random() > 0.5:
            out = cv2.dilate(out, kernel, iterations=1)
        else:
            out = cv2.erode(out, kernel, iterations=1)
    out = cv2.GaussianBlur(out, (5, 5), 0)
    _, out = cv2.threshold(out, 127, 255, cv2.THRESH_BINARY)
    return out


def features_from_contour(contour) -> np.ndarray | None:
    """app/pill_analyze.py의 classify_shape()와 같은 기하학적 값 + Hu moments."""
    area = cv2.contourArea(contour)
    raw_perimeter = cv2.arcLength(contour, True)
    if raw_perimeter == 0 or area == 0:
        return None

    rect = cv2.minAreaRect(contour)
    (_, _), (w, h), _ = rect
    if min(w, h) <= 0:
        return None
    aspect_ratio = max(w, h) / min(w, h)

    hull = cv2.convexHull(contour)
    hull_area = cv2.contourArea(hull)
    hull_perimeter = cv2.arcLength(hull, True)
    circularity = 4 * np.pi * area / (hull_perimeter**2)
    relative_circularity = circularity / _ideal_ellipse_circularity(aspect_ratio)

    bbox_area = w * h
    area_bbox_ratio = hull_area / bbox_area if bbox_area > 0 else 0

    epsilon = 0.035 * raw_perimeter
    approx = cv2.approxPolyDP(contour, epsilon, True)
    vertices = len(approx)

    hu = cv2.HuMoments(cv2.moments(contour)).flatten()
    # Hu moments는 값의 스케일이 극단적으로 작아서(로그 스케일에 가까움) 그대로
    # 쓰면 트리 기반 모델에서도 분할 기준을 잡기 어렵다 - 부호를 보존한 로그
    # 스케일링으로 다른 특징들과 비슷한 범위로 맞춘다.
    hu_log = np.sign(hu) * np.log10(np.abs(hu) + 1e-30)

    return np.array([aspect_ratio, relative_circularity, area_bbox_ratio, float(vertices), *hu_log])
