"""서버 쪽 app/pill_analyze.py에 연결하는 추론 래퍼.

export_weights.py가 만든(현재는 train_shape_classifier.py가 직접 저장하는)
가중치 파일을 로드해 컨투어를 입력받아 모양 라벨을 돌려준다.

## 배치 결과 (2026-09-01, 참고사진 118건 검증셋)
규칙 기반 classify_shape() 대비 정확도 78.8% -> 90.7% (오답 21건 개선,
7건 퇴보) - ML/evaluate_shape_classifier.py 실행 결과. 특히 oval/oblong
경계에서 규칙 기반의 약점(oval 재현율 0.41)을 크게 개선했다.

## 폴백 설계
모델 파일이 없거나 로드에 실패하면 predict_shape()가 None을 돌려준다 -
app/pill_analyze.py 쪽에서 None이면 기존 규칙 기반 classify_shape()로
자동 전환하도록 짜여 있다(_get_fastsam_model()과 같은 lazy-load 싱글턴
패턴). 그래서 이 파일 하나 없이 서버를 띄워도(아직 배포 전이거나 롤백
상황) 정상 동작한다 - 모델 파일의 존재 여부 자체가 on/off 스위치다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MODEL_PATH = Path(__file__).resolve().parent / "models" / "shape_classifier.joblib"

_MODEL_BUNDLE = None
_LOAD_ATTEMPTED = False


def _get_model_bundle():
    global _MODEL_BUNDLE, _LOAD_ATTEMPTED
    if _LOAD_ATTEMPTED:
        return _MODEL_BUNDLE
    _LOAD_ATTEMPTED = True
    if not MODEL_PATH.is_file():
        return None
    try:
        import joblib

        _MODEL_BUNDLE = joblib.load(MODEL_PATH)
    except Exception:
        # scikit-learn 미설치, 파일 손상 등 - 규칙 기반 폴백으로 조용히 넘어간다.
        _MODEL_BUNDLE = None
    return _MODEL_BUNDLE


def predict_shape(contour) -> str | None:
    """모양 라벨을 예측한다. 모델을 못 쓰면(파일 없음/로드 실패) None."""
    bundle = _get_model_bundle()
    if bundle is None:
        return None

    from shape_features import features_from_contour

    feats = features_from_contour(contour)
    if feats is None:
        return None

    model, encoder = bundle["model"], bundle["label_encoder"]
    pred = model.predict(feats.reshape(1, -1))
    return encoder.inverse_transform(pred)[0]
