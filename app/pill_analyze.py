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
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ML/이 있으면(로컬 학습 + 배포된 가중치 파일) 학습 기반 모양 분류기를 쓰고,
# 없거나 로드 실패하면 규칙 기반 classify_shape()로 조용히 폴백한다 - ML/
# 자체가 서버에 아직 안 올라갔을 수도 있어서(ImportError) try/except로
# 감싼다. 2026-09-01 검증(참고사진 118건): 규칙 기반 대비 정확도
# 78.8% -> 90.7% (ML/evaluate_shape_classifier.py 참고).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ML"))
try:
    from infer_shape import predict_shape as _predict_shape_ml
except ImportError:
    _predict_shape_ml = None

# 각인(imprint) 인식도 같은 패턴 - ML/models/imprint_crnn.pt가 있으면 그걸
# 우선 쓰고, 없거나 예측 실패하면 기존 EasyOCR 방식(extract_imprint)으로
# 폴백한다. 검증 결과(2026-09-03, ML/IMPRINT/evaluate_imprint_model.py,
# 검증셋 2,300건): 정확 일치율 EasyOCR 21.3% -> CRNN 75.7%, 인식 실패율
# 50.7% -> 0.0%.
try:
    from infer_imprint import predict_imprint as _predict_imprint_ml
except ImportError:
    _predict_imprint_ml = None

# 섀도 모드 로그: 서빙되는 값(ML)은 그대로 두고, 규칙 기반 결과도 같이 계산해서
# 둘이 일치하는지만 기록한다. ML/README.md의 검증 미해결 리스크(90.7%는
# 스튜디오 참고사진 기준이라 실사용자 사진에서도 우위가 유지되는지 확인
# 안 됨, hexagon/pentagon/triangle/semicircle 같은 소수 클래스는 표본 1~7건
# 뿐이라 특히 못 믿음) 때문에, 실사용 로그가 쌓이기 전까지 ML로 완전 전환하지
# 않고 이 로그로 실측 우위를 확인한다. storage/ml_confirmations.jsonl(사용자가
# 확정한 진짜 정답)과 나중에 대조하면 실제 정확도 비교도 가능하다.
_SHADOW_LOG_PATH = Path(__file__).resolve().parent.parent / "storage" / "ml_shadow.jsonl"
_IMPRINT_SHADOW_LOG_PATH = Path(__file__).resolve().parent.parent / "storage" / "imprint_shadow.jsonl"


