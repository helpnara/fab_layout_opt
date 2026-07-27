"""내장 데이터셋 `smallfab-21`.

⚠️ **이 데이터는 벤치마크 원본이 아니다.**

SMT2020 (Kopp, Hassoun, Kalir, Mönch 2020, IEEE Trans. Semiconductor Manufacturing) 및
MIMAC (SEMATECH 1997) testbed의 **구조**를 참조한 합성 데이터다. 원본은 설비 1,000대급
규모이고 이 프로젝트의 대상 범위(10~40대)와 맞지 않으며, 배포처가 실행 환경 네트워크
정책에서 차단되어 있다 (`docs/SPEC.md` §4.1).

벤치마크에서 가져온 것은 다음의 *구조적 특징*이다.

- 설비를 그룹 단위로 묶고 라우트는 그룹을 참조한다
- 재진입 흐름: 같은 노광 설비를 레이어마다 다시 방문한다
- 배치 설비(확산로·세정)와 단일 설비가 섞여 있다
- 그룹별 MTBF/MTTR로 가동률이 70~98% 범위에 분포한다
- 제품이 여러 종이고 라우트 길이와 방문 분포가 서로 다르다

수치 자체는 공개 문헌의 통상적 범위를 참고해 정한 자리표시자다. 대외 자료에 결과를
쓸 경우 "벤치마크 구조를 참조한 합성 데이터"로 표기해야 한다.

라우트는 **레이어 템플릿의 연결**로 생성한다. 각 레이어는 실제 공정 순서
(세정 → 증착/열처리 → 노광 → 식각/주입 → 평탄화 → 계측)를 따른다.
`tests/test_builtin.py`가 그룹별 방문 횟수가 의도한 표와 정확히 일치하는지 검증한다.
"""

from __future__ import annotations

from ..core.model import Fab, Product, Step, ToolGroup, TransportSpec

DATASET_NAME = "smallfab-21"

# ---- 설비 그룹 -----------------------------------------------------------

_GROUPS: tuple[ToolGroup, ...] = (
    ToolGroup("PHOTO", "노광 (스캐너+트랙)", 3, 60.0, "single", 1, 80.0, 8.9, 0.15, "노광"),
    ToolGroup("ETCH_DRY", "건식식각", 3, 45.0, "single", 1, 100.0, 8.7, 0.15, "식각"),
    ToolGroup("ETCH_WET", "습식식각", 1, 60.0, "batch", 4, 200.0, 10.5, 0.12, "식각"),
    ToolGroup("DIFF_FURN", "확산·산화로", 2, 240.0, "batch", 6, 300.0, 15.8, 0.08, "열처리"),
    ToolGroup("CVD", "화학기상증착", 2, 40.0, "single", 1, 120.0, 9.0, 0.15, "성막"),
    ToolGroup("PVD", "물리기상증착", 1, 35.0, "single", 1, 120.0, 9.0, 0.15, "성막"),
    ToolGroup("CMP", "화학기계연마", 2, 40.0, "single", 1, 110.0, 7.0, 0.18, "평탄화"),
    ToolGroup("IMPL_HC", "고전류 이온주입", 2, 50.0, "single", 1, 90.0, 12.3, 0.15, "주입"),
    ToolGroup("IMPL_MC", "중전류 이온주입", 2, 55.0, "single", 1, 90.0, 12.3, 0.15, "주입"),
    ToolGroup("CLEAN", "세정", 1, 25.0, "batch", 4, 250.0, 10.4, 0.12, "평탄화"),
    ToolGroup("METRO", "계측", 2, 20.0, "single", 1, 400.0, 8.2, 0.20, "계측"),
)

# ---- 라우트: 레이어 템플릿 -----------------------------------------------
# 제품 LOGIC_A — 10 레이어 / 84 스텝. PHOTO를 10회 재방문한다.

_LOGIC_A_LAYERS: tuple[tuple[str, ...], ...] = (
    # L1 웰 형성
    ("CLEAN", "CVD", "CVD", "DIFF_FURN", "PHOTO", "IMPL_HC", "ETCH_DRY", "DIFF_FURN", "METRO"),
    # L2 STI (소자 분리)
    ("CLEAN", "CVD", "PHOTO", "ETCH_DRY", "ETCH_WET", "DIFF_FURN", "CMP", "METRO"),
    # L3 게이트 산화
    ("CLEAN", "DIFF_FURN", "CVD", "PHOTO", "IMPL_HC", "IMPL_MC", "ETCH_DRY", "METRO"),
    # L4 폴리 게이트
    ("CLEAN", "CVD", "PHOTO", "ETCH_DRY", "IMPL_MC", "IMPL_MC", "ETCH_DRY", "DIFF_FURN", "METRO"),
    # L5 소스/드레인
    ("CLEAN", "CVD", "PHOTO", "IMPL_HC", "IMPL_HC", "IMPL_MC", "DIFF_FURN", "ETCH_DRY",
     "ETCH_WET", "METRO"),
    # L6 콘택
    ("CLEAN", "CVD", "PVD", "PHOTO", "ETCH_DRY", "CMP", "METRO"),
    # L7 메탈1
    ("CLEAN", "PVD", "PVD", "CVD", "PHOTO", "ETCH_DRY", "ETCH_WET", "DIFF_FURN", "CMP", "METRO"),
    # L8 비아1
    ("CLEAN", "CVD", "PVD", "PHOTO", "ETCH_DRY", "ETCH_DRY", "CMP", "METRO"),
    # L9 메탈2
    ("CLEAN", "PVD", "PVD", "CVD", "PHOTO", "ETCH_DRY", "DIFF_FURN", "CMP", "METRO"),
    # L10 패시베이션
    ("CLEAN", "PHOTO", "ETCH_DRY", "ETCH_WET", "CMP", "METRO"),
)

