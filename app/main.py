"""보약(boyak) 백엔드 - FastAPI.

React 프론트가 직접 호출하는 API. 중간 단계 없이 파이썬이 업로드/분석/재분석을
전부 담당한다

  POST /upload       (multipart, field "image") -> 저장 + 분석 결과 반환
  POST /reanalyze    (JSON: image_path, target_id, mask_png_base64)
                      -> 마스크로 즉시 재진단 + 학습용 교정 데이터 저장
  POST /drug_lookup  (JSON: shape, color, imprint, imprint_candidates)
                      -> 모양+색상(+각인)과 잘 맞는 순서로 약 이름 후보 조회
  POST /reject_target (JSON: image_path, target_id, shape, color, points)
                      -> 오검출(미대상) 기록 (검출 단계 재학습용 음성 예제)
  POST /confirm_result (JSON: image_path, target_id, item_seq)
                      -> 결과화면에서 "이 약이 맞다" 확정 (모양 분류기
                         학습용 - 확정된 item_seq의 DB drug_shape가 이
                         사진/영역의 정답 라벨이 된다)
  POST /correct_label (JSON: image_path, target_id, points, field, value)
                      -> 탐지 영역은 맞는데 모양/색상/각인 라벨만 틀렸을 때
                         마스킹 없이 바로 정답 교정 (재학습용 raw 데이터로 축적)
  POST /signup       (JSON: uuid, name, age) -> 로그인 없는 UUID 기반 최소 가입
  GET  /users/{uuid} -> 프로필(튜토리얼 완료 여부 포함) 조회
  PATCH /users/{uuid}/tutorial (JSON: completed) -> 튜토리얼 완료/재설정
  GET  /app_version_check?version_code=&platform= -> 최소 허용 버전 통과 여부.
                      앱이 FE(/intro)를 열 때 넘긴 버전을 FE가 그대로 실어 호출.
                      거절 시 store_url도 같이 내려주고 FE가 그리로 이동시킨다
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
import pymysql
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnose import diagnose as diagnose_image  # noqa: E402
from pill_analyze import diagnose_from_mask  # noqa: E402
from app.services.pill_service import PillService  # noqa: E402
from app.models.user_model import UserModel  # noqa: E402
from app.models.app_version_model import AppVersionModel  # noqa: E402

pill_service = PillService()
user_model = UserModel()
app_version_model = AppVersionModel()

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage" / "images"
CORRECTIONS_DIR = BASE_DIR / "storage" / "ml_corrections"
CORRECTIONS_LOG = BASE_DIR / "storage" / "ml_corrections.jsonl"
REJECTIONS_LOG = BASE_DIR / "storage" / "ml_rejections.jsonl"
CONFIRMATIONS_LOG = BASE_DIR / "storage" / "ml_confirmations.jsonl"
LABEL_CORRECTIONS_DIR = BASE_DIR / "storage" / "ml_label_corrections"
LABEL_CORRECTIONS_LOG = BASE_DIR / "storage" / "ml_label_corrections.jsonl"
DRUG_IMAGE_DIR = BASE_DIR / "storage" / "drug_image"  # batch(PHP)가 채워 넣는 참고 이미지

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
CORRECTIONS_DIR.mkdir(parents=True, exist_ok=True)
LABEL_CORRECTIONS_DIR.mkdir(parents=True, exist_ok=True)
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
    # /upload, /reanalyze가 내려준 imprint_candidates를 그대로 실어 보낸다(선택).
    # 사용자가 각인을 직접 고쳤으면 고친 값만 믿어야 하므로 보내지 않는다.
    imprint_candidates: Optional[list[str]] = None


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


class CorrectLabelRequest(BaseModel):
    image_path: str
    target_id: Optional[Union[int, str]] = None
    points: list  # /upload가 내려준 정규화(0~1) 폴리곤 좌표 그대로 - 탐지 영역 자체는 맞음
    field: str  # "shape" | "color" | "imprint"
    value: str


class SignupRequest(BaseModel):
    uuid: str
    name: str
    age: int
    platform: str = "web"  # "web" | "app" - FE가 네이티브 앱 웹뷰인지 구분해서 보낸다


class TutorialStatusRequest(BaseModel):
    completed: bool


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


def _contour_from_points(points: list, image_shape) -> np.ndarray:
    """/upload가 내려준 정규화(0~1) 폴리곤 좌표를 원본 이미지 픽셀 좌표의
    컨투어로 되돌린다 - 탐지 영역은 맞으므로 재탐지 없이 그대로 재구성."""
    h, w = image_shape[:2]
    pts = np.array([[round(x * w), round(y * h)] for x, y in points], dtype=np.int32)
    return pts.reshape(-1, 1, 2)


def _save_label_correction(body: "CorrectLabelRequest") -> None:
    """탐지 영역(폴리곤)은 맞는데 모양/색상/각인 "라벨"만 틀렸을 때 사용자가
    직접 고친 정답을 raw 데이터로 쌓아둔다 - 마스킹 교정(_save_correction)과
    달리 마스크를 다시 그릴 필요 없이, 이미 탐지된 폴리곤(points)에서
    마스크/크롭을 그대로 재구성한다. 모양/색상 재학습엔 마스크(컨투어)가,
    각인 재학습엔 크롭 이미지가 필요해서 어느 필드를 고쳤든 둘 다 저장해둔다."""
    image = cv2.imread(body.image_path)
    if image is None:
        raise HTTPException(400, f"이미지를 찾을 수 없습니다: {body.image_path}")

    contour = _contour_from_points(body.points, image.shape)

    mask = np.zeros(image.shape[:2], dtype=np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, -1)

    x, y, w, h = cv2.boundingRect(contour)
    pad = int(0.05 * max(w, h))
    x0, y0 = max(x - pad, 0), max(y - pad, 0)
    x1, y1 = min(x + w + pad, image.shape[1]), min(y + h + pad, image.shape[0])
    crop = image[y0:y1, x0:x1]

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    stem = f"{Path(body.image_path).stem}_{body.target_id}"
    mask_path = LABEL_CORRECTIONS_DIR / f"{ts}_{stem}_mask.png"
    crop_path = LABEL_CORRECTIONS_DIR / f"{ts}_{stem}_crop.png"
    cv2.imwrite(str(mask_path), mask)
    cv2.imwrite(str(crop_path), crop)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "image_path": body.image_path,
        "target_id": body.target_id,
        "field": body.field,
        "value": body.value,
        "mask_path": str(mask_path),
        "crop_path": str(crop_path),
    }
    with open(LABEL_CORRECTIONS_LOG, "a", encoding="utf-8") as f:
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
                # ML(RandomForest)이 서빙됐을 때만 0~1 값, 규칙 기반 폴백이면 null
                # (규칙 기반은 확률 개념이 없음). 색상은 애초에 확률 기반이 아니라 신뢰도 없음.
                "shape_confidence": r.get("shape_confidence"),
                "color": r["color"],
                "shape_label": pill_service.get_shape_label(r["shape"]),
                "color_label": pill_service.get_color_label(r["color"]),
                "color_rgb": list(r["color_rgb"]),
                "imprint": r.get("imprint", ""),
                # 판독 후보 전체(신뢰도 높은 순, 첫 번째가 imprint와 같음) - FE가
                # /drug_lookup에 그대로 넘기면 DB 대조가 차순위 후보까지 활용한다.
                "imprint_candidates": r.get("imprint_candidates", []),
                # ML(CRNN)이 서빙됐을 때만 0~1 값, EasyOCR 폴백이면 null - FE가 낮은
                # 신뢰도일 때 사용자 교정을 유도할지 판단하는 근거로 쓴다.
                "imprint_confidence": r.get("imprint_confidence"),
                "points": points,
            }
        )
    return pills


@app.get("/app_version_check")
def app_version_check(version_code: str, platform: str = "android"):
    """네이티브 앱이 FE(/intro)를 열 때 자기 버전을 쿼리파라미터로 넘기고,
    FE가 그 값을 그대로 실어 호출한다. platform은 지금은 android만
    대상이고(web은 항상 통과). 해당 platform+version_code가 테이블에
    등록돼 있고 is_active면 통과, 그 외(미등록 포함)는 전부 거절이다.
    거절이면 FE가 store_url로 구글 플레이스토어로 넘긴다."""
    if platform != "android":
        return {"allowed": True}
    try:
        row = app_version_model.get_by_version(platform, version_code)
        if row is not None and row["is_active"]:
            return {"allowed": True}
        store_url = row["store_url"] if row is not None else app_version_model.get_latest_store_url(platform)
    except pymysql.MySQLError:
        raise HTTPException(503, "일시적인 오류입니다. 잠시 후 다시 시도해주세요.")
    return {"allowed": False, "store_url": store_url}


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

    # 폰 사진엔 위치정보(GPS) 등 EXIF 메타데이터가 남아있을 수 있다 - cv2로
    # 다시 인코딩해서 저장하면 픽셀만 남고 메타데이터는 사라진다(수집 자체를
    # 안 하는 것과 같은 효과 - 개인정보처리방침에 별도 항목을 추가할 필요가 없다).
    cv2.imwrite(str(dest), cv_image)

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
        "shape_confidence": result.get("shape_confidence"),
        "color": result["color"],
        "shape_label": pill_service.get_shape_label(result["shape"]),
        "color_label": pill_service.get_color_label(result["color"]),
        "color_rgb": list(result["color_rgb"]),
        "imprint": result.get("imprint", ""),
        "imprint_candidates": result.get("imprint_candidates", []),
        "imprint_confidence": result.get("imprint_confidence"),
    }


@app.post("/drug_lookup")
def drug_lookup(body: DrugLookupRequest):
    """모양+색상(+각인)으로 후보 약을 찾는다 (결과화면에서 사용, '약 찾기').
    실제 DB(product 테이블, batch가 채워 넣음) 조회 결과를 돌려준다."""
    drugs = pill_service.find_drugs(body.shape, body.color, body.imprint, body.imprint_candidates)
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


@app.post("/correct_label")
def correct_label(body: CorrectLabelRequest):
    """판독/결과화면에서 언제든 "모양/색상/각인이 틀렸다"고 사용자가 직접
    고친 값을 기록한다 (탐지 영역은 맞으므로 마스킹 불필요). 화면 응답에
    영향 없이 재학습용 raw 데이터만 남기고, 실제 약 찾기는 FE가 고친 값으로
    바로 /drug_lookup을 호출해 처리한다."""
    if body.field not in ("shape", "color", "imprint"):
        raise HTTPException(400, f"알 수 없는 field 입니다: {body.field}")
    try:
        _save_label_correction(body)
    except HTTPException:
        raise
    except Exception:
        pass  # 학습 데이터 저장 실패가 사용자 응답을 막으면 안 된다
    return {"status": "ok"}


@app.post("/signup")
def signup(body: SignupRequest):
    """하이브리드 앱 최초 실행 시 이름/나이만 받아 기기 UUID로 가입한다
    (로그인/비밀번호 없음). 이미 가입된 UUID면 새로 만들지 않고 기존
    정보를 그대로 돌려준다 - 앱이 같은 UUID로 재요청하는 경우(재실행 등)
    대비."""
    if body.platform not in ("web", "app"):
        raise HTTPException(400, f"알 수 없는 platform 입니다: {body.platform}")
    try:
        existing = user_model.find_by_uuid(body.uuid)
        if existing:
            return existing
        return user_model.create(body.uuid, body.name, body.age, body.platform)
    except pymysql.MySQLError:
        # DB 연결이 잠깐 끊긴 경우 - 처리 안 된 예외로 그냥 죽으면 FastAPI가
        # CORS 헤더 없는 500을 내려보내 브라우저에는 "CORS 오류"로만 보이고
        # 원인을 알 수 없게 된다(가입 화면 전체가 막힌 것처럼 보임). 503으로
        # 명확히 응답해 FE가 재시도를 유도할 수 있게 한다.
        raise HTTPException(503, "일시적인 오류입니다. 잠시 후 다시 시도해주세요.")


@app.get("/users/{user_uuid}")
def get_user(user_uuid: str):
    """가입 여부 확인 및 프로필(튜토리얼 완료 여부 포함) 조회."""
    try:
        user = user_model.find_by_uuid(user_uuid)
    except pymysql.MySQLError:
        raise HTTPException(503, "일시적인 오류입니다. 잠시 후 다시 시도해주세요.")
    if not user:
        raise HTTPException(404, "가입되지 않은 사용자입니다.")
    return user


@app.patch("/users/{user_uuid}/tutorial")
def set_tutorial_status(user_uuid: str, body: TutorialStatusRequest):
    """튜토리얼 완료 처리, 또는 설정 화면에서 '다시 보기' 선택 시 false로 리셋."""
    try:
        if not user_model.find_by_uuid(user_uuid):
            raise HTTPException(404, "가입되지 않은 사용자입니다.")
        user_model.set_tutorial_completed(user_uuid, body.completed)
    except pymysql.MySQLError:
        raise HTTPException(503, "일시적인 오류입니다. 잠시 후 다시 시도해주세요.")
    return {"status": "ok"}


@app.get("/drug_search")
def drug_search(q: str):
    """약 이름/제조사명으로 직접 검색 (사진 없이, 식약처 의약품 정보 테이블 조회)."""
    q = q.strip()
    if not q:
        return {"drugs": []}
    return {"drugs": pill_service.search_drugs(q)}
