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