def _log_shadow_comparison(ml_shape: str, rule_shape: str) -> None:
    try:
        _SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ml_shape": ml_shape,
            "rule_shape": rule_shape,
            "match": ml_shape == rule_shape,
        }
        with open(_SHADOW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 로깅 실패가 실제 서빙(모양 판정 응답)을 막으면 안 된다


def _log_imprint_shadow(ml_text: str, easyocr_text: str) -> None:
    try:
        _IMPRINT_SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ml_text": ml_text,
            "easyocr_text": easyocr_text,
            "match": ml_text == easyocr_text,
        }
        with open(_IMPRINT_SHADOW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 로깅 실패가 실제 서빙(각인 응답)을 막으면 안 된다

NAMED_COLORS = {
    # "ivory"는 DB(product.color_class1/2)에 단 1건도 없는 값이라(MFDS 원본
    # 데이터에 사실상 안 쓰임 - 실측 확인) 여기 팔레트에 있으면 흰색에
    # 가까운 알약이 최근접 매칭으로 ivory에 걸려 검색 결과가 0건이 되는
    # 버그가 생긴다. 그래서 일부러 뺐다 - 흰색 계열은 전부 "white"로
    # 흡수되게 한다. "clear"도 같은 이유(17건뿐 + 투명은 단일 RGB로 표현
    # 자체가 애매함)로 뺐다.
    #
    # 2026-09-02: 손으로 고른 이상적 색상표에서, product.color_class1(정답
    # 라벨) + storage/drug_image/(참고사진) 기준 실측 중앙값으로 재보정했다
    # (ML/calibrate_color.py, 색상당 최대 300장 샘플). 계기: item_seq
    # 196000011(DB 라벨 yellow)의 실제 추출 RGB(238,227,151)가 옛 yellow
    # 기준색(255,221,51)보다 옛 pink 기준색(255,175,190)에 유클리드 거리상
    # 더 가까워 "pink"로 오분류되는 게 실측 확인됨 - 파스텔톤/연한 색이 특히
    # 취약했다. 재보정 후 이 케이스는 yellow로 정상 분류됨(거리 830 vs 2945).
    "white": (235, 237, 235),
    "yellow": (228, 206, 134),
    "orange": (234, 181, 113),
    "pink": (232, 174, 161),
    "red": (158, 48, 40),
    "brown": (118, 56, 41),
    "purple": (111, 60, 125),
    "blue": (100, 182, 205),
    "green": (122, 193, 122),
    "gray": (185, 192, 181),
    "black": (44, 41, 39),
}


_FASTSAM_MODEL = None
# 다른 학습 모델(ML/models/shape_classifier.joblib, imprint_crnn.pt)과 같은
# 위치에 모아둔다 - 예전엔 저장소 루트에 따로 있었다.
_FASTSAM_MODEL_PATH = Path(__file__).resolve().parent.parent / "ML" / "models" / "FastSAM-s.pt"


def _get_fastsam_model():
    global _FASTSAM_MODEL
    if _FASTSAM_MODEL is None:
        from ultralytics import FastSAM

        _FASTSAM_MODEL = FastSAM(str(_FASTSAM_MODEL_PATH))
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


def _quad_is_rectangular(approx) -> bool:
    """approxPolyDP로 나온 4개 꼭짓점의 내각이 전부 90도 근처인지 본다.

    사각형/장방형은 내각이 90도에 가깝고, 마름모는 마주보는 꼭짓점 쌍의
    내각이 90도에서 크게 벗어난다(하나는 예각, 하나는 둔각) - 종횡비만으로는
    이 둘을 구분할 수 없어서 각도를 직접 봐야 한다.
    """
    pts = approx.reshape(-1, 2).astype(np.float64)
    if len(pts) != 4:
        return True
    for i in range(4):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % 4]
        v1, v2 = a - b, c - b
        cos_angle = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
        angle = np.degrees(np.arccos(np.clip(cos_angle, -1, 1)))
        if not (65 <= angle <= 115):
            return False
    return True


def classify_shape(contour) -> str:
    """알약 윤곽을 정해진 모양 어휘(식약처 API의 DRUG_SHAPE 값과 1:1로
    맞춘 것) 중 하나로 분류한다. 어느 것에도 뚜렷이 들어맞지 않으면
    "other"(기타)로 분류한다 - 억지로 아무 모양이나 갖다 붙이지 않는다.

    알려진 한계: 육각형/팔각형/오각형/반원형은 원과 원형도가 비슷해서
    (변이 많을수록 원에 가까워짐) 아래 원/타원 판정에 먼저 걸려 원/타원으로
    분류될 수 있다. "변 길이가 고른 다각형만 예외 처리" 방식을 시도했으나,
    실제 원형 알약 사진에서 세그멘테이션 잡음을 진짜 다각형으로 오판하는
    회귀가 확인되어 되돌렸다 - 원/타원이 전체의 67%로 가장 흔한 케이스라
    이 회귀 위험을 감수할 수 없었다. 실사진 검증 데이터가 쌓이기 전까지는
    이 한계를 그대로 둔다 (전체 대비 이 4개 모양은 약 1.4%).
    """
    area = cv2.contourArea(contour)
    raw_perimeter = cv2.arcLength(contour, True)
    if raw_perimeter == 0:
        return "other"

    rect = cv2.minAreaRect(contour)
    (_, _), (w, h), _ = rect
    aspect_ratio = max(w, h) / max(min(w, h), 1e-6)

    # "capsule" 카테고리는 일부러 안 쓴다 - 실측 확인: MFDS 원본 데이터는
    # 캡슐형 약도 DRUG_SHAPE을 "장방형"이나 "타원형"으로 등록한다("캡슐형"
    # 값 자체가 원본에 없음). 그래서 여기서 종횡비만으로 capsule로 조기
    # 판정하면 DB에 아예 없는 값이라 검색이 항상 0건이 된다(ivory 색상과
    # 동일한 패턴의 버그). 매끈한 캡슐형 윤곽은 원형도가 높아 아래
    # round/oval/oblong 분기를 그대로 타므로(검증: 종횡비 1.5~8 전 구간에서
    # 스타디움의 상대 원형도가 0.82 문턱을 항상 넘음), 자연스럽게 타원/장방형
    # 중 하나로 갈린다 - MFDS의 실제 라벨링 방식과 일치한다.

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
        if aspect_ratio < 1.25:
            return "round"
        # 장방형(스타디움 - 직선 옆면 + 반원 양끝)은 원형도만으로는 타원과
        # 구분이 안 된다(실측 확인: 종횡비 2.19에서 완벽한 장방형의 상대
        # 원형도가 오히려 이상적 타원보다 높게 나옴). 대신 "윤곽 면적 /
        # bounding box 면적" 비율을 쓰면 확실히 갈린다 - 타원은 종횡비와
        # 무관하게 항상 π/4(~0.785)로 일정하지만, 장방형은 종횡비가
        # 커질수록 1에 가까워진다(옆면이 직선이라 박스를 거의 다 채움).
        # 두 이론값의 중간을 기준선으로 삼는다. 합성 도형(매끈함+세그멘테이션
        # 잡음 흉내) 검증: 종횡비 1.25~2.3 전 구간에서 오분류 0건.
        hull_area = cv2.contourArea(cv2.convexHull(contour))
        bbox_area = w * h
        if bbox_area > 0:
            ellipse_ratio = np.pi / 4
            stadium_ratio = 1 - (1 - np.pi / 4) / aspect_ratio
            if hull_area / bbox_area >= (ellipse_ratio + stadium_ratio) / 2:
                return "oblong"
        return "oval"

    epsilon = 0.035 * raw_perimeter
    approx = cv2.approxPolyDP(contour, epsilon, True)
    vertices = len(approx)

    if vertices == 3:
        return "triangle"
    if vertices == 4:
        if _quad_is_rectangular(approx):
            return "oblong" if aspect_ratio > 1.15 else "square"
        return "diamond"
    if vertices == 5:
        return "pentagon"
    if vertices == 6:
        return "hexagon"
    if vertices == 8:
        return "octagon"
    # 7각형 등 애매하게 남는 다각형이나 근사가 잘 안 되는 경우는 억지로
    # 육각/팔각에 끼워 맞추지 않고 기타로 둔다.
    return "other"


