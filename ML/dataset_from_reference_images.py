"""1차 학습셋 구축: MFDS 참고사진(storage/drug_image/) + DB product.drug_shape.

batch가 이미 받아둔 참고사진마다 DB에 확정된 drug_shape가 있으므로, 별도
라벨링 없이 (이미지 경로, 정답 모양) 쌍을 바로 뽑아낼 수 있다. product 테이블
전체를 훑어서 실제로 파일이 로컬에 존재하는 것만 골라 CSV 매니페스트로
저장한다 (item_seq, image_path, shape). 없는 파일은 조용히 건너뛴다 - batch의
이미지 다운로드가 아직 전체를 못 받은 상태일 수 있어서다.

다만 참고사진은 배경/조명이 깔끔한 "정답 사진"이라 실사용자 사진과 도메인
차이가 있음 - dataset_from_user_confirmations.py로 보완 필요.

사용법 (venv의 pymysql/dotenv가 필요해서 반드시 venv 파이썬으로 실행):
  venv/Scripts/python.exe ML/dataset_from_reference_images.py [--output ML/data/reference_images.csv]
"""

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.db import get_connection  # noqa: E402

DRUG_IMAGE_DIR = BASE_DIR / "storage" / "drug_image"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "data" / "reference_images.csv"


def build_dataset(output_path: Path) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT item_seq, drug_shape FROM product WHERE drug_shape IS NOT NULL AND drug_shape != ''")
            rows = cur.fetchall()
    finally:
        conn.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shape_counts = Counter()
    missing = 0

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["item_seq", "image_path", "shape"])
        for row in rows:
            item_seq = row["item_seq"]
            image_path = DRUG_IMAGE_DIR / f"{item_seq}.jpg"
            if not image_path.is_file():
                missing += 1
                continue
            shape = row["drug_shape"]
            writer.writerow([item_seq, str(image_path), shape])
            shape_counts[shape] += 1

    total = sum(shape_counts.values())
    print(f"학습셋 저장: {output_path} (총 {total}건, 이미지 없어서 제외 {missing}건)")
    for shape, count in shape_counts.most_common():
        print(f"  {shape}\t{count}\t{count/total*100:.2f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build_dataset(args.output)
