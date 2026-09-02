"""보약(boyak) 백엔드 - FastAPI.

React 프론트가 직접 호출하는 API. 중간 단계 없이 파이썬이 업로드/분석/재분석을
전부 담당한다

  POST /upload       (multipart, field "image") -> 저장 + 분석 결과 반환
  POST /reanalyze    (JSON: image_path, target_id, mask_png_base64)
                      -> 마스크로 즉시 재진단 + 학습용 교정 데이터 저장
  POST /drug_lookup  (JSON: shape, color) -> 모양+색상으로 약 이름 후보 조회
  POST /reject_target (JSON: image_path, target_id, shape, color, points)
                      -> 오검출(미대상) 기록 (검출 단계 재학습용 음성 예제)
  POST /confirm_result (JSON: image_path, target_id, item_seq)
                      -> 결과화면에서 "이 약이 맞다" 확정 (모양 분류기
                         학습용 - 확정된 item_seq의 DB drug_shape가 이
                         사진/영역의 정답 라벨이 된다)
  GET  /images/{name} -> 업로드된 이미지 정적 서빙
  GET  /health

각 엔드포인트는 한 가지 일만 한다 (RESTful). PHP 버전의 "결과화면"처럼
재분석+이름조회를 서버가 한 번에 묶어서 처리하지 않고, React(App.jsx의
submitAll())가 /reanalyze -> /drug_lookup 순서로 호출해 조합한다.

실행: run.bat (또는 ./run.sh) - 88 포트  (boyak-be 루트에서)
"""

import base64
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnose import diagnose as diagnose_image  # noqa: E402
from pill_analyze import diagnose_from_mask  # noqa: E402
from app.services.pill_service import PillService  # noqa: E402

pill_service = PillService()

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage" / "images"
CORRECTIONS_DIR = BASE_DIR / "storage" / "ml_corrections"
CORRECTIONS_LOG = BASE_DIR / "storage" / "ml_corrections.jsonl"
REJECTIONS_LOG = BASE_DIR / "storage" / "ml_rejections.jsonl"
CONFIRMATIONS_LOG = BASE_DIR / "storage" / "ml_confirmations.jsonl"
DRUG_IMAGE_DIR = BASE_DIR / "storage" / "drug_image"  # batch(PHP)가 채워 넣는 참고 이미지

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
CORRECTIONS_DIR.mkdir(parents=True, exist_ok=True)
DRUG_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="보이는 알약(보약) API")

# 개발 단계: 프론트 오리진 제한 없이 전부 허용. 배포 시 실제 프론트 도메인으로 좁혀야 한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/images", StaticFiles(directory=STORAGE_DIR), name="images")
app.mount("/drug_images", StaticFiles(directory=DRUG_IMAGE_DIR), name="drug_images")

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}


class ReanalyzeRequest(BaseModel):
    image_path: str
    target_id: Optional[Union[int, str]] = None
    mask_png_base64: str


class DrugLookupRequest(BaseModel):
    shape: str
    color: str
    imprint: Optional[str] = None


class RejectTargetRequest(BaseModel):
    image_path: str
    target_id: Optional[Union[int, str]] = None
    shape: str
    color: str
    points: list


class ConfirmResultRequest(BaseModel):
    image_path: str
    target_id: Optional[Union[int, str]] = None
    item_seq: str


