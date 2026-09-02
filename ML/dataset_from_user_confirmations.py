"""실사용 도메인 학습셋 구축: 결과화면에서 "이 약이 맞다"로 확정한 건을
(실사진 마스크, 확정된 item_seq의 DB drug_shape) 쌍으로 변환한다.

reference_images.csv(MFDS 참고사진)는 배경/조명이 깔끔한 "정답 사진"이라
실사용자 사진과 도메인 차이가 있다 - 이 스크립트가 그 차이를 메우는
실도메인 학습 데이터를 만든다. 사람이 모양을 따로 라벨링할 필요는 없다 -
확정된 약의 DB drug_shape를 그대로 정답으로 쓴다.

## 원천 로그 2개를 조인한다
- storage/ml_confirmations.jsonl : {image_path, target_id, item_seq} - "이 약이
  맞다" 확정 기록 (app/main.py POST /confirm_result)
- storage/ml_corrections.jsonl : {image_path, target_id, mask_path} - 마스킹
  단계를 거친 알약의 실제 보정 마스크 (app/main.py POST /reanalyze)

## 알려진 한계 (마스크 없는 확정 건)
결과화면에서 마스킹 없이 그대로 확정한("confirmed") 알약은 그 알약 하나만의
폴리곤/마스크가 서버에 저장되지 않는다 - /upload 응답의 points는 화면
표시용으로만 쓰이고 서버에 남지 않는다. 그래서 지금은 마스킹 단계를 거쳐
ml_corrections.jsonl에 마스크가 남아있는("reanalyzed") 건만 크롭 가능하고,
나머지는 스킵하고 건수만 집계한다. 이 갭을 메우려면 /confirm_result가 points도
같이 받도록 확장해야 한다(현재는 하지 않기로 함).

사용법 (venv의 pymysql/dotenv가 필요해서 반드시 venv 파이썬으로 실행):
  venv/Scripts/python.exe ML/dataset_from_user_confirmations.py [--output ML/data/user_confirmations.csv]
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.db import get_connection  # noqa: E402

CONFIRMATIONS_LOG = BASE_DIR / "storage" / "ml_confirmations.jsonl"
CORRECTIONS_LOG = BASE_DIR / "storage" / "ml_corrections.jsonl"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "data" / "user_confirmations.csv"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _shape_by_item_seq(item_seqs: set[str]) -> dict[str, str]:
    if not item_seqs:
        return {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(item_seqs))
            cur.execute(
                f"SELECT item_seq, drug_shape FROM product WHERE item_seq IN ({placeholders})",
                tuple(item_seqs),
            )
            return {row["item_seq"]: row["drug_shape"] for row in cur.fetchall()}
    finally:
        conn.close()


def build_dataset(output_path: Path) -> None:
    confirmations = _read_jsonl(CONFIRMATIONS_LOG)
    corrections = _read_jsonl(CORRECTIONS_LOG)

    # (image_path, target_id) 쌍으로 조인한다. 같은 쌍에 여러 번 보정했을 수
    # 있으니(재교정), 가장 최신 timestamp의 mask_path를 쓴다.
    mask_by_key: dict[tuple, str] = {}
    for c in sorted(corrections, key=lambda r: r.get("timestamp", "")):
        key = (c["image_path"], str(c.get("target_id")))
        mask_by_key[key] = c["mask_path"]

    item_seqs = {c["item_seq"] for c in confirmations}
    shape_by_item_seq = _shape_by_item_seq(item_seqs)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shape_counts = Counter()
    skipped_no_mask = 0
    skipped_no_shape = 0

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["item_seq", "image_path", "mask_path", "shape"])
        for c in confirmations:
            key = (c["image_path"], str(c.get("target_id")))
            mask_path = mask_by_key.get(key)
            if mask_path is None:
                # 마스킹 없이 그대로 확정한 건 - 알약 하나만의 영역 정보가
                # 없어서 지금은 크롭할 수 없다(모듈 docstring의 "알려진 한계" 참고).
                skipped_no_mask += 1
                continue
            shape = shape_by_item_seq.get(c["item_seq"])
            if not shape:
                skipped_no_shape += 1
                continue
            writer.writerow([c["item_seq"], c["image_path"], mask_path, shape])
            shape_counts[shape] += 1

    total = sum(shape_counts.values())
    print(f"학습셋 저장: {output_path} (총 {total}건)")
    print(f"  스킵 - 마스크 없음(마스킹 없이 확정): {skipped_no_mask}건")
    print(f"  스킵 - DB에 item_seq 없음/모양값 없음: {skipped_no_shape}건")
    for shape, count in shape_counts.most_common():
        print(f"  {shape}\t{count}\t{count/total*100:.2f}%" if total else f"  {shape}\t{count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build_dataset(args.output)
