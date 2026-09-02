# 알약 모양 분류기 (학습 기반)

`app/pill_analyze.py`의 `classify_shape()`는 규칙 기반(원형도/종횡비/꼭짓점 각도
임계값)이라 데이터가 쌓여도 스스로 나아지지 않고, 캡슐-장방형처럼 2D 윤곽선상
원천적으로 애매한 경계는 규칙으로 못 푼다. 이 디렉토리는 그 자리를 대체할
학습 기반 모양 분류기를 로컬에서 개발하는 공간이다.

## 배포 방식
FastSAM(`FastSAM-s.pt`)과 동일한 패턴 - 학습은 여기(로컬)에서만 하고, 서버에는
학습된 가중치 파일만 올린다. 서버 자원(CPU only) 문제로 서버에서 학습은 하지 않는다.

## 파이프라인 순서
1. `dataset_from_reference_images.py` #openAPI 자료
2. `dataset_from_user_confirmations.py` #사용자 반응 자료
3. `train_shape_classifier.py` 
4. `evaluate_shape_classifier.py`
5. `export_weights.py`
6. `infer_shape.py` (서버 쪽 `app/pill_analyze.py`에 연결할 추론 래퍼)


## 규칙 기반 → 학습 기반 전환
1. 서버 추론 준비

- ML/infer_shape.py 완성 — _get_fastsam_model()처럼 모델을 lazy-load하는 싱글턴 + predict_shape(contour) -> str 함수(컨투어→특징 추출→model.predict→라벨 역변환)
- app/pill_analyze.py의 classify_shape() 호출부를 이 함수로 교체(또는 병행). 완전 교체보다는 폴백 유지 추천 — 모델 로드 실패나 예외 시 기존 규칙 기반으로 자동 전환

2. 배포

- ML/models/shape_classifier.joblib을 서버로 전달(FastSAM-s.pt처럼 수동 — git엔 안 올라감, .gitignore 처리 이미 해둠)
- 서버 requirements.txt에 scikit-learn, joblib 추가 필요 — 이건 짚고 넘어가야 합니다. 학습은 로컬에서만 하지만, model.predict()를 돌리는 추론 자체도 scikit-learn 런타임이 있어야 동작합니다("서버는 가중치만 받으면 된다"고 했던 게 정확히는 "학습 코드/데이터셋은 안 가져가도 된다"는 뜻이지, 라이브러리까지 안 깔아도 된다는 뜻은 아니었습니다)

3. 실사용 검증 (제일 중요한 미해결 리스크)

- 지금까지 성능(90.7%)은 전부 스튜디오 참고사진 기준입니다. 실사용자가 휴대폰으로 찍은 사진에서도 이 우위가 유지되는지 검증 안 됨 — 배포 직후엔 규칙 기반과 나란히 로그만 찍는 "섀도 모드"로 돌려보고, 실제로도 이기는지 확인 후 전환하는 게 안전
- ml_confirmations.jsonl이 쌓이면 그걸로 재검증


4. 소수 클래스 리스크 인지

- hexagon/pentagon/triangle 등은 표본이 절대적으로 부족(1~7건)해서 이 클래스들의 실제 신뢰도는 낮습니다 — 배포하더라도 이 한계를 알고 가야 함

5. 재학습 주기

- 데이터(특히 user_confirmations.csv)가 쌓일 때마다 어떻게/언제 재학습할지 정해야 함(수동 트리거 vs 주기적)