# 제품 MEM_B — 7 레이어 / 68 스텝. 노광은 적고 확산로에 편중된다 (메모리 특성).

_MEM_B_LAYERS: tuple[tuple[str, ...], ...] = (
    # M1 딥 트렌치
    ("CLEAN", "CLEAN", "DIFF_FURN", "CVD", "CVD", "DIFF_FURN", "PHOTO", "IMPL_HC", "IMPL_MC",
     "ETCH_DRY", "ETCH_WET", "DIFF_FURN", "METRO"),
    # M2 커패시터
    ("CLEAN", "CVD", "CVD", "DIFF_FURN", "PHOTO", "IMPL_HC", "IMPL_MC", "ETCH_DRY",
     "DIFF_FURN", "METRO"),
    # M3 게이트
    ("CLEAN", "DIFF_FURN", "CVD", "CVD", "PHOTO", "ETCH_DRY", "ETCH_WET", "DIFF_FURN", "METRO"),
    # M4 비트라인
    ("CLEAN", "CVD", "CVD", "DIFF_FURN", "PHOTO", "ETCH_DRY", "ETCH_DRY", "DIFF_FURN",
     "CMP", "METRO"),
    # M5 워드라인
    ("CLEAN", "CVD", "CVD", "PVD", "PHOTO", "ETCH_DRY", "ETCH_DRY", "DIFF_FURN", "CMP", "METRO"),
    # M6 메탈
    ("CLEAN", "CVD", "PVD", "DIFF_FURN", "PHOTO", "ETCH_DRY", "ETCH_WET", "CMP", "METRO"),
    # M7 패시베이션
    ("CLEAN", "CVD", "PVD", "DIFF_FURN", "PHOTO", "ETCH_DRY", "METRO"),
)

# 검증용 목표 방문 횟수 표. 라우트를 손으로 고칠 때 실수를 잡기 위한 것이며
# tests/test_builtin.py가 실제 생성 결과와 대조한다.

TARGET_VISITS: dict[str, dict[str, int]] = {
    "LOGIC_A": {
        "PHOTO": 10, "ETCH_DRY": 12, "ETCH_WET": 4, "DIFF_FURN": 8, "CVD": 10,
        "PVD": 6, "CMP": 6, "IMPL_HC": 4, "IMPL_MC": 4, "CLEAN": 10, "METRO": 10,
    },
    "MEM_B": {
        "PHOTO": 7, "ETCH_DRY": 9, "ETCH_WET": 3, "DIFF_FURN": 12, "CVD": 12,
        "PVD": 3, "CMP": 3, "IMPL_HC": 2, "IMPL_MC": 2, "CLEAN": 8, "METRO": 7,
    },
}
TARGET_STEP_COUNTS = {"LOGIC_A": 84, "MEM_B": 68}


def _build_route(layers: tuple[tuple[str, ...], ...]) -> tuple[Step, ...]:
    steps: list[Step] = []
    for layer_no, layer in enumerate(layers, start=1):
        for gid in layer:
            steps.append(Step(index=len(steps), group=gid, layer=layer_no))
    return tuple(steps)


def build_smallfab21(
    mix_logic: float = 0.7,
    vehicles: int = 2,
    counts: dict[str, int] | None = None,
) -> Fab:
    """내장 데이터셋을 생성한다.

    `counts`로 그룹별 설비 대수를 덮어쓸 수 있다 (최적화가 대수를 바꿀 때 사용).
    """
    groups = {}
    for g in _GROUPS:
        n = g.count if counts is None else counts[g.gid]
        groups[g.gid] = ToolGroup(
            g.gid, g.name, n, g.process_minutes, g.mode, g.batch_size,
            g.mtbf_h, g.mttr_h, g.process_cv, g.family,
        )

    products = {
        # 제품 색상은 검증된 범주 팔레트의 슬롯 1·2 (dataviz 기준)
        "LOGIC_A": Product(
            "LOGIC_A", "로직 A", _build_route(_LOGIC_A_LAYERS), mix_logic, "#2a78d6"
        ),
        "MEM_B": Product(
            "MEM_B", "메모리 B", _build_route(_MEM_B_LAYERS), round(1.0 - mix_logic, 10), "#eb6834"
        ),
    }
    return Fab(
        name=DATASET_NAME,
        groups=groups,
        products=products,
        wafers_per_lot=25,
        transport=TransportSpec(vehicles=vehicles),
        source="synthetic (SMT2020/MIMAC 구조 참조, 원본 수치 아님)",
    )


def load(name: str = DATASET_NAME, **kwargs) -> Fab:
    if name != DATASET_NAME:
        raise KeyError(f"내장 데이터셋 '{name}' 없음. 사용 가능: {DATASET_NAME}")
    return build_smallfab21(**kwargs)
