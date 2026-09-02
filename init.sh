#!/usr/bin/env bash
# 리눅스 서버 초기 세팅 스크립트. venv 생성 + 파이썬 패키지 설치 +
# opencv/pytesseract가 필요로 하는 시스템 패키지까지 한 번에 처리한다.
# 실행: ./init.sh (sudo 권한 필요 - apt 설치 때문)
set -euo pipefail

cd "$(dirname "$0")"

echo "== 시스템 패키지 설치 (venv, tesseract-ocr, opencv 런타임 라이브러리) =="
# 서버에 등록된 다른 PPA(예: PHP용)가 이 Ubuntu 버전을 아직 지원 안 해서
# 404가 날 수 있다. 우리가 쓸 패키지는 전부 기본 저장소(main/security)에
# 있으니, update 단계의 개별 저장소 실패는 무시하고 계속 진행한다.
sudo apt-get update -y || true
sudo apt-get install -y python3-venv python3-pip tesseract-ocr libgl1 libglib2.0-0

echo "== venv 생성 =="
if [ ! -d venv ]; then
    python3 -m venv venv
else
    echo "venv 이미 있음, 건너뜀"
fi

echo "== 파이썬 패키지 설치 =="
venv/bin/pip install --upgrade pip
# torch를 그냥 requirements.txt로 설치하면 CUDA(GPU)가 포함된 풀버전(500MB+)을
# 받는다. 이 서버엔 GPU가 없으니 CPU 전용 빌드(훨씬 작음)로 먼저 깔아두고,
# 나머지 requirements.txt를 설치하면 pip이 torch/torchvision은 이미 설치된
# 걸로 보고 건너뛴다.
venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
venv/bin/pip install -r requirements.txt

echo "== 설치 확인 =="
venv/bin/python -c "import fastapi, cv2, torch, easyocr; print('ok')"

echo "== FastSAM 모델 가중치 다운로드 (없으면) =="
# 첫 실제 사용자 요청 때 지연/실패 없도록 미리 받아둔다 (약 23MB).
if [ ! -f FastSAM-s.pt ]; then
    venv/bin/python -c "from ultralytics import FastSAM; FastSAM('FastSAM-s.pt')"
else
    echo "FastSAM-s.pt 이미 있음, 건너뜀"
fi

echo "== 완료. 실행: ./run.sh =="