def _classify_shape(contour) -> tuple[str, float | None]:
    """모양 판정 진입점. (모양, 신뢰도)를 돌려준다 - 신뢰도는 ML 서빙일 때만
    0~1 값(RandomForest predict_proba), 규칙 기반 폴백이면 None(규칙 기반은
    확률 개념이 없음).

    ML 모델(ML/models/shape_classifier.joblib)이 있으면 그걸 서빙하고,
    없거나 실패하면 규칙 기반 classify_shape()로 폴백한다. ML이 성공한
    경우도 규칙 기반을 같이 계산해 storage/ml_shadow.jsonl에 일치 여부를
    남긴다(섀도 모드 - 서빙 결과에는 영향 없음, _log_shadow_comparison 주석 참고)."""
    rule_result = classify_shape(contour)
    if _predict_shape_ml is not None:
        predicted, confidence = _predict_shape_ml(contour)
        if predicted is not None:
            _log_shadow_comparison(predicted, rule_result)
            return predicted, confidence
    return rule_result, None


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


_OCR_ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
# 이 밑이면 오탐 가능성이 높다고 보고 버린다. 실측: 맞는 경우는 94~99%대가
# 흔하지만, "8"을 "0"으로 틀리면서 93%대를 찍는 경우도 확인됨 - 신뢰도만
# 으로는 이런 케이스를 완전히 못 거른다. 표본이 적어 임계값을 더 올려서
# 맞추려 하기보다(과최적화 위험), 명백히 낮은 신뢰도만 걸러내는 선으로
# 둔다. 어차피 틀린 값이 섞여도 DB 매칭은 LIKE 실패 -> 폭넓은 후보로
# 완만하게 떨어질 뿐, 엉뚱한 약으로 잘못 특정되진 않는다.
_OCR_CONFIDENCE_THRESHOLD = 0.7

_OCR_READER = None


def _get_ocr_reader():
    global _OCR_READER
    if _OCR_READER is None:
        import easyocr

        _OCR_READER = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _OCR_READER


