# fab_layout_opt

반도체 공장(fab) 설비 배치 최적화 시뮬레이터.

Bay 구조 클린룸에서 설비를 어디에 놓고 몇 대를 사야 사이클타임과 처리량이 좋아지는지를
이산사건 시뮬레이션(DES)으로 평가하고 최적화한다.

## 현재 상태

**설계 단계.** 구현 코드는 아직 없다. 상세 설계는 [`docs/SPEC.md`](docs/SPEC.md)를 참조.

## 개요

| 항목 | 내용 |
|---|---|
| 목표 지표 | 사이클타임 / 처리량 (생산능력 = 병목 포화 지점의 생산량) |
| 규모 | 설비 10~40대 소규모 fab (내장 데이터셋은 21대) |
| 공간 모델 | 중앙 spine 통로 + 양측 bay, bay당 slot 6개 |
| 결정 변수 | 설비→bay/slot 배치, 그룹별 설비 대수, 반송기 대수 |
| 제약 | slot 배타, capex 예산, 사이클타임 상한 |
| 모델링 요소 | 재진입 흐름, 설비 고장(MTBF/MTTR), 배치 설비, 반송기 대수 제한 |
| 최적화 | 2단 구조 — 거리 대리지표로 스크리닝 → DES로 검증 |
| 기준선 | 기능별 배치(동종 설비를 한 bay에) = 실무 관행 |
| 산출물 | Python 코어 라이브러리 + CLI + FastAPI + 정적 웹 UI |

## 계획된 사용법

```bash
# 기준선 시뮬레이션
fablayout simulate --scenario scenario.yaml

# 최적화 (2단 파이프라인)
fablayout optimize --scenario scenario.yaml --budget-same-as-baseline

# 웹 UI
uvicorn fablayout.api.app:app --reload
```

## 데이터 출처에 관한 주의

내장 데이터셋 `smallfab-21`은 SMT2020 / MIMAC 벤치마크의 **구조를 참조한 합성 데이터**이며
원본 수치가 아니다. 원본 파일을 확보한 경우 `data/loaders/` 의 로더로 읽을 수 있다.
자세한 배경은 [`docs/SPEC.md` §4.1](docs/SPEC.md)에 있다.
