# boyak-be
보이는 약 BE

알약 사진을 업로드받아 모양/색상을 판독하는 FastAPI 백엔드.
사람이 교정한 마스크는 `storage/ml_corrections/`에 계속 쌓여서, 나중에
FastSAM(현재 쓰는 세그멘테이션 모델) 파인튜닝 재료로 쓰인다.

## 실행

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
run.bat   # (또는 ./run.sh) - 88 포트로 실행
```

첫 실행 시 `FastSAM-s.pt`(세그멘테이션 모델 가중치, 약 23MB)가 자동
다운로드된다. 각인(imprint) OCR을 쓰려면 Tesseract-OCR이 설치되어 있어야
하고, 설치 경로가 다르면 `app/pill_analyze.py`의 `TESSERACT_CMD`를 맞게
바꿔야 한다.

## API

| 메서드/경로 | 설명 |
|---|---|
| `GET /health` | 상태 확인 |
| `POST /upload` | 이미지 업로드(multipart, field `image`) → 저장 + 분석 결과 반환 |
| `POST /reanalyze` | 마스크로 즉시 재진단 (JSON: `image_path`, `target_id`, `mask_png_base64`) |
| `GET /images/{name}` | 업로드된 이미지 정적 서빙 |

## 지원 범위

`app/pill_analyze.py` 상단 문서 참고 — 알약 2~6개, 서로 겹치지 않게 찍은
사진에서 검증됨. 무더기로 쌓인 사진은 정확도가 크게 떨어진다.

## ML 모델 현황

| 모델 | 방식 | 증분학습 가능 여부 | GPU 필요 |
|---|---|---|---|
| 모양 분류 | RandomForestClassifier | 불가 — 배치 재학습만 가능 (41분/회 확인됨) | 불필요 |
| 사물탐지 | FastSAM(YOLOv8s) | 배치 재학습 (Ultralytics 파인튜닝) | 불필요 — 프로덕션 CPU로 이미 검증됨 |
| 각인 분석 | CRNN(CNN+BiLSTM), 신규 학습 예정 | 배치 재학습 (신규 데이터 누적 시) | 사실상 필요 — Colab로 전환 중 |
| 색상 | OpenCV 룰 | 재학습 대상 아님 | 불필요 |

### 모양 분류 — RandomForestClassifier
- 기존 규칙 기반(원형도/종횡비/꼭짓점 각도 임계값, `classify_shape()`)은 데이터가 쌓여도 스스로 나아지지 않고 캡슐-장방형처럼 원천적으로 애매한 경계를 못 풀어서, 이를 대체할 학습 기반 분류기를 `ML/`에서 개발 중.
- 파이프라인: `dataset_from_reference_images.py`(참고사진) → `dataset_from_user_confirmations.py`(사용자 확정 반응) → `train_shape_classifier.py` → `evaluate_shape_classifier.py` → `export_weights.py` → `infer_shape.py`(서버 추론 래퍼).
- **증분학습 불가**: RandomForest 구조상 새 데이터가 들어오면 처음부터 다시 학습해야 함. 25,000+건 기준 FastSAM 특징 추출 단계가 CPU에서 수십분 걸리고(1회 41분 확인), 그래서 특징 캐시(`ML/train_shape_classifier.py`)로 반복 비용을 줄여둠.
- **GPU 불필요**: 학습/추론 모두 scikit-learn CPU 연산만 사용.
- 배포는 FastSAM과 동일 패턴 — 학습은 로컬에서만 하고, 가중치 파일(`ML/models/shape_classifier.joblib`)만 수동으로 서버에 올림(`.gitignore` 처리됨). 서버 `requirements.txt`에 scikit-learn/joblib 필요.
- 현재 검증된 성능(90.7%)은 스튜디오 참고사진 기준이며, 실사용자 휴대폰 사진 기준 검증은 아직 안 됨 — 배포 시 규칙 기반과 병행하는 섀도 모드 권장. hexagon/pentagon/triangle 등 소수 클래스는 표본 부족(1~7건)으로 신뢰도 낮음.

### 사물탐지 — FastSAM(YOLOv8s)
- 알약 영역을 분리하는 세그멘테이션 모델(`FastSAM-s.pt`, 최초 실행 시 자동 다운로드, 약 23MB).
- **배치 재학습**: 사람이 교정한 마스크(`storage/ml_corrections/`)가 계속 쌓이고, 이를 Ultralytics 파인튜닝 재료로 사용. 상시/증분 학습은 아니고 주기적으로 배치 처리.
- **GPU 불필요**: 프로덕션 환경이 CPU-only인데도 이미 이 상태로 검증되어 운영 중.

### 각인 분석 — CRNN(CNN+BiLSTM), 신규 학습 예정
- 표준 scene-text recognition 구조(CRNN+CTC)로 알약에 새겨진 문자를 인식. 기존 EasyOCR 대비 검증셋(2,300건) 기준 정확 일치율 21.3% → 75.7%로 큰 폭 개선 확인(`ML/infer_imprint.py`, `app/pill_analyze.py` 참고).
- 서버는 `ML/models/imprint_crnn.pt`가 있으면 이 모델 예측을 우선 서빙하고, 없으면 EasyOCR로 폴백.
- **배치 재학습**: 신규 데이터(각인 교정/확정 기록)가 누적될 때마다 `ML/IMPRINT/train_imprint_model.py`(또는 `_v2.py`, 체크포인트 이어학습 `--resume` 지원)로 재학습.
- **GPU 사실상 필요**: 로컬 CPU로도 돌아가지만 느려서, 실제 학습은 Colab GPU(T4) 권장 — `ML/IMPRINT/train_on_colab.ipynb`에 데이터 업로드(임시 nginx static 경로 오픈 후 원복)부터 학습까지 절차가 정리되어 있음. 현재 로컬 CPU 의존에서 Colab 기반으로 전환 중.

### 색상 — OpenCV 룰
- 알약 색상을 미리 정의된 `NAMED_COLORS` 팔레트와의 최소 거리로 매칭하는 순수 규칙 기반 방식 (`app/pill_analyze.py`). 학습 모델이 아니라 **재학습 대상 자체가 아님**.
- `ML/calibrate_color.py`는 "재학습"이 아니라 DB 정답 라벨(`product.color_class1`) + 참고사진 실측값으로 팔레트 기준색을 재보정하는 1회성 캘리브레이션 도구. 파스텔톤처럼 애매한 색이 엉뚱한 카테고리로 스냅되는 문제를 완화하기 위해 만들어짐.
- **GPU 불필요**: 단순 RGB 거리 계산.