def _detect_divider_x(gray: np.ndarray):
    """알약 중앙을 가로지르는 분할선(스코어라인)의 x좌표를 찾는다. 없으면 None.

    분할선은 알약 지름의 상당 부분을 가로지르는 거의 수직인 직선이라
    Hough 직선 검출로 잘 잡힌다 (실측: 분할선 있는 알약은 검출됨, 없는
    알약은 0개 - 오검출이 거의 없다).
    """
    h, _ = gray.shape
    edges = cv2.Canny(gray, 30, 90)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=60, minLineLength=int(h * 0.5), maxLineGap=10
    )
    if lines is None:
        return None

    xs = []
    for x1, y1, x2, y2 in lines:
        angle = abs(np.degrees(np.arctan2(int(y2) - int(y1), int(x2) - int(x1))))
        if 80 <= angle <= 100:  # 거의 수직인 것만 분할선 후보로 본다
            xs.append((int(x1) + int(x2)) / 2)
    if not xs:
        return None
    return int(np.median(xs))


def _ocr_one_side(region: np.ndarray) -> str:
    """이미 분할선 기준으로 잘라둔 한쪽(글자 하나짜리로 가정)을 읽는다.
    이미 위치를 알고 있으니 텍스트 탐지 단계는 건너뛰고(recognize) 바로
    인식만 한다 - readtext보다 이 경우 더 정확하다(실측 확인)."""
    if region.size == 0:
        return ""
    reader = _get_ocr_reader()
    results = reader.recognize(region, allowlist=_OCR_ALLOWLIST)
    if not results:
        return ""
    _, text, conf = results[0]
    return text if text and conf >= _OCR_CONFIDENCE_THRESHOLD else ""


