# 실험 결과

`scripts/`의 실험이 남긴 측정값이다. **리포트(`scripts/build_report.py`)가 이 파일을
읽는다** — `midfab`에서 DES를 수십 회 돌리는 측정이라 리포트 생성 중에 다시 계산하기에는
너무 비싸기 때문이다(합쳐서 약 14분). 숫자의 출처는 여전히 커밋된 스크립트이고, 생성
시각이 각 JSON에 함께 남는다.

    surrogate_calibration.json   scripts/calibrate_surrogate.py   M5 게이트 판정
    midfab_space.json            scripts/expand_space.py          규모·분해·대수 재구성

데이터셋이나 시뮬레이션 모델을 바꾸면 두 스크립트를 다시 돌려야 한다.

    python scripts/calibrate_surrogate.py --out results/surrogate_calibration.json
    python scripts/expand_space.py        --out results/midfab_space.json

`tests/test_anneal.py`가 `surrogate_calibration.json`의 판정 결과를 검사한다. 파일이
없으면 그 테스트들은 건너뛴다.
