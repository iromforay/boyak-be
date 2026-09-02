"""검증셋으로 학습된 모양 분류기의 성능을 평가한다.

train_shape_classifier.py가 남긴 test_groups.json(검증셋으로 빠진 그룹id
목록)을 그대로 재사용해서, app/pill_analyze.py의 규칙 기반 classify_shape()와
방금 학습한 ML 모델을 **정확히 같은 검증셋**에 나란히 돌려 비교한다. 증강
샘플은 학습 때만 쓰는 것이라 여기선 원본(1건당 1개)만 평가한다.

규칙 기반보다 나아지지 않으면 교체하지 않는다 - 이 스크립트의 결과가 그
판단 근거다.

주의: reference_images.csv/user_confirmations.csv의 행 순서가 마지막 학습
이후 바뀌면(재생성 등) 그룹id가 어긋난다 - 평가 전에 재학습부터 다시 하는
게 안전하다.

사용법 (venv의 scikit-learn이 필요해서 반드시 venv 파이썬으로 실행):
  venv/Scripts/python.exe ML/evaluate_shape_classifier.py
"""

import csv
import json
import sys
from pathlib import Path

import cv2
import joblib
import numpy as np
from sklearn.metrics import classification_report

from shape_features import contours_from_reference_image, features_from_contour

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.pill_analyze import classify_shape  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
REFERENCE_CSV = DATA_DIR / "reference_images.csv"
USER_CONFIRMATIONS_CSV = DATA_DIR / "user_confirmations.csv"
TEST_GROUPS_PATH = DATA_DIR / "test_groups.json"
MODEL_PATH = Path(__file__).resolve().parent / "models" / "shape_classifier.joblib"


def _collect_test_samples(csv_path: Path, group_offset: int, test_groups: set, is_reference: bool):
    """train_shape_classifier.py의 그룹id 부여 순서를 그대로 재현한다 -
    성공한 행에만 순번을 매기므로(실패한 행은 건너뛰고 카운터를 안 올림),
    같은 순서로 다시 돌려야 그때 그 test_groups와 정확히 대응된다."""
    if not csv_path.is_file():
        return []
    samples = []
    group = group_offset
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if is_reference:
                image = cv2.imread(row["image_path"])
                if image is None:
                    continue
                contours = contours_from_reference_image(image)
                if not contours:
                    continue
                contour = max(contours, key=cv2.contourArea)
            else:
                mask = cv2.imread(row["mask_path"], cv2.IMREAD_GRAYSCALE)
                if mask is None:
                    continue
                from shape_features import contour_from_mask

                contour = contour_from_mask(mask)
                if contour is None:
                    continue

            if group in test_groups:
                samples.append((contour, row["shape"]))
            group += 1
    return samples


def evaluate() -> None:
    if not MODEL_PATH.is_file():
        print(f"모델이 없습니다: {MODEL_PATH} - 먼저 train_shape_classifier.py를 실행하세요.")
        return
    if not TEST_GROUPS_PATH.is_file():
        print(f"검증셋 그룹 정보가 없습니다: {TEST_GROUPS_PATH} - 먼저 train_shape_classifier.py를 실행하세요.")
        return

    bundle = joblib.load(MODEL_PATH)
    model, encoder = bundle["model"], bundle["label_encoder"]
    test_groups = set(json.load(open(TEST_GROUPS_PATH, encoding="utf-8")))

    print(f"검증셋 재구성 중 (그룹 {len(test_groups)}개)...")
    samples = _collect_test_samples(REFERENCE_CSV, group_offset=0, test_groups=test_groups, is_reference=True)
    samples += _collect_test_samples(
        USER_CONFIRMATIONS_CSV, group_offset=1_000_000, test_groups=test_groups, is_reference=False
    )
    print(f"재구성된 검증 샘플: {len(samples)}건 (train_shape_classifier.py 실행 시점과 같아야 함)")
    if not samples:
        print("검증 샘플을 못 찾았습니다 - reference_images.csv가 재생성되지 않았는지 확인하세요.")
        return

    y_true = [s[1] for s in samples]
    rule_pred = [classify_shape(s[0]) for s in samples]
    ml_pred_encoded = model.predict(np.array([features_from_contour(s[0]) for s in samples]))
    ml_pred = list(encoder.inverse_transform(ml_pred_encoded))

    labels = sorted(set(y_true) | set(rule_pred) | set(ml_pred))

    print("\n=== 규칙 기반 (classify_shape) ===")
    print(classification_report(y_true, rule_pred, labels=labels, zero_division=0))

    print("=== ML (RandomForest) ===")
    print(classification_report(y_true, ml_pred, labels=labels, zero_division=0))

    rule_correct = sum(1 for t, r in zip(y_true, rule_pred) if t == r)
    ml_correct = sum(1 for t, m in zip(y_true, ml_pred) if t == m)
    both_wrong = sum(1 for t, r, m in zip(y_true, rule_pred, ml_pred) if t != r and t != m)
    ml_fixed = sum(1 for t, r, m in zip(y_true, rule_pred, ml_pred) if t != r and t == m)
    rule_fixed = sum(1 for t, r, m in zip(y_true, rule_pred, ml_pred) if t == r and t != m)

    print(f"전체 {len(samples)}건")
    print(f"  규칙 기반 정답: {rule_correct}건 ({rule_correct/len(samples)*100:.1f}%)")
    print(f"  ML 정답: {ml_correct}건 ({ml_correct/len(samples)*100:.1f}%)")
    print(f"  규칙 기반은 틀렸는데 ML은 맞음: {ml_fixed}건")
    print(f"  ML은 틀렸는데 규칙 기반은 맞음: {rule_fixed}건")
    print(f"  둘 다 틀림: {both_wrong}건")


if __name__ == "__main__":
    evaluate()
