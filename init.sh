#!/usr/bin/env bash
# 리눅스 서버 초기 세팅 스크립트. venv 생성 + 파이썬 패키지 설치 +
# opencv/pytesseract가 필요로 하는 시스템 패키지까지 한 번에 처리한다.
# 실행: ./init.sh (sudo 권한 필요 - apt 설치 때문)
set -euo pipefail

cd "$(dirname "$0")"

echo "== 시스템 패키지 설치 (venv, tesseract-ocr, opencv 런타임 라이브러리) =="
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip tesseract-ocr libgl1 libglib2.0-0

echo "== venv 생성 =="
if [ ! -d venv ]; then
    python3 -m venv venv
else
    echo "venv 이미 있음, 건너뜀"
fi

echo "== 파이썬 패키지 설치 =="
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

echo "== 설치 확인 =="
venv/bin/python -c "import fastapi, cv2, torch, pytesseract; print('ok')"

echo "== 완료. 실행: ./run.sh =="