def extract_imprint(image: np.ndarray, contour) -> tuple[list[str], float | None]:
    """알약 표면의 양각 각인을 읽어, (우선순위 순서의 후보 리스트, ML 신뢰도)를
    돌려준다. 신뢰도는 ML 경로가 서빙됐을 때만 0~1 값이고, EasyOCR 폴백이면
    None이다(ML 자체를 못 써서 비교 대상이 없다는 뜻 - "신뢰도가 0"과는 다르다).

    ML/models/imprint_crnn.pt(CRNN)가 있으면 그 예측을 최우선 후보로 서빙하고
    (모양의 _classify_shape()와 같은 패턴), EasyOCR 결과는 섀도 로그로만 같이
    남긴다(storage/imprint_shadow.jsonl). ML이 없거나 예측 실패 시에만 아래
    EasyOCR 후보 목록을 그대로 서빙한다.

    신뢰도를 서버가 임의 임계값으로 잘라서 숨기지 않고 그대로 응답에 실어
    보낸다 - "마크"(로고/기호)나 각인 없는 뒷면처럼 학습에서 제외했던 입력엔
    모델이 항상 뭔가 그럴듯하게 지어내는 게 실측 확인됐다(2026-09-03,
    item_seq 196500051/195900001). 이런 경우를 서버가 완벽히 걸러낼 수 없으니,
    신뢰도를 FE로 넘겨서 낮을 때 사용자 교정을 유도하는 판단은 FE/UX가 하게
    한다.

    EasyOCR 경로의 후보 순서는 [통짜, 합친 것(왼쪽+분할선+오른쪽), 왼쪽만,
    오른쪽만]. 통짜는 분할선 오검출에 안전한 최우선 후보(_imprint_candidates_from_gray
    참고), 합친 것은 정보량이 많지만(둘 다 맞아야 함) 하나라도 틀리면 전체가
    어긋난다. 한쪽만 맞춘 후보는 정보는 적어도 더 신뢰할 수 있다. 실제 DB
    매칭이 앞에서부터 순서대로 시도해보도록 이 순서를 그대로 유지해야 한다.
    결과는 DB(product.print_front/print_back)에 실제 저장된 형식과 맞춰
    "분할선" 토큰을 그대로 끼워 넣는다 - 그래야 LIKE 매칭으로 DB의
    "V분할선8"과 바로 맞아떨어진다. 아무것도 못 읽으면 빈 리스트를 돌려준다 -
    틀린 값을 억지로 채우는 것보다 모르면 모른다고 하는 게 낫다.
    """
    x, y, w, h = cv2.boundingRect(contour)
    pad = int(0.05 * max(w, h))
    x0, y0 = max(x - pad, 0), max(y - pad, 0)
    x1, y1 = min(x + w + pad, image.shape[1]), min(y + h + pad, image.shape[0])
    crop = image[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    if _predict_imprint_ml is not None:
        predicted, confidence = _predict_imprint_ml(gray)
        if predicted:
            easyocr_candidates = _imprint_candidates_from_gray(gray)
            _log_imprint_shadow(predicted, easyocr_candidates[0] if easyocr_candidates else "")
            return [predicted], confidence

    return _imprint_candidates_from_gray(gray), None


def _imprint_candidates_from_gray(gray: np.ndarray) -> list[str]:
    """이미 잘라낸(각인 영역만 남은) 흑백 크롭에서 후보 목록을 뽑는다.
    extract_imprint()의 실제 로직 본체 - ML/build_dataset.py가 만드는 크롭도
    같은 bounding-rect+5% 패딩 규칙이라, 그쪽에서 저장한 크롭 PNG를 그대로
    이 함수에 넣으면 evaluate_imprint_model.py 같은 도구가 extract_imprint()와
    똑같은 EasyOCR 경로를 재사용할 수 있다(원본 이미지+contour 없이도)."""
    gh, gw = gray.shape

    # 통짜 인식은 분할선 유무와 상관없이 항상 먼저 시도한다. 원래는 분할선이
    # 없을 때만 이 경로를 탔는데, 실측 확인(item_seq 197100081) 결과 진짜
    # 분할선이 없는 두 글자짜리 각인(예: "KS")에서도 _detect_divider_x가
    # 글자 획을 분할선으로 오검출해서 억지로 좌/우로 쪼개는 사례가 있었다 -
    # "KS"(신뢰도 0.997로 깨끗이 읽힘)가 "분할선KS"로 나와 DB 매칭(정확한
    # "KS")이 아예 안 맞게 되는 문제였다. 통짜 결과를 항상 최우선 후보로 두면
    # 이런 오검출에 안전하다 - 진짜 분할선(홈)이 있는 경우는 그 홈이 글자
    # 연속성을 끊어서 통짜 인식이 보통 실패하므로(원래 이 함수가 좌/우 분리
    # 방식을 쓰는 이유), 실전에서 이 우선순위가 뒤바뀌는 부작용은 적다.
    reader = _get_ocr_reader()
    found = reader.readtext(gray, allowlist=_OCR_ALLOWLIST)
    best = max(found, key=lambda r: r[2], default=None)
    whole_text = best[1] if best and best[1] and best[2] >= _OCR_CONFIDENCE_THRESHOLD else ""

    divider_x = _detect_divider_x(gray)
    if divider_x is None:
        return [whole_text] if whole_text else []

    margin = int(gw * 0.06)
    left_text = _ocr_one_side(gray[:, : max(divider_x - margin, 0)])
    right_text = _ocr_one_side(gray[:, min(divider_x + margin, gw) :])

    candidates = []
    if whole_text:
        candidates.append(whole_text)
    if left_text and right_text:
        candidates.append(f"{left_text}분할선{right_text}")
    if left_text:
        candidates.append(f"{left_text}분할선")
    if right_text:
        candidates.append(f"분할선{right_text}")
    return candidates


def analyze(image_path: str, with_imprint: bool = True) -> list:
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"이미지를 읽을 수 없습니다: {image_path}")

    results = []
    for contour in segment_pills(image):
        mask = build_mask(image.shape, contour)
        shape, shape_confidence = _classify_shape(contour)
        color_name, color_rgb = detect_color(image, mask)
        entry = {
            "shape": shape,
            "shape_confidence": shape_confidence,
            "color": color_name,
            "color_rgb": color_rgb,
            "contour": contour,
        }
        if with_imprint:
            candidates, imprint_confidence = extract_imprint(image, contour)
            entry["imprint"] = candidates[0] if candidates else "(인식 실패 - 흐리거나 대비가 낮음)"
            # 우선순위 순서([합친것, 왼쪽만, 오른쪽만]) 그대로 보존 - DB 매칭 쪽에서
            # 이 순서대로 하나씩 시도하는 건 추후 작업.
            entry["imprint_candidates"] = candidates
            # ML 경로일 때만 0~1 값, EasyOCR 폴백이면 None(비교 대상 자체가 없다는 뜻).
            entry["imprint_confidence"] = imprint_confidence
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

    shape, shape_confidence = _classify_shape(contour)
    color_name, color_rgb = detect_color(image, mask)
    result = {"shape": shape, "shape_confidence": shape_confidence, "color": color_name, "color_rgb": color_rgb}
    if with_imprint:
        candidates, imprint_confidence = extract_imprint(image, contour)
        result["imprint"] = candidates[0] if candidates else ""
        result["imprint_candidates"] = candidates
        result["imprint_confidence"] = imprint_confidence
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