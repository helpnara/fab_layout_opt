"""Fab 도메인 모델.

시뮬레이션의 최소 단위는 **lot**(웨이퍼 25장)이다. 웨이퍼 단위로 모델링하면 계산량이
25배가 되고, 실제 fab도 lot 단위로 반송·처리한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .geometry import SlotId

ProcessMode = Literal["single", "batch"]


@dataclass(frozen=True, slots=True)
class ToolGroup:
    """동일 공정을 수행하는 대체 가능한 설비들의 집합 (예: 스캐너 3대 = PHOTO)."""

    gid: str
    name: str
    count: int
    """기본 설비 대수. 최적화 시 결정 변수가 된다."""
    process_minutes: float
    """lot(배치 설비는 배치) 1회 처리시간의 평균."""
    mode: ProcessMode = "single"
    batch_size: int = 1
    """배치 설비가 한 번에 처리하는 lot 수. single이면 1."""
    mtbf_h: float = 1e9
    """평균 고장간격 (가동시간 기준)."""
    mttr_h: float = 0.0
    process_cv: float = 0.15
    """처리시간 변동계수. 0이면 결정론적."""
    family: str = ""
    """공정 계열 (노광·식각·성막·열처리·평탄화·주입·계측). 그룹화·리포트용 메타데이터.

    렌더링 색상은 여기서 정하지 않는다. 설비 그룹이 11개라 계열별 색을 고정 배정하면
    구별 가능한 범주 색 수(8)를 넘겨 색맹 안전성이 깨진다. 배치도는 이름 라벨로
    정체성을, 순차 램프로 크기를 표현한다 (`viz/svg.py`).
    """

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError(f"{self.gid}: count >= 0")
        if self.process_minutes <= 0:
            raise ValueError(f"{self.gid}: process_minutes > 0")
        if self.mode == "batch" and self.batch_size < 2:
            raise ValueError(f"{self.gid}: batch 설비는 batch_size >= 2")
        if self.mode == "single" and self.batch_size != 1:
            raise ValueError(f"{self.gid}: single 설비는 batch_size == 1")

    @property
    def availability(self) -> float:
        """가동률 A = MTBF / (MTBF + MTTR)."""
        return self.mtbf_h / (self.mtbf_h + self.mttr_h)

    @property
    def minutes_per_lot(self) -> float:
        """설비 점유 시간을 lot 1개분으로 환산한 값 (능력 계산용).

        배치 설비는 batch_size개 lot이 처리시간을 나눠 쓴다.
        """
        return self.process_minutes / self.batch_size

    def instances(self, count: int | None = None) -> tuple[str, ...]:
        """설비 인스턴스 ID (`PHOTO#1`, `PHOTO#2`, ...)."""
        n = self.count if count is None else count
        return tuple(f"{self.gid}#{i}" for i in range(1, n + 1))


@dataclass(frozen=True, slots=True)
class Step:
    """라우트의 공정 단계 하나."""

    index: int
    """0-based 라우트 내 순번."""
    group: str
    layer: int
    """이 스텝이 속한 레이어 번호 (1-based). 재진입 구조를 드러내기 위한 메타데이터."""


@dataclass(frozen=True, slots=True)
class Product:
    """제품 = 라우트 + 제품 믹스 비중."""

    pid: str
    name: str
    steps: tuple[Step, ...]
    mix: float
    """전체 투입 중 이 제품의 비율. Fab 내 총합 1.0."""
    color: str = "#888888"

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def layer_count(self) -> int:
        return max((s.layer for s in self.steps), default=0)

    def visit_counts(self) -> dict[str, int]:
        """설비 그룹별 방문 횟수. 재진입 정도를 나타낸다."""
        out: dict[str, int] = {}
        for s in self.steps:
            out[s.group] = out.get(s.group, 0) + 1
        return out

    def move_count(self) -> int:
        """lot 1개가 요구하는 반송 횟수 = 스텝 수 (투입 지점 → 첫 설비 포함)."""
        return len(self.steps)


@dataclass(frozen=True, slots=True)
class TransportSpec:
    """반송 시스템 파라미터.

    실제 fab의 AMHS는 **bay 내부(intrabay)와 bay 간(interbay)이 분리**되어 있다.
    bay를 넘어가는 lot은 출발 bay 입구의 스토커에 입고되고, interbay 반송으로 옮겨진 뒤
    도착 bay 스토커에서 재출고된다. 이 입출고는 초가 아니라 **분 단위** 비용이다.

    따라서 이동 1회의 비용은 거리만으로 결정되지 않는다.

        스토커 조작 횟수 = 넘는 bay 경계의 수
            같은 bay 내부 이동      → 0회
            다른 bay로 이동          → 2회 (출발 bay 출고 + 도착 bay 입고)
            투입 스토커 → 첫 설비    → 1회
            마지막 설비 → 출하 스토커 → 1회

    이 구조가 "공정을 같은 bay 안에서 이어가라"는 bay 배치의 본질적 동기다.
    M1 실측에서 이 요소를 빼면 배치가 처리량에 사실상 영향을 주지 못했다
    (반송 부하가 처리시간의 1.9%, 반송차 가동률 21%). 넣으면 6% / 66%가 된다.
    """

    vehicles: int = 2
    """반송차 대수. 최적화 시 결정 변수가 된다."""
    speed_mps: float = 1.5
    load_time_s: float = 15.0
    unload_time_s: float = 15.0
    handoff_time_s: float = 10.0
    """설비 포트 접속 (측면 오프셋 호이스트 동작을 여기에 흡수시킨다)."""
    stocker_time_s: float = 120.0
    """스토커 입고 또는 출고 1회 소요시간. bay 경계를 넘을 때만 발생한다."""

    @property
    def fixed_handling_s(self) -> float:
        """거리·bay와 무관한 이동 1회당 고정시간."""
        return self.load_time_s + self.unload_time_s + 2 * self.handoff_time_s

    def move_seconds(self, distance_m: float, stocker_ops: int = 0) -> float:
        """적차 이동 1회의 반송차 점유 시간."""
        return (
            self.fixed_handling_s
            + distance_m / self.speed_mps
            + stocker_ops * self.stocker_time_s
        )

    def empty_seconds(self, distance_m: float) -> float:
        """빈차 회송 시간. 스토커 조작은 없다."""
        return distance_m / self.speed_mps


@dataclass(frozen=True, slots=True)
class Fab:
    """공정 흐름 + 설비 그룹 정의. 배치(layout)와는 분리되어 있다."""

    name: str
    groups: dict[str, ToolGroup]
    products: dict[str, Product]
    wafers_per_lot: int = 25
    transport: TransportSpec = field(default_factory=TransportSpec)
    source: str = "builtin"
    """데이터 출처. 벤치마크 원본이 아님을 표시하기 위한 필드."""

    def __post_init__(self) -> None:
        total = sum(p.mix for p in self.products.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"제품 믹스 합이 1.0이 아니다: {total}")
        for p in self.products.values():
            for s in p.steps:
                if s.group not in self.groups:
                    raise ValueError(f"{p.pid} step {s.index}: 미정의 그룹 {s.group}")

    @property
    def tool_counts(self) -> dict[str, int]:
        return {g.gid: g.count for g in self.groups.values()}

    @property
    def total_tools(self) -> int:
        return sum(g.count for g in self.groups.values())

    def tool_instances(self, counts: dict[str, int] | None = None) -> tuple[str, ...]:
        """모든 설비 인스턴스 ID. counts로 대수 구성을 덮어쓸 수 있다."""
        out: list[str] = []
        for gid in self.groups:
            n = self.groups[gid].count if counts is None else counts[gid]
            out.extend(self.groups[gid].instances(n))
        return tuple(out)

    @staticmethod
    def group_of(instance: str) -> str:
        """`PHOTO#2` → `PHOTO`."""
        return instance.split("#", 1)[0]

    def weighted_visits(self) -> dict[str, float]:
        """제품 믹스 가중 그룹별 방문 횟수 (lot 1개당)."""
        out: dict[str, float] = {gid: 0.0 for gid in self.groups}
        for p in self.products.values():
            for gid, n in p.visit_counts().items():
                out[gid] += p.mix * n
        return out

    def weighted_moves_per_lot(self) -> float:
        return sum(p.mix * p.move_count() for p in self.products.values())

    def raw_process_hours(self, pid: str | None = None) -> float:
        """순수 처리시간 합 (대기 0 가정). X-factor의 분모.

        배치 설비도 lot은 배치 전체 시간을 겪으므로 batch_size로 나누지 **않는다**.
        능력 계산의 `minutes_per_lot`과 구분해야 하는 지점이다.
        """
        if pid is not None:
            p = self.products[pid]
            return sum(self.groups[s.group].process_minutes for s in p.steps) / 60.0
        return sum(p.mix * self.raw_process_hours(p.pid) for p in self.products.values())


# ---- 배치 (Layout) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Assignment:
    """설비 인스턴스 → slot 배치. 최적화의 주 결정 변수."""

    placement: dict[str, SlotId]

    def __post_init__(self) -> None:
        slots = list(self.placement.values())
        if len(set(slots)) != len(slots):
            dupes = {s for s in slots if slots.count(s) > 1}
            raise ValueError(f"한 slot에 설비가 둘 이상 배치되었다: {sorted(map(str, dupes))}")

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(self.placement)

    def slot_of(self, instance: str) -> SlotId:
        return self.placement[instance]

    def occupied(self) -> frozenset[SlotId]:
        return frozenset(self.placement.values())

    def group_slots(self, gid: str) -> tuple[SlotId, ...]:
        """한 그룹에 속한 설비들이 차지한 slot들."""
        return tuple(
            slot for inst, slot in self.placement.items() if Fab.group_of(inst) == gid
        )

    def bay_of_group(self, gid: str) -> set[int]:
        return {s.bay for s in self.group_slots(gid)}
