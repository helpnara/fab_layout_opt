"""내장 데이터셋 `midfab` — 탐색공간을 키운 중규모 fab.

⚠️ `smallfab-21`과 마찬가지로 **벤치마크 원본이 아니라 구조를 참조한 합성 데이터**다
(`docs/SPEC.md` §4.1).

**왜 키웠는가.** M4의 탐색공간 점검에서 `smallfab-21`은 기능별 배치가 이미 분포 바깥
(무작위 표본 중 이기는 것 0개)이었고, 최적화 여지가 5% 남짓이었다. 원인은 크기가
아니라 **구조**다.

    설비 그룹이 11개뿐이고 bay가 5개라, "같은 그룹은 같은 bay에" 규칙이
    거의 그대로 최적해가 된다. 고민할 것이 남지 않는다.

그래서 규모를 키우면서 **기능별 배치가 자동으로 최적이 되지 않는 구조**를 넣었다.

1. **트랙을 노광에서 분리했다.** 실제 fab에서 코터/디벨로퍼(트랙)는 스캐너와 짝을
   이뤄 동작하며, lot은 `트랙 → 노광 → 트랙` 순으로 오간다. 트랙 방문 횟수가 노광의
   2배라 **전체 그룹 중 가장 많다.** 기능별 배치는 트랙을 한 bay에, 노광을 다른 bay에
   몰아넣어 이 왕복을 전부 bay 교차로 만든다 — 최적화기가 고쳐야 할 지점이 생긴다.
2. **식각·증착·계측을 세분했다.** 폴리/메탈/산화막 식각, 산화막/질화막 CVD,
   CD/오버레이 계측. 어느 것끼리 bay를 공유할지가 실제 결정이 된다.
3. **제품을 3종으로 늘렸다.** 라우트별 방문 분포가 달라 한 배치가 모두를 만족시키지
   못한다.

설비 대수는 **목표 처리량과 그룹별 여유율에서 역산**한다. 손으로 적은 표를 두면 규모를
바꿀 때마다 다시 맞춰야 하고, 근접 병목 구조가 깨지기 쉽다.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from ..core.geometry import BaySpec, LayoutGeometry
from ..core.model import Fab, Product, Step, ToolGroup, TransportSpec

DATASET_NAME = "midfab"


@dataclass(frozen=True, slots=True)
class _GroupSpec:
    gid: str
    name: str
    minutes: float
    mode: str
    batch: int
    mtbf_h: float
    mttr_h: float
    cv: float
    family: str
    slack: float
    """목표 처리량 대비 남길 여유. 1.05면 병목, 2.0이면 넉넉하다.

    근접 병목이 여럿이어야 최적화 여지가 생긴다. 병목이 하나만 압도적이면 답이
    자명해지고, 전부 여유면 배치가 무관해진다.
    """


_GROUPS: tuple[_GroupSpec, ...] = (
    # 노광 계열 — 트랙이 노광의 2배로 방문된다
    _GroupSpec("PHOTO_ARF", "노광 ArF", 70.0, "single", 1, 70.0, 7.8, 0.15, "노광", 1.04),
    _GroupSpec("PHOTO_KRF", "노광 KrF", 55.0, "single", 1, 90.0, 8.5, 0.15, "노광", 1.22),
    _GroupSpec("TRACK", "코터·디벨로퍼", 25.0, "single", 1, 200.0, 6.0, 0.12, "노광", 1.12),
    # 식각
    _GroupSpec("ETCH_POLY", "폴리 식각", 50.0, "single", 1, 100.0, 8.7, 0.15, "식각", 1.55),
    _GroupSpec("ETCH_METAL", "메탈 식각", 55.0, "single", 1, 95.0, 9.0, 0.15, "식각", 1.30),
    _GroupSpec("ETCH_OXIDE", "산화막 식각", 45.0, "single", 1, 110.0, 8.0, 0.15, "식각", 1.06),
    _GroupSpec("ETCH_WET", "습식식각", 60.0, "batch", 4, 200.0, 10.5, 0.12, "식각", 1.90),
    # 열처리
    _GroupSpec("DIFF_FURN", "확산·산화로", 240.0, "batch", 6, 300.0, 15.8, 0.08, "열처리", 1.10),
    _GroupSpec("RTP", "급속열처리", 30.0, "single", 1, 150.0, 9.0, 0.12, "열처리", 1.45),
    # 성막
    _GroupSpec("CVD_OX", "산화막 CVD", 45.0, "single", 1, 120.0, 9.0, 0.15, "성막", 1.05),
    _GroupSpec("CVD_NI", "질화막 CVD", 40.0, "single", 1, 120.0, 9.0, 0.15, "성막", 1.35),
    _GroupSpec("PVD", "물리기상증착", 35.0, "single", 1, 120.0, 9.0, 0.15, "성막", 1.18),
    # 평탄화·세정
    _GroupSpec("CMP", "화학기계연마", 40.0, "single", 1, 110.0, 7.0, 0.18, "평탄화", 1.08),
    _GroupSpec("CLEAN", "세정", 25.0, "batch", 4, 250.0, 10.4, 0.12, "평탄화", 1.70),
    # 이온주입
    _GroupSpec("IMPL_HC", "고전류 이온주입", 50.0, "single", 1, 90.0, 12.3, 0.15, "주입", 1.60),
    _GroupSpec("IMPL_MC", "중전류 이온주입", 55.0, "single", 1, 90.0, 12.3, 0.15, "주입", 1.50),
    # 계측
    _GroupSpec("METRO_CD", "CD 계측", 20.0, "single", 1, 400.0, 8.2, 0.20, "계측", 1.25),
    _GroupSpec("METRO_OVL", "오버레이 계측", 15.0, "single", 1, 400.0, 8.2, 0.20, "계측", 1.75),
)

# ---- 레이어 유형 ---------------------------------------------------------
# 노광(PHOTO_*) 앞뒤로 트랙이 자동 삽입된다 (`_expand`).

_LAYERS: dict[str, tuple[str, ...]] = {
    "WELL": ("CLEAN", "CVD_OX", "DIFF_FURN", "PHOTO_KRF", "IMPL_HC", "ETCH_OXIDE",
             "RTP", "METRO_CD"),
    "STI": ("CLEAN", "CVD_NI", "PHOTO_KRF", "ETCH_OXIDE", "ETCH_WET", "DIFF_FURN",
            "CMP", "METRO_CD"),
    "GATE": ("CLEAN", "DIFF_FURN", "CVD_NI", "PHOTO_ARF", "ETCH_POLY", "IMPL_MC",
             "RTP", "METRO_CD", "METRO_OVL"),
    "SD": ("CLEAN", "PHOTO_KRF", "IMPL_HC", "IMPL_MC", "RTP", "ETCH_WET", "METRO_CD"),
    "CONTACT": ("CLEAN", "CVD_OX", "CMP", "PHOTO_ARF", "ETCH_OXIDE", "PVD", "CMP",
                "METRO_CD", "METRO_OVL"),
    "METAL": ("CLEAN", "PVD", "CVD_OX", "PHOTO_ARF", "ETCH_METAL", "CMP",
              "METRO_CD", "METRO_OVL"),
    "VIA": ("CLEAN", "CVD_OX", "PHOTO_ARF", "ETCH_OXIDE", "PVD", "CMP", "METRO_CD"),
    "PASS": ("CLEAN", "CVD_NI", "PHOTO_KRF", "ETCH_OXIDE", "METRO_CD"),
    "CAP": ("CLEAN", "CVD_NI", "DIFF_FURN", "PHOTO_ARF", "ETCH_OXIDE", "DIFF_FURN",
            "CMP", "METRO_CD"),
    "TRENCH": ("CLEAN", "CLEAN", "DIFF_FURN", "CVD_NI", "PHOTO_ARF", "ETCH_OXIDE",
               "ETCH_WET", "DIFF_FURN", "METRO_CD"),
}

_PRODUCTS: dict[str, tuple[str, tuple[str, ...], float]] = {
    "LOGIC_A": ("로직 A", (
        "WELL", "STI", "GATE", "SD", "CONTACT",
        "METAL", "VIA", "METAL", "VIA", "METAL", "VIA", "METAL", "VIA", "PASS",
    ), 0.50),
    "MEM_B": ("메모리 B", (
        "WELL", "STI", "TRENCH", "CAP", "GATE", "SD", "CONTACT",
        "METAL", "VIA", "METAL", "PASS",
    ), 0.35),
    "ANALOG_C": ("아날로그 C", (
        "WELL", "STI", "GATE", "SD", "CONTACT", "METAL", "VIA", "PASS",
    ), 0.15),
}


def _expand(layer: tuple[str, ...]) -> tuple[str, ...]:
    """노광 앞뒤에 트랙을 넣는다 — lot은 트랙 → 노광 → 트랙으로 오간다.

    이 왕복 때문에 트랙 방문 횟수가 노광의 2배가 되고, 전체 그룹 중 최다가 된다.
    기능별 배치가 트랙과 노광을 다른 bay에 두면 이 왕복이 전부 bay 교차가 되므로,
    **최적화기가 고칠 여지**가 생긴다.
    """
    out: list[str] = []
    for gid in layer:
        if gid.startswith("PHOTO_"):
            out += ["TRACK", gid, "TRACK"]
        else:
            out.append(gid)
    return tuple(out)


def _route(layer_types: tuple[str, ...]) -> tuple[Step, ...]:
    steps: list[Step] = []
    for layer_no, lt in enumerate(layer_types, start=1):
        for gid in _expand(_LAYERS[lt]):
            steps.append(Step(index=len(steps), group=gid, layer=layer_no))
    return tuple(steps)


def _weighted_visits() -> dict[str, float]:
    out: dict[str, float] = {g.gid: 0.0 for g in _GROUPS}
    for pid, (_, layer_types, mix) in _PRODUCTS.items():
        for step in _route(layer_types):
            out[step.group] += mix
    return out


def required_counts(target_lots_per_day: float) -> dict[str, int]:
    """목표 처리량과 그룹별 여유율에서 설비 대수를 역산한다.

        대수 = ⌈목표 × 여유 × 요구작업량 ÷ (1440 × 가동률)⌉
    """
    visits = _weighted_visits()
    counts: dict[str, int] = {}
    for g in _GROUPS:
        per_lot = g.minutes / (g.batch if g.mode == "batch" else 1)
        workload = visits[g.gid] * per_lot
        availability = g.mtbf_h / (g.mtbf_h + g.mttr_h)
        counts[g.gid] = max(1, ceil(
            target_lots_per_day * g.slack * workload / (1440.0 * availability)
        ))
    return counts


def build_midfab(
    target_lots_per_day: float = 20.0,
    vehicles: int = 16,
    counts: dict[str, int] | None = None,
) -> Fab:
    """중규모 fab. `target_lots_per_day`로 규모를 조절한다."""
    resolved = counts or required_counts(target_lots_per_day)
    groups = {
        g.gid: ToolGroup(g.gid, g.name, resolved[g.gid], g.minutes, g.mode,
                         g.batch if g.mode == "batch" else 1,
                         g.mtbf_h, g.mttr_h, g.cv, g.family)
        for g in _GROUPS
    }
    colors = ("#2a78d6", "#eb6834", "#1baf7a")
    products = {
        pid: Product(pid, name, _route(layers), mix, colors[i % len(colors)])
        for i, (pid, (name, layers, mix)) in enumerate(_PRODUCTS.items())
    }
    return Fab(
        name=DATASET_NAME,
        groups=groups,
        products=products,
        wafers_per_lot=25,
        transport=TransportSpec(vehicles=vehicles),
        source="synthetic (SMT2020/MIMAC 구조 참조, 원본 수치 아님)",
    )


def midfab_geometry(columns: int = 6, positions_per_side: int = 4) -> LayoutGeometry:
    """중규모 배치용 기하 — bay 12개(북 6 / 남 6), bay당 2벽 × 4위치 = 8 slot.

    총 96 slot, spine 길이 81m. `smallfab`(30 slot, 39m)보다 슬롯이 3.2배, spine이
    2배다. bay가 많아지면 "어느 그룹끼리 bay를 공유할 것인가"가 실제 결정이 되고,
    spine이 길어지면 멀리 떨어진 bay 사이의 이동 비용이 커진다.
    """
    bays = []
    n = 1
    for side in ("N", "S"):
        for col in range(columns):
            bays.append(BaySpec(n, col, side, f"{'북' if side == 'N' else '남'}{col + 1}"))
            n += 1
    return LayoutGeometry(bays=tuple(bays), positions_per_side=positions_per_side)


def load(name: str = DATASET_NAME, **kwargs) -> Fab:
    if name != DATASET_NAME:
        raise KeyError(f"내장 데이터셋 '{name}' 없음. 사용 가능: {DATASET_NAME}")
    return build_midfab(**kwargs)
