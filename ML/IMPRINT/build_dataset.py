"""각인(imprint) 인식 ML 학습용 데이터셋을 만든다.

## 라벨링 방식
참고사진(storage/drug_image/{item_seq}.jpg) 1장엔 같은 알약의 앞/뒷면이 나란히
있다(왼쪽=앞면, 오른쪽=뒷면 - 실측 확인됨, verify_front_back_order.py 참고).
segment_pills()로 알약 2개를 분리하고, x좌표로 정렬해서 왼쪽 크롭엔
product.print_front, 오른쪽 크롭엔 print_back을 정답으로 붙인다. 즉 참고사진
1장 = 학습 데이터 2건(앞/뒤).

컨투어가 정확히 2개가 아니면(검출 실패/한쪽만 잡힘/노이즈로 3개 이상) 어느
쪽이 앞/뒤인지 확신할 수 없으므로 건너뛴다 - 확실한 라벨만 학습에 쓴다.
"마크"(로고/기호, 실제 텍스트 없음)로 표기된 라벨도 텍스트 인식 학습 대상이
아니라서 제외한다.

## 출력
- ML/IMPRINT/data/crops/{item_seq}_front.png / {item_seq}_back.png
- ML/IMPRINT/data/labels.csv (image_path, text, item_seq, side)

train_shape_classifier.py처럼 신규 계산분만 병렬 처리하고, 이미 크롭 파일이
있는 item_seq는 재계산하지 않는다(재실행 시 이어서 하는 효과).

사용법 (venv 파이썬):
  venv/Scripts/python.exe ML/IMPRINT/build_dataset.py
"""

import csv
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cv2

ML_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = ML_DIR.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(ML_DIR))

from app.db import get_connection  # noqa: E402
from app.pill_analyze import segment_pills  # noqa: E402

# train_shape_classifier.py의 워커 수 설정(ML/.env의 ML_TRAIN_WORKERS)을 그대로 쓴다.
import train_shape_classifier as tm  # noqa: E402

DRUG_IMAGE_DIR = BASE_DIR / "storage" / "drug_image"
DATA_DIR = Path(__file__).resolve().parent / "data"
CROPS_DIR = DATA_DIR / "crops"
LABELS_CSV = DATA_DIR / "labels.csv"


def _fetch_rows() -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT item_seq, print_front, print_back FROM product "
                "WHERE (print_front IS NOT NULL AND print_front != '') "
                "   OR (print_back IS NOT NULL AND print_back != '')"
            )
            return cur.fetchall()
    finally:
        conn.close()


def _clean_label(text):
    if not text:
        return None
    text = text.strip()
    if not text or "마크" in text:
        return None
    return text


# 워커 프로세스에서 돌릴 top-level 함수. item_seq 하나를 처리해서 크롭 PNG를
# 직접 디스크에 쓰고(이미지 자체를 IPC로 주고받으면 무거움), 결과 행(작은
# 데이터)만 돌려준다.
def _process_item(item_seq: str, print_front: str, print_back: str):
    image_path = DRUG_IMAGE_DIR / f"{item_seq}.jpg"
    image = cv2.imread(str(image_path))
    if image is None:
        return "no_image", []

    try:
        contours = list(segment_pills(image))
    except ValueError:
        return "wrong_count", []

    if len(contours) != 2:
        return "wrong_count", []

    boxed = [(cv2.boundingRect(c)[0], c) for c in contours]
    boxed.sort(key=lambda t: t[0])
    (_, front_contour), (_, back_contour) = boxed

    rows = []
    no_label = 0
    for side, contour, raw_label in [
        ("front", front_contour, print_front),
        ("back", back_contour, print_back),
    ]:
        label = _clean_label(raw_label)
        if label is None:
            no_label += 1
            continue
        x, y, w, h = cv2.boundingRect(contour)
        pad = int(0.05 * max(w, h))
        x0, y0 = max(x - pad, 0), max(y - pad, 0)
        x1, y1 = min(x + w + pad, image.shape[1]), min(y + h + pad, image.shape[0])
        crop = image[y0:y1, x0:x1]
        crop_path = CROPS_DIR / f"{item_seq}_{side}.png"
        cv2.imwrite(str(crop_path), crop)
        # image_path는 파일명만 저장한다(CROPS_DIR 기준 상대) - 로컬 절대경로를
        # 그대로 넣으면 Colab 등 다른 환경으로 옮겼을 때 경로가 안 맞아 전부
        # 못 읽는다(train_shape_classifier.py 캐시 서명 때 겪은 것과 같은 종류의
        # 이식성 문제). 실제 로드는 train_imprint_model.py가 DATA_DIR/crops를
        # 기준으로 다시 조립한다.
        rows.append((crop_path.name, label, item_seq, side))

    if not rows:
        return "no_label", []
    return "ok", rows


