"""dataset_from_*.py가 만든 학습셋으로 모양 분류기를 학습한다.

서버는 CPU 추론만 가능하므로(GPU 없음), 딥러닝 대신 shape_features.py의
기하학적 특징 벡터(종횡비/원형도/면적비/꼭짓점 수/Hu moments) + RandomForest를
쓴다 - 지금 데이터 규모(수백~수천 건)에도 잘 맞고, 학습/추론 둘 다 가볍다.
학습 자체는 로컬에서만 하고, 결과 가중치만 export_weights.py로 뽑아 서버에
올린다(FastSAM-s.pt와 동일한 패턴).

## 입력
- ML/data/reference_images.csv (item_seq, image_path, shape) - 필수
- ML/data/user_confirmations.csv (item_seq, image_path, mask_path, shape) - 있으면 합침

## 출력
- ML/models/shape_classifier.joblib (모델 + 라벨 인코더 + feature 이름)

사용법 (venv의 scikit-learn이 필요해서 반드시 venv 파이썬으로 실행):
  venv/Scripts/python.exe ML/train_shape_classifier.py
"""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import cv2
import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.preprocessing import LabelEncoder

from shape_features import (
    augment_mask,
    contour_from_mask,
    contours_from_reference_image,
    features_from_contour,
    mask_from_contour,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
MODELS_DIR = Path(__file__).resolve().parent / "models"
REFERENCE_CSV = DATA_DIR / "reference_images.csv"
USER_CONFIRMATIONS_CSV = DATA_DIR / "user_confirmations.csv"
DEFAULT_OUTPUT = MODELS_DIR / "shape_classifier.joblib"

# 원본 하나당 증강 마스크를 몇 개 더 만들지. 회전/크기 증강은 안 쓴다 - 지금
# 특징(Hu moments 등)이 이미 회전/크기 불변이라 그런 증강은 새 정보를 안 만든다
# (shape_features.augment_mask 주석 참고). 대신 세그멘테이션 잡음(침식/팽창
# +블러)을 흉내낸 증강만 쓴다.
N_AUGMENT = 4

_RNG = np.random.default_rng(42)


def _augmented_features(mask: np.ndarray) -> list[np.ndarray]:
    out = []
    for _ in range(N_AUGMENT):
        aug_mask = augment_mask(mask, _RNG)
        aug_contour = contour_from_mask(aug_mask)
        if aug_contour is None:
            continue
        feats = features_from_contour(aug_contour)
        if feats is not None:
            out.append(feats)
    return out


def _load_reference_rows(csv_path: Path, group_offset: int) -> list[tuple[np.ndarray, str, int]]:
    """(특징 벡터, 정답 shape, 그룹id) 목록. 컨투어를 하나도 못 찾은 사진만 건너뛴다.

    segment_pills()(FastSAM)로 검출하면 사진 1건에 알약이 여러 개(참고사진
    특유의 앞/뒷면 나란히 배치) 잡히는데, 같은 물리적 알약의 앞/뒷면이라
    모양이 사실상 같은 값이라 전부 다 쓰면 안 된다 - 우연히 둘 다 검출된
    품목이 학습에서 암묵적으로 더 큰 가중치를 갖게 된다(품목별 표본 수가
    들쭉날쭉해짐). 그래서 가장 큰 컨투어 하나만 대표로 쓴다 - 물리적 아이템
    1개 = 학습 샘플 1개(+증강)로 깔끔하게 맞춘다.
    """
    if not csv_path.is_file():
        return []
    samples = []
    skipped = 0
    group = group_offset
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            image = cv2.imread(row["image_path"])
            if image is None:
                skipped += 1
                continue
            contours = contours_from_reference_image(image)
            if not contours:
                skipped += 1
                continue
            contour = max(contours, key=cv2.contourArea)
            feats = features_from_contour(contour)
            if feats is None:
                skipped += 1
                continue
            shape = row["shape"]
            samples.append((feats, shape, group))
            mask = mask_from_contour(contour, image.shape)
            samples.extend((f, shape, group) for f in _augmented_features(mask))
            group += 1
    print(f"  {csv_path.name}: {len(samples)}건 로드(증강 포함), {skipped}건 스킵(검출 실패)")
    return samples


def _load_user_confirmation_rows(csv_path: Path, group_offset: int) -> list[tuple[np.ndarray, str, int]]:
    if not csv_path.is_file():
        return []
    samples = []
    skipped = 0
    group = group_offset
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mask = cv2.imread(row["mask_path"], cv2.IMREAD_GRAYSCALE)
            if mask is None:
                skipped += 1
                continue
            contour = contour_from_mask(mask)
            if contour is None:
                skipped += 1
                continue
            feats = features_from_contour(contour)
            if feats is None:
                skipped += 1
                continue
            shape = row["shape"]
            samples.append((feats, shape, group))
            samples.extend((f, shape, group) for f in _augmented_features(mask))
            group += 1
    print(f"  {csv_path.name}: {len(samples)}건 로드(증강 포함), {skipped}건 스킵")
    return samples


def train(output_path: Path) -> None:
    print("학습셋 로딩:")
    # user 쪽 그룹id 시작값을 큰 상수로 고정해둔다(증강 포함 개수 같은 가변값에
    # 의존하면 안 됨) - evaluate_shape_classifier.py가 같은 오프셋으로 CSV
    # 행 순번 -> 그룹id를 독립적으로 재현해야 하기 때문이다.
    ref_samples = _load_reference_rows(REFERENCE_CSV, group_offset=0)
    user_samples = _load_user_confirmation_rows(USER_CONFIRMATIONS_CSV, group_offset=1_000_000)
    samples = ref_samples + user_samples
    if len(samples) < 20:
        print(f"학습셋이 너무 적습니다({len(samples)}건) - 최소 20건은 있어야 의미 있는 검증이 됩니다.")
        return

    X = np.array([s[0] for s in samples])
    y_raw = [s[1] for s in samples]
    groups = np.array([s[2] for s in samples])

    # 원본 기준(증강 포함 전) 분포를 보여줘야 실제 데이터 규모를 오해하지 않는다.
    counts_by_group = {}
    for feats, label, group in samples:
        counts_by_group.setdefault(group, label)
    counts = Counter(counts_by_group.values())
    print(f"클래스 분포(원본 {len(counts_by_group)}건 기준, 증강 포함 총 {len(samples)}건):", dict(counts))

    # 1건짜리 클래스는 train/test 층화 분할이 불가능하므로 미리 걸러낸다.
    rare = {label for label, c in counts.items() if c < 2}
    if rare:
        print(f"표본이 1건뿐인 클래스는 이번 학습에서 제외합니다: {sorted(rare)}")
        keep = [i for i, label in enumerate(y_raw) if label not in rare]
        X = X[keep]
        y_raw = [y_raw[i] for i in keep]
        groups = groups[keep]

    encoder = LabelEncoder()
    y = encoder.fit_transform(y_raw)

    # 그룹(같은 알약의 원본+증강본)이 train/test에 걸쳐 섞이지 않게
    # StratifiedGroupKFold로 나눈다 - 첫 fold를 검증셋(약 20%)으로 쓴다.
    splitter = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=42)
    train_idx, test_idx = next(splitter.split(X, y, groups))
    X_train, X_test = X[train_idx], X[test_idx]
    y_train, y_test = y[train_idx], y[test_idx]

    # evaluate_shape_classifier.py가 규칙 기반(classify_shape())과 정확히
    # 같은 검증셋으로 비교할 수 있도록, 이번 학습에서 검증셋으로 빠진
    # 그룹id를 남겨둔다. reference_images.csv/user_confirmations.csv의 행
    # 순서가 이 학습 이후로 안 바뀌었다는 전제(그룹id = 행 순번이라서다).
    test_groups_path = DATA_DIR / "test_groups.json"
    with open(test_groups_path, "w", encoding="utf-8") as f:
        json.dump(sorted({int(g) for g in groups[test_idx]}), f)
    print(f"검증셋 그룹 저장: {test_groups_path} ({len(set(groups[test_idx]))}개 그룹)")

    model = RandomForestClassifier(
        n_estimators=200,
        class_weight="balanced",  # 원형/타원/장방형이 압도적으로 많아서 소수 클래스가 묻히지 않게 가중치를 준다.
        random_state=42,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    print("\n검증셋 성능:")
    # 표본이 적은 클래스는 우연히 테스트셋에 하나도 안 걸릴 수 있다 - labels를
    # 명시해서 그런 클래스도 (0으로) 표에 나오게 한다. 안 그러면
    # target_names 길이와 실제 등장 클래스 수가 달라서 에러가 난다.
    print(
        classification_report(
            y_test,
            y_pred,
            labels=range(len(encoder.classes_)),
            target_names=encoder.classes_,
            zero_division=0,
        )
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "label_encoder": encoder}, output_path)
    print(f"모델 저장: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    train(args.output)
