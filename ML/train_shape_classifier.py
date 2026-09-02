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
  ./venv/bin/python ML/train_shape_classifier.py

캐시에 없는(신규) 아이템의 FastSAM 특징 추출은 여러 프로세스로 병렬 처리한다.
기본 워커 수는 4개(메모리 안전 우선) - 코어/메모리 여유에 따라 조정하려면
매번 커맨드라인에 안 붙이고 ML/.env에 넣어둬도 된다(batch/env.php와 같은
취지, .env.example 참고):
  ML_TRAIN_WORKERS=2 venv/Scripts/python.exe ML/train_shape_classifier.py
"""

import argparse
import csv
import json
import os
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
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


def _load_local_env() -> None:
    """batch/env.php와 같은 취지의 의존성 없는 초경량 로더 - ML/.env가 있으면
    읽어서 os.environ에 아직 없는 키만 채운다(커맨드라인/시스템 환경변수가
    항상 우선). ML_TRAIN_WORKERS처럼 로컬 PC마다 다른 설정을 매번 안 치고
    파일에 넣어두고 싶을 때 쓴다. `KEY=값 #설명` 처럼 값 뒤 인라인 주석도
    지원한다(batch/env.php는 줄 전체가 #으로 시작할 때만 주석 처리라 이 점만 다름)."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split("#", 1)[0]  # 값 뒤에 붙은 인라인 주석 제거
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_local_env()

# feature 정의(shape_features.FEATURE_NAMES 등)가 바뀌면 캐시가 예전 벡터
# 구성을 그대로 돌려주면 안 되므로, 서명에 이 버전을 같이 넣어서 캐시를
# 무효화할 수 있게 한다. FastSAM 특징 추출 로직을 바꿀 때마다 올릴 것.
FEATURE_CACHE_VERSION = 1

DATA_DIR = Path(__file__).resolve().parent / "data"
MODELS_DIR = Path(__file__).resolve().parent / "models"
REFERENCE_CSV = DATA_DIR / "reference_images.csv"
USER_CONFIRMATIONS_CSV = DATA_DIR / "user_confirmations.csv"
DEFAULT_OUTPUT = MODELS_DIR / "shape_classifier.joblib"

# 워커 프로세스 1개당 torch/scipy/sklearn/opencv/ultralytics를 통째로 다시
# 임포트해야 해서(가벼운 스레드가 아니라 무거운 프로세스), CPU 코어 수만 보고
# 워커 수를 정하면 안 된다 - 코어가 많은 환경에서 os.cpu_count()-1개를 그대로
# 띄웠다가 메모리/페이징 파일이 바닥나 BrokenProcessPool로 죽는 걸 실제로
# 겪었다. 기본은 보수적으로 4개, 필요하면 환경변수로 조정.
#   ML_TRAIN_WORKERS=2 python train_shape_classifier.py   (메모리 부족 시)
#   ML_TRAIN_WORKERS=8 python train_shape_classifier.py   (RAM 넉넉하면)
MAX_PARALLEL_WORKERS = int(os.environ.get("ML_TRAIN_WORKERS", "4"))

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


# 아이템 1건(캐시에 없는 것)의 FastSAM 특징 추출을 워커 프로세스에서 돌리기
# 위한 top-level 함수들. multiprocessing(spawn, Windows 기본값 포함)은 이
# 함수들을 자식 프로세스에서 다시 import할 수 있어야 하므로 모듈 최상위에
# 있어야 한다(클로저/메서드는 피클링이 안 됨). 코어가 여유 있는 로컬 PC에서
# 순차 실행 시 CPU 활용률이 한 코어 몫(15~30%)에 머무는 걸 확인해서 추가함 -
# 워커 프로세스마다 FastSAM 모델을 각자 로드하므로(app.pill_analyze의 lazy
# 싱글턴 패턴) 프로세스 수만큼 메모리가 더 들지만, 로컬 PC는 서버와 달리
# 여유가 있다는 전제.
def _compute_reference_item_features(image_path: str) -> list[np.ndarray] | None:
    image = cv2.imread(image_path)
    if image is None:
        return None
    contours = contours_from_reference_image(image)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    feats = features_from_contour(contour)
    if feats is None:
        return None
    mask = mask_from_contour(contour, image.shape)
    return [feats] + _augmented_features(mask)


def _compute_confirmation_item_features(mask_path: str) -> list[np.ndarray] | None:
    mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
    if mask is None:
        return None
    contour = contour_from_mask(mask)
    if contour is None:
        return None
    feats = features_from_contour(contour)
    if feats is None:
        return None
    return [feats] + _augmented_features(mask)


# 전체 pending 건수의 이 비율마다 한 번씩 캐시를 디스크에 flush한다(총 10번
# 안팎) - 고정 건수(예: 500건마다)로 하면 25,000+건 기준 캐시 저장을 50번 가까이
# 하게 돼서 저장 자체의 I/O 부하가 누적된다. 건수에 비례해서 체크포인트 횟수를
# 고정해두면 데이터가 늘어도 저장 횟수(=I/O 부하)는 그대로다. 중간에 끊겨도
# (Ctrl+C, 크래시, 메모리 부족으로 인한 BrokenProcessPool 등) 마지막 체크포인트
# 까지는 남아있어서 다음 실행이 그 이후 아이템부터 이어서 계산한다.
# ML/.env의 ML_CHECKPOINT_FRACTION으로 조정 가능(MAX_PARALLEL_WORKERS와 같은 방식).
CHECKPOINT_FRACTION = float(os.environ.get("ML_CHECKPOINT_FRACTION", "0.10"))


def _compute_pending_in_parallel(compute_fn, pending: list[tuple[str, str]], csv_path: Path, cache_map: dict) -> int:
    """pending = [(cache_key, 파일경로), ...] 를 여러 프로세스에 나눠 돌리고,
    끝난 건 즉시 cache_map에 채워 넣는다(참조로 받아 그 자리에서 갱신 - 호출부의
    cache_map과 같은 객체). 전체의 CHECKPOINT_FRACTION만큼 처리될 때마다(총
    1/CHECKPOINT_FRACTION번 안팎), 그리고 끝나고 나면 디스크에 저장한다. 돌려주는
    값은 스킵(실패) 건수. 1건뿐이면 프로세스 띄우는 오버헤드가 더 크니 순차 처리한다.
    """
    if not pending:
        return 0

    if len(pending) == 1:
        key, path = pending[0]
        item_feats = compute_fn(path)
        if item_feats is None:
            return 1
        cache_map[key] = item_feats
        _save_item_cache(csv_path, cache_map)
        return 0

    checkpoint_every = max(1, int(len(pending) * CHECKPOINT_FRACTION))
    max_workers = max(1, min(MAX_PARALLEL_WORKERS, (os.cpu_count() or 2) - 1))
    print(
        f"    {len(pending)}건 신규 계산 필요 - {max_workers}개 프로세스로 병렬 처리 "
        f"({checkpoint_every}건마다 중간 저장)"
    )
    t0 = time.perf_counter()
    skipped = 0
    done_since_checkpoint = 0
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_key = {executor.submit(compute_fn, path): key for key, path in pending}
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            item_feats = future.result()
            if item_feats is None:
                skipped += 1
                continue
            cache_map[key] = item_feats
            done_since_checkpoint += 1
            if done_since_checkpoint >= checkpoint_every:
                _save_item_cache(csv_path, cache_map)
                done_since_checkpoint = 0
                print(f"    중간 저장: 지금까지 {len(cache_map)}건 캐시에 반영")
    elapsed = time.perf_counter() - t0
    per_item_ms = elapsed / len(pending) * 1000
    print(f"    병렬 계산 소요: {elapsed:.1f}초 (워커 {max_workers}개, 건당 평균 {per_item_ms:.0f}ms)")
    _save_item_cache(csv_path, cache_map)  # 마지막 자투리(체크포인트 미만 남은 분) 저장
    return skipped


# FastSAM 특징 추출(25,000+건이면 CPU에서 수십분~수시간)이 재학습마다 반복되지
# 않도록, 결과를 CSV 옆에 "아이템 단위"로 캐싱한다. CSV 전체의 해시로 캐시를
# on/off 하면, 새 알약 몇 건이 추가되기만 해도 캐시 전체가 무효화돼서 25,000+건을
# 통째로 다시 계산하게 된다(진짜 증분이 아님) - 그래서 item_seq(참고사진 쪽)
# 또는 마스크 파일명(사용자 확인 쪽)을 키로 써서, 이미 계산된 아이템은 그대로
# 재사용하고 새로 추가된 아이템만 FastSAM을 돌린다. 캐시엔 특징 벡터만 들어있고
# 정답 라벨(shape)은 매번 CSV에서 새로 읽어 붙이므로, 라벨을 나중에 정정해도
# (사진 자체는 안 바뀌었으면) 재계산 없이 바로 반영된다.
def _cache_path_for(csv_path: Path) -> Path:
    return csv_path.with_suffix(".features_cache.npz")


def _load_item_cache(csv_path: Path) -> dict[str, list[np.ndarray]]:
    cache_path = _cache_path_for(csv_path)
    if not cache_path.is_file():
        return {}
    try:
        data = np.load(cache_path, allow_pickle=False)
        if str(data["version"]) != f"v{FEATURE_CACHE_VERSION}":
            return {}
        keys, feats = data["keys"], data["feats"]
    except Exception:
        return {}
    cache_map: dict[str, list[np.ndarray]] = {}
    for key, feat in zip(keys, feats):
        cache_map.setdefault(str(key), []).append(feat)
    return cache_map


def _save_item_cache(csv_path: Path, cache_map: dict[str, list[np.ndarray]]) -> None:
    if not cache_map:
        return
    keys = [key for key, vecs in cache_map.items() for _ in vecs]
    feats = [v for vecs in cache_map.values() for v in vecs]
    np.savez_compressed(
        _cache_path_for(csv_path),
        keys=np.array(keys),
        feats=np.array(feats),
        version=f"v{FEATURE_CACHE_VERSION}",
    )


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

    cache_map = _load_item_cache(csv_path)  # item_seq -> [원본 feats, 증강 feats...]
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))

    # 캐시에 없는 것만 모아서 병렬로 계산한다(코어 여유가 있는 로컬 PC에서
    # 순차 처리 시 CPU 활용률이 한 코어 몫에 머무는 걸 확인해서 병렬화함).
    cache_hits = sum(1 for row in rows if row["item_seq"] in cache_map)
    pending = [(row["item_seq"], row["image_path"]) for row in rows if row["item_seq"] not in cache_map]
    cache_size_before = len(cache_map)
    skipped = _compute_pending_in_parallel(_compute_reference_item_features, pending, csv_path, cache_map)
    cache_misses = len(cache_map) - cache_size_before

    # group id는 "성공한 행에만 순번을 매긴다"는 evaluate_shape_classifier.py와의
    # 약속이라, 병렬 계산이 끝난 뒤 원래 파일 순서대로 다시 훑으며 순차 부여한다.
    samples = []
    group = group_offset
    for row in rows:
        item_seq = row["item_seq"]  # product.item_seq UNIQUE라 이 CSV에서 1행=1아이템
        if item_seq not in cache_map:
            continue
        samples.extend((feats, row["shape"], group) for feats in cache_map[item_seq])
        group += 1

    print(
        f"  {csv_path.name}: {len(samples)}건 로드(증강 포함), {skipped}건 스킵(검출 실패), "
        f"캐시 재사용 {cache_hits}건 / 신규 계산(FastSAM) {cache_misses}건"
    )
    return samples