def _recover_orphan_labels(rows_db: list) -> int:
    """크롭 파일은 있는데 labels.csv엔 없는 것들을 복구한다 - 예전 실행이 크롭을
    다 쓰고 나서 labels.csv 기록 전에 죽으면(Ctrl+C, 크래시 등) 생기는 상태.
    "이미 처리됨" 판단은 크롭 파일 존재로 하는데, labels.csv가 그거보다 뒤처져
    있으면 그 차이만큼 라벨이 영구 누락되므로 매 실행 시작 시 항상 맞춰준다."""
    if not CROPS_DIR.is_dir():
        return 0

    labeled = set()
    if LABELS_CSV.is_file():
        with open(LABELS_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                labeled.add((row["item_seq"], row["side"]))

    by_item_seq = {row["item_seq"]: row for row in rows_db}
    orphans = []
    for p in CROPS_DIR.glob("*.png"):
        item_seq, _, side = p.stem.rpartition("_")
        if (item_seq, side) not in labeled:
            orphans.append((item_seq, side, p.name))
    if not orphans:
        return 0

    is_new = not LABELS_CSV.is_file()
    recovered = 0
    with open(LABELS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["image_path", "text", "item_seq", "side"])
        for item_seq, side, path in orphans:
            db_row = by_item_seq.get(item_seq)
            if not db_row:
                continue
            raw = db_row["print_front"] if side == "front" else db_row["print_back"]
            label = _clean_label(raw)
            if label is None:
                continue
            writer.writerow([path, label, item_seq, side])
            recovered += 1
    return recovered


def build() -> None:
    rows_db = _fetch_rows()
    print(f"각인 라벨 있는 item_seq: {len(rows_db)}건")
    CROPS_DIR.mkdir(parents=True, exist_ok=True)

    recovered = _recover_orphan_labels(rows_db)
    if recovered:
        print(f"크롭은 있지만 라벨 누락됐던 것 복구: {recovered}건")

    # 이미 처리된(크롭 파일 존재) item_seq는 건너뛴다 - 재실행 시 이어서 진행.
    pending = []
    already_done = 0
    for row in rows_db:
        front_exists = (CROPS_DIR / f"{row['item_seq']}_front.png").is_file()
        back_exists = (CROPS_DIR / f"{row['item_seq']}_back.png").is_file()
        if front_exists or back_exists:
            already_done += 1
            continue
        pending.append(row)
    print(f"이미 처리됨(스킵): {already_done}건, 신규 처리 대상: {len(pending)}건")

    stats = {"no_image": 0, "wrong_count": 0, "no_label": 0, "ok": 0}
    total_written = 0

    # labels.csv는 기존 내용(이전 실행분)에 이어서 append하고, 처리되는 대로
    # 바로바로 한 줄씩 쓴다(끝날 때까지 메모리에 모아뒀다가 한 번에 쓰지 않음) -
    # 중간에 끊겨도(Ctrl+C, 크래시 등) 그때까지 처리한 라벨은 남는다. 크롭
    # 파일도 이미 개별로 저장돼 있으니, 재실행하면 이 CSV에 없는 것부터
    # 자연스럽게 이어진다(위 already_done 스킵 로직).
    is_new = not LABELS_CSV.is_file()
    with open(LABELS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["image_path", "text", "item_seq", "side"])
            f.flush()

        if pending:
            max_workers = max(1, min(tm.MAX_PARALLEL_WORKERS, (os.cpu_count() or 2) - 1))
            print(f"{max_workers}개 프로세스로 병렬 처리 시작...")
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_process_item, row["item_seq"], row["print_front"], row["print_back"]): row[
                        "item_seq"
                    ]
                    for row in pending
                }
                done = 0
                for future in as_completed(futures):
                    status, rows = future.result()
                    stats[status] += 1
                    for r in rows:
                        writer.writerow(r)
                        total_written += 1
                    done += 1
                    if done % 200 == 0:
                        f.flush()
                    if done % 1000 == 0:
                        print(f"  진행 {done}/{len(pending)}... (지금까지 {total_written}건 저장)")
        f.flush()

    print(f"\n이번 실행 신규 저장: {total_written}건")
    print(f"  이미지 없음/읽기 실패: {stats['no_image']}건")
    print(f"  컨투어 2개 아님: {stats['wrong_count']}건")
    print(f"  라벨 없음(둘 다 마크 등): {stats['no_label']}건")
    print(f"결과 CSV: {LABELS_CSV}")


if __name__ == "__main__":
    build()