def _decode_mask_png(mask_b64: str, shape) -> np.ndarray:
    """프론트가 보낸 base64 PNG 마스크를 이미지와 같은 크기의 흑백 마스크로 디코딩한다."""
    header, _, data = mask_b64.partition(",")
    raw = base64.b64decode(data if data else header)
    arr = np.frombuffer(raw, dtype=np.uint8)
    mask = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if mask is None:
        raise ValueError("마스크 PNG를 디코딩하지 못했습니다.")
    if mask.shape[:2] != shape[:2]:
        mask = cv2.resize(mask, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
    _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    return mask


def _save_correction(image_path: str, target_id, mask: np.ndarray, result: dict) -> str:
    """사람이 교정한 마스크 = ML(FastSAM) 판독 능력을 끌어올릴 학습 데이터.
    화면에 즉시 결과를 보여주는 것과 별개로, 나중에 파인튜닝에 쓸 수 있도록 쌓아둔다."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    mask_path = CORRECTIONS_DIR / f"{ts}_{Path(image_path).stem}_{target_id}.png"
    cv2.imwrite(str(mask_path), mask)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "image_path": image_path,
        "target_id": target_id,
        "mask_path": str(mask_path),
        "shape": result["shape"],
        "color": result["color"],
    }
    with open(CORRECTIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return str(mask_path)


def _save_rejection(body: "RejectTargetRequest") -> None:
    """사람이 '미대상(오탐)'으로 표시한 FastSAM 검출 = 검출 단계 오탐 억제
    학습에 쓸 음성(negative) 예제. 마스크 보정이 필요 없으니(애초에 알약이
    아니라는 판정이라) 원본 폴리곤 좌표만 기록해둔다."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "image_path": body.image_path,
        "target_id": body.target_id,
        "shape": body.shape,
        "color": body.color,
        "points": body.points,
    }
    with open(REJECTIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _save_confirmation(body: "ConfirmResultRequest") -> None:
    """결과화면에서 사용자가 "이 약이 맞다"고 확정한 건 = 모양 분류기 학습용
    정답 라벨. 여기서 shape을 직접 조회해 저장하지 않고 item_seq만 남긴다 -
    학습 데이터를 만들 때(ML/dataset_from_user_confirmations.py) product
    테이블에서 그 시점 기준 drug_shape을 조인해서 쓴다."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "image_path": body.image_path,
        "target_id": body.target_id,
        "item_seq": body.item_seq,
    }
    with open(CONFIRMATIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _to_pills(results: list, w: int, h: int) -> list:
    pills = []
    for i, r in enumerate(results, 1):
        contour = r["contour"]
        eps = 0.01 * cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, eps, True).reshape(-1, 2)
        points = [[round(float(x) / w, 4), round(float(y) / h, 4)] for x, y in approx]
        pills.append(
            {
                "index": i,
                "shape": r["shape"],
                "color": r["color"],
                "shape_label": pill_service.get_shape_label(r["shape"]),
                "color_label": pill_service.get_color_label(r["color"]),
                "color_rgb": list(r["color_rgb"]),
                "imprint": r.get("imprint", ""),
                "points": points,
            }
        )
    return pills


@app.get("/health")
def health():
    """서버가 떠 있는지 확인한다."""
    return {"status": "ok"}


@app.post("/upload")
async def upload(image: UploadFile = File(...)):
    """이미지등록 + 분석요청 (판독화면). 사진을 storage/images/에 저장하고
    바로 분석해서 알약별 모양/색상/폴리곤 좌표를 반환한다."""
    ext = Path(image.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"허용된 확장자({', '.join(ALLOWED_EXT)})가 아닙니다: {ext}")

    name = f"{uuid.uuid4().hex}{ext}"
    dest = STORAGE_DIR / name
    with open(dest, "wb") as f:
        shutil.copyfileobj(image.file, f)

    cv_image = cv2.imread(str(dest))
    if cv_image is None:
        raise HTTPException(400, "이미지를 읽을 수 없습니다.")
    h, w = cv_image.shape[:2]

    try:
        results = diagnose_image(str(dest), save_overlay=False)
    except Exception as e:
        raise HTTPException(422, str(e))

    return {
        "image_path": str(dest),
        "image_url": f"/images/{name}",
        "image_size": [w, h],
        "pills": _to_pills(results, w, h),
    }


@app.post("/reanalyze")
def reanalyze(body: ReanalyzeRequest):
    """재분석 (마스킹). 사람이 칠한 마스크로 즉시 모양/색상을 다시 계산하고,
    그 마스크를 storage/ml_corrections/에 학습용 데이터로 저장한다."""
    image_path = body.image_path
    if not Path(image_path).exists():
        raise HTTPException(400, f"이미지를 찾을 수 없습니다: {image_path}")

    image = cv2.imread(image_path)
    if image is None:
        raise HTTPException(400, f"이미지를 읽을 수 없습니다: {image_path}")

    try:
        mask = _decode_mask_png(body.mask_png_base64, image.shape)
        result = diagnose_from_mask(image, mask)
    except Exception as e:
        raise HTTPException(422, str(e))

    try:
        _save_correction(image_path, body.target_id, mask, result)
    except Exception:
        pass  # 학습 데이터 저장 실패가 사용자 응답을 막으면 안 된다

    return {
        "image_path": image_path,
        "target_id": body.target_id,
        "shape": result["shape"],
        "color": result["color"],
        "shape_label": pill_service.get_shape_label(result["shape"]),
        "color_label": pill_service.get_color_label(result["color"]),
        "color_rgb": list(result["color_rgb"]),
        "imprint": result.get("imprint", ""),
    }


@app.post("/drug_lookup")
def drug_lookup(body: DrugLookupRequest):
    """모양+색상(+각인)으로 후보 약을 찾는다 (결과화면에서 사용, '약 찾기').
    실제 DB(product 테이블, batch가 채워 넣음) 조회 결과를 돌려준다."""
    drugs = pill_service.find_drugs(body.shape, body.color, body.imprint)
    return {
        "shape_label": pill_service.get_shape_label(body.shape),
        "color_label": pill_service.get_color_label(body.color),
        "drug_names": [d["item_name"] for d in drugs],
        "drugs": drugs,
    }


@app.post("/reject_target")
def reject_target(body: RejectTargetRequest):
    """사용자가 '미대상(오탐)'으로 표시한 FastSAM 검출을 기록한다 (판독화면 -
    '이건 알약이 아니다' 표시). 화면 응답에 영향 없이 로그만 남긴다."""
    try:
        _save_rejection(body)
    except Exception:
        pass  # 학습 데이터 저장 실패가 사용자 응답을 막으면 안 된다
    return {"status": "ok"}


@app.post("/confirm_result")
def confirm_result(body: ConfirmResultRequest):
    """결과화면에서 사용자가 특정 약으로 확정한 걸 기록한다 (모양 분류기
    학습용 정답 라벨). 화면 응답에 영향 없이 로그만 남긴다."""
    try:
        _save_confirmation(body)
    except Exception:
        pass  # 학습 데이터 저장 실패가 사용자 응답을 막으면 안 된다
    return {"status": "ok"}


@app.get("/drug_search")
def drug_search(q: str):
    """약 이름/제조사명으로 직접 검색 (사진 없이, 식약처 의약품 정보 테이블 조회)."""
    q = q.strip()
    if not q:
        return {"drugs": []}
    return {"drugs": pill_service.search_drugs(q)}