def _load_user_confirmation_rows(csv_path: Path, group_offset: int) -> list[tuple[np.ndarray, str, int]]:
    if not csv_path.is_file():
        return []

    # 이 CSV는 item_seq가 중복될 수 있다(같은 알약을 여러 번 확인) - 대신 확인
    # 건마다 고유하게 생성되는 mask_path 파일명을 캐시 키로 쓴다. 파일명 자체는
    # 환경(로컬/서버/Colab)이 바뀌어도 그대로라 경로 루트만 달라도 캐시가 맞는다.
    cache_map = _load_item_cache(csv_path)
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    row_keys = [Path(row["mask_path"]).name for row in rows]

    cache_hits = sum(1 for key in row_keys if key in cache_map)
    pending = [(key, row["mask_path"]) for key, row in zip(row_keys, rows) if key not in cache_map]
    cache_size_before = len(cache_map)
    skipped = _compute_pending_in_parallel(_compute_confirmation_item_features, pending, csv_path, cache_map)
    cache_misses = len(cache_map) - cache_size_before

    samples = []
    group = group_offset
    for key, row in zip(row_keys, rows):
        if key not in cache_map:
            continue
        samples.extend((feats, row["shape"], group) for feats in cache_map[key])
        group += 1

    print(
        f"  {csv_path.name}: {len(samples)}건 로드(증강 포함), {skipped}건 스킵, "
        f"캐시 재사용 {cache_hits}건 / 신규 계산 {cache_misses}건"
    )
    return samples


def train(output_path: Path) -> None:
    t_start = time.perf_counter()
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

    elapsed = time.perf_counter() - t_start
    print(f"전체 소요 시간: {elapsed/60:.1f}분 ({elapsed:.0f}초, 워커 {MAX_PARALLEL_WORKERS}개 설정)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    train(args.output)
