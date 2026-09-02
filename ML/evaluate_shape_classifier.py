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
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cv2
import joblib
import numpy as np
from sklearn.metrics import classification_report

from shape_features import contour_from_mask, contours_from_reference_image, features_from_contour

# train_shape_classifier.py의 아이템 캐시(_load_item_cache)와 워커 수 설정
# (MAX_PARALLEL_WORKERS, ML/.env의 ML_TRAIN_WORKERS)을 그대로 재사용한다 -
# import 시점에 그쪽 모듈의 _load_local_env()도 같이 실행돼 .env가 반영된다.
import train_shape_classifier as tm

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.pill_analyze import classify_shape  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
REFERENCE_CSV = DATA_DIR / "reference_images.csv"
USER_CONFIRMATIONS_CSV = DATA_DIR / "user_confirmations.csv"
TEST_GROUPS_PATH = DATA_DIR / "test_groups.json"
MODEL_PATH = Path(__file__).resolve().parent / "models" / "shape_classifier.joblib"


# 워커 프로세스에서 돌릴 top-level 함수(피클링 위해 모듈 최상위에 있어야 함).
def _extract_reference_contour(image_path: str):
    image = cv2.imread(image_path)
    if image is None:
        return None
    contours = contours_from_reference_image(image)
    if not contours:
        return None
    return max(contours, key=cv2.contourArea)


def _extract_confirmation_contour(mask_path: str):
    mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
    if mask is None:
        return None
    return contour_from_mask(mask)


def _parallel_extract(compute_fn, items: list[tuple[int, str]]) -> dict:
    """items = [(row_index, 파일경로), ...] 를 병렬로 처리해 {row_index: contour}를
    돌려준다(실패한 건 없음). train_shape_classifier.py의 워커 수 설정을 그대로 쓴다.
    캐시 저장은 안 한다 - contour는 크고(가변 길이 좌표) 이 스크립트 안에서만 쓰는
    일회성 값이라 파일로 남길 필요가 없다."""
    results = {}
    if not items:
        return results
    if len(items) == 1:
        idx, path = items[0]
        contour = compute_fn(path)
        if contour is not None:
            results[idx] = contour
        return results

    max_workers = max(1, min(tm.MAX_PARALLEL_WORKERS, (os.cpu_count() or 2) - 1))
    print(f"    {len(items)}건 병렬 추출 - {max_workers}개 프로세스")
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {executor.submit(compute_fn, path): idx for idx, path in items}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            contour = future.result()
            if contour is not None:
                results[idx] = contour
    return results


def _collect_test_samples_slow(rows, group_offset: int, test_groups: set, is_reference: bool):
    """캐시가 없을 때 쓰는 원래(순차) 방식 - 전체를 다시 FastSAM/마스크로 훑는다."""
    samples = []
    group = group_offset
    for row in rows:
        if is_reference:
            contour = _extract_reference_contour(row["image_path"])
        else:
            contour = _extract_confirmation_contour(row["mask_path"])
        if contour is None:
            continue
        if group in test_groups:
            samples.append((contour, row["shape"]))
        group += 1
    return samples


def _collect_test_samples(csv_path: Path, group_offset: int, test_groups: set, is_reference: bool):
    """train_shape_classifier.py의 그룹id 부여 순서를 그대로 재현한다 -
    성공한 행에만 순번을 매기므로(실패한 행은 건너뛰고 카운터를 안 올림),
    같은 순서로 다시 돌려야 그때 그 test_groups와 정확히 대응된다.

    train_shape_classifier.py가 남긴 아이템 캐시가 있으면, "어떤 행이
    성공했는지"(=캐시 키 존재 여부)를 이미지 처리 없이 알아낼 수 있어서
    그룹id 재현 자체엔 이미지가 필요 없다. 그 다음 검증셋(전체의 ~20%)에
    해당하는 행만 실제로 이미지를 열어 병렬로 컨투어를 뽑는다 - 검증 안 쓰는
    나머지 80%는 아예 손도 안 댄다."""
    if not csv_path.is_file():
        return []

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    cache_map = tm._load_item_cache(csv_path)
    if not cache_map:
        print(f"  {csv_path.name}: 캐시 없음 - 전체 재계산 폴백(느림)")
        return _collect_test_samples_slow(rows, group_offset, test_groups, is_reference)

    group = group_offset
    target_rows = []  # (rows 안에서의 인덱스, row) - 검증셋 그룹에 속하는 것만
    for i, row in enumerate(rows):
        key = row["item_seq"] if is_reference else Path(row["mask_path"]).name
        if key not in cache_map:
            continue  # 학습 때도 실패했던 행 - group 카운터 안 올림(원본 로직과 동일)
        if group in test_groups:
            target_rows.append((i, row))
        group += 1

    compute_fn = _extract_reference_contour if is_reference else _extract_confirmation_contour
    path_field = "image_path" if is_reference else "mask_path"
    items = [(i, row[path_field]) for i, row in target_rows]
    extracted = _parallel_extract(compute_fn, items)

    samples = []
    for i, row in target_rows:
        contour = extracted.get(i)
        if contour is None:
            continue  # 학습 시점엔 성공했는데 지금은 실패(파일 변경 등) - 극히 드묾
        samples.append((contour, row["shape"]))
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
