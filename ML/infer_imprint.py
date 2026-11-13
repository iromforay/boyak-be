"""서버 쪽 app/pill_analyze.py에 연결하는 각인(imprint) 인식 추론 래퍼.

ML/IMPRINT/train_imprint_model.py(CRNN+CTC)가 만든 가중치(ML/models/imprint_crnn.pt)를
로드해, 흑백 크롭 이미지 하나를 입력받아 예측 문자열을 돌려준다.

## 검증 결과 (2026-09-03, ML/IMPRINT/evaluate_imprint_model.py)
검증셋 2,300건 기준 정확 일치율 EasyOCR 21.3% -> CRNN 75.7%, 인식 실패율
50.7% -> 0.0%. 상세: ML/IMPRINT/eval_results/eval_20260903_112830.json

## 폴백 설계
모델 파일이 없거나 로드에 실패하면 predict_imprint()가 None을 돌려준다 -
app/pill_analyze.py 쪽에서 None이면 기존 EasyOCR 방식(extract_imprint)으로
자동 전환하도록 짜여 있다(ML/infer_shape.py와 같은 lazy-load 싱글턴 패턴).
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "IMPRINT"))

MODEL_PATH = Path(__file__).resolve().parent / "models" / "imprint_crnn.pt"

_MODEL = None
_VOCAB = None
_BLANK_IDX = None
_IMG_SIZE = None  # (width, height)
_LOAD_ATTEMPTED = False


def _get_model():
    global _MODEL, _VOCAB, _BLANK_IDX, _IMG_SIZE, _LOAD_ATTEMPTED
    if _LOAD_ATTEMPTED:
        return _MODEL
    _LOAD_ATTEMPTED = True
    if not MODEL_PATH.is_file():
        return None
    try:
        import torch
        from train_imprint_model import CRNN, IMG_HEIGHT, IMG_WIDTH, NUM_CLASSES, BLANK_IDX

        checkpoint = torch.load(MODEL_PATH, map_location="cpu", weights_only=True)
        model = CRNN(NUM_CLASSES)
        model.load_state_dict(checkpoint["model_state"])
        model.eval()

        _MODEL = model
        _VOCAB = checkpoint["vocab"]
        _BLANK_IDX = BLANK_IDX
        _IMG_SIZE = (IMG_WIDTH, IMG_HEIGHT)
    except Exception:
        # 가중치 파일 손상, torch 미설치 등 - 규칙 기반(EasyOCR) 폴백으로 조용히 넘어간다.
        _MODEL = None
    return _MODEL


def _ctc_greedy_decode(ids: list) -> list:
    decoded = []
    prev = None
    for i in ids:
        if i != prev and i != _BLANK_IDX:
            decoded.append(i)
        prev = i
    return decoded


def predict_imprint(gray_crop: np.ndarray) -> tuple[str, float] | tuple[None, float]:
    """흑백 크롭(각인 영역만 남은 이미지) -> (예측 문자열, 신뢰도 0~1).
    모델을 못 쓰거나(파일 없음/로드 실패) 빈 문자열로 디코딩되면 (None, 0.0).

    "마크"(로고/기호)나 각인 없는 뒷면처럼 학습 데이터에서 아예 제외했던
    입력에 대해서도 모델은 항상 뭔가 그럴듯한 글자를 지어낸다(실측 확인,
    2026-09-03) - 그런 경우를 걸러낼 방법이 신뢰도뿐이라 계산해서 같이
    돌려준다. 값은 실제로 채택된 각 글자 시점의 softmax 확률 평균이라,
    "이 예측을 모델이 얼마나 확신하는가"를 뜻한다 - 이 값을 낮다고 서버가
    임의로 잘라버리지 않고 그대로 FE에 넘겨서, 낮은 신뢰도인 경우 사용자
    교정을 유도하는 판단은 FE/UX 쪽에 맡긴다."""
    model = _get_model()
    if model is None:
        return None, 0.0

    import cv2
    import torch

    resized = cv2.resize(gray_crop, _IMG_SIZE, interpolation=cv2.INTER_AREA)
    tensor = torch.from_numpy((resized.astype("float32") / 255.0 - 0.5) / 0.5).unsqueeze(0).unsqueeze(0)
    with torch.no_grad():
        logits = model(tensor)
        probs = logits.softmax(dim=-1)[0]  # (T, num_classes)
        pred_ids = probs.argmax(dim=-1)
        max_probs = probs.max(dim=-1).values
        pred_ids_list = pred_ids.tolist()
        max_probs_list = max_probs.tolist()

    decoded_ids = []
    decoded_confidences = []
    prev = None
    for i, p in zip(pred_ids_list, max_probs_list):
        if i != prev and i != _BLANK_IDX:
            decoded_ids.append(i)
            decoded_confidences.append(p)
        prev = i

    if not decoded_ids:
        return None, 0.0
    text = "".join(_VOCAB[i] for i in decoded_ids)
    confidence = sum(decoded_confidences) / len(decoded_confidences)
    return text, confidence
