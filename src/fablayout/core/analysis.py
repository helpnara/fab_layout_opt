"""해석적 사전 계산 — DES 없이 구하는 능력·부하·반송 지표.

세 곳에서 쓴다.

1. **데이터셋 점검**: 병목이 하나만 압도적이면 최적화 여지가 없고, 전부 여유면 배치가
   무관해진다. 근접 병목 구조인지 여기서 확인한다.
2. **생산능력 탐색의 상한**: DES 투입률 램프의 시작점 (`sim/capacity.py`). 실측 능력은
   대기·고장·반송 때문에 반드시 이 상한보다 **낮아야** 한다 — 검증 항목이다.
3. **최적화 대리지표**: 흐름-거리와 해석적 가동률 (`opt/surrogate.py`).

여기 계산은 대기행렬을 무시하므로 낙관적 상한이다. 최종 판정은 항상 DES가 한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .distance import DistanceMatrix
from .geometry import LayoutGeometry, SlotId
from .model import Assignment, Fab

MINUTES_PER_DAY = 1440.0
SECONDS_PER_DAY = 86400.0


# ---- 설비 능력 -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroupLoad:
    gid: str
    name: str
    tools: int
    availability: float
    visits_per_lot: float
    workload_min_per_lot: float
    """lot 1개가 이 그룹의 설비 시간을 몇 분 요구하는가 (배치 설비는 분담분)."""
    capacity_lots_per_day: float

    def utilization_at(self, lots_per_day: float) -> float:
        if self.capacity_lots_per_day <= 0:
            return float("inf")
        return lots_per_day / self.capacity_lots_per_day


@dataclass(frozen=True, slots=True)
class CapacityReport:
    groups: tuple[GroupLoad, ...]
    """능력 오름차순 (병목이 먼저)."""
    bottleneck: GroupLoad
    capacity_lots_per_day: float
    capacity_wafers_per_day: float
    raw_process_hours: float
    """순수 처리시간 합. X-factor의 분모."""

    def by_gid(self, gid: str) -> GroupLoad:
        for g in self.groups:
            if g.gid == gid:
                return g
        raise KeyError(gid)

    def near_bottleneck(self, within: float = 0.20) -> tuple[GroupLoad, ...]:
        """병목 능력의 (1+within)배 이내에 있는 그룹들 = 실질적 근접 병목."""
        limit = self.capacity_lots_per_day * (1.0 + within)
        return tuple(g for g in self.groups if g.capacity_lots_per_day <= limit)


def analytic_capacity(fab: Fab, counts: dict[str, int] | None = None) -> CapacityReport:
    """설비 대수 구성만으로 결정되는 생산능력 상한."""
    visits = fab.weighted_visits()
    loads: list[GroupLoad] = []
    for gid, group in fab.groups.items():
        n = group.count if counts is None else counts[gid]
        workload = visits[gid] * group.minutes_per_lot
        if workload <= 0:
            capacity = float("inf")
        elif n <= 0:
            capacity = 0.0
        else:
            capacity = n * MINUTES_PER_DAY * group.availability / workload
        loads.append(
            GroupLoad(
                gid=gid,
                name=group.name,
                tools=n,
                availability=group.availability,
                visits_per_lot=visits[gid],
                workload_min_per_lot=workload,
                capacity_lots_per_day=capacity,
            )
        )
    loads.sort(key=lambda g: g.capacity_lots_per_day)
    bottleneck = loads[0]
    return CapacityReport(
        groups=tuple(loads),
        bottleneck=bottleneck,
        capacity_lots_per_day=bottleneck.capacity_lots_per_day,
        capacity_wafers_per_day=bottleneck.capacity_lots_per_day * fab.wafers_per_lot,
        raw_process_hours=fab.raw_process_hours(),
    )


# ---- 흐름-거리 및 반송 부하 ----------------------------------------------

ENTRY = "ENTRY"
"""투입/출하 지점. spine 왼쪽 끝(x=0, y=0)의 스토커에 해당한다."""


def _entry_distance(geo: LayoutGeometry, slot: SlotId) -> float:
    x, y = geo.track_point(slot)
    return abs(x) + abs(y)


def _group_pair_expectation(
    dm: DistanceMatrix,
    a_slots: tuple[SlotId, ...],
    b_slots: tuple[SlotId, ...],
) -> tuple[float, float]:
    """두 그룹 사이 (기대 이동거리, bay 교차 확률).

    한 그룹에 설비가 여러 대이므로 실제 출발/도착 설비는 디스패칭에 달려 있다.
    해석적 근사에서는 모든 (출발설비, 도착설비) 쌍의 평균을 쓴다.
    """
    if not a_slots or not b_slots:
        return (0.0, 0.0)
    dist = 0.0
    cross = 0
    for a in a_slots:
        for b in b_slots:
            dist += dm.get(a, b)
            if a.bay != b.bay:
                cross += 1
    n = len(a_slots) * len(b_slots)
    return (dist / n, cross / n)


@dataclass(frozen=True, slots=True)
class FlowReport:
    """배치가 주어졌을 때의 반송 부하 분해.

    반송차 점유시간은 네 항으로 나뉜다.

        고정 상하차   moves × (load + unload + 2×handoff)   ← 배치와 무관
        주행         총거리 ÷ 속도                          ← 배치가 줄일 수 있음
        스토커       bay 경계 통과 횟수 × stocker_time      ← 배치가 줄일 수 있음 (지배항)
        빈차 회송     추정                                   ← 배치가 줄일 수 있음

    스토커 항이 지배적이므로, 최적화의 실질적 레버는 **거리보다 bay 교차 횟수**다.
    M5의 대리지표는 두 항을 모두 담아야 한다.
    """

    distance_m_per_lot: float
    """lot 1개가 유발하는 총 반송 주행거리."""
    moves_per_lot: float
    """반송 횟수 = 스텝 수 + 1 (투입 → 첫 설비, 마지막 설비 → 출하 포함)."""
    interbay_moves_per_lot: float
    """bay를 넘는 이동 횟수 (투입/출하 이동은 제외)."""
    stocker_ops_per_lot: float
    """스토커 입출고 조작 횟수 = 통과하는 bay 경계 수."""
    mean_move_m: float
    handling_seconds_per_lot: float
    travel_seconds_per_lot: float
    stocker_seconds_per_lot: float
    empty_seconds_per_lot: float
    """빈차 회송 추정 시간. 회송 거리를 평균 이동거리의 `empty_ratio`배로 근사한다."""

    @property
    def loaded_seconds_per_lot(self) -> float:
        return (
            self.handling_seconds_per_lot
            + self.travel_seconds_per_lot
            + self.stocker_seconds_per_lot
        )

    @property
    def vehicle_seconds_per_lot(self) -> float:
        return self.loaded_seconds_per_lot + self.empty_seconds_per_lot

    @property
    def interbay_fraction(self) -> float:
        return self.interbay_moves_per_lot / self.moves_per_lot if self.moves_per_lot else 0.0

    @property
    def layout_controllable_fraction(self) -> float:
        """반송 부하 중 배치가 바꿀 수 있는 비율 (고정 상하차를 제외한 부분)."""
        total = self.vehicle_seconds_per_lot
        return (total - self.handling_seconds_per_lot) / total if total else 0.0

    def required_vehicles(self, lots_per_day: float) -> float:
        return lots_per_day * self.vehicle_seconds_per_lot / SECONDS_PER_DAY

    def vehicle_utilization(self, lots_per_day: float, vehicles: int) -> float:
        if vehicles <= 0:
            return float("inf")
        return self.required_vehicles(lots_per_day) / vehicles


def flow_report(
    fab: Fab,
    geo: LayoutGeometry,
    assignment: Assignment,
    dm: DistanceMatrix | None = None,
    empty_ratio: float = 0.6,
) -> FlowReport:
    """배치가 주어졌을 때의 흐름-거리·bay 교차·반송차 부하.

    `empty_ratio`는 빈차 회송 거리를 적차 이동거리의 몇 배로 볼지에 대한 계수다.
    반송차가 다음 요청지로 이동하는 거리는 요청 분포에 달려 있어 해석적으로 정확히
    구할 수 없다. 0.6은 "직전 하차 지점 근처에서 다음 요청이 발생하는 경향"을 반영한
    보수적 근사이며, DES 실측으로 교정할 값이다 (M4).
    """
    dm = dm or DistanceMatrix.build(geo)
    slots_by_group = {gid: assignment.group_slots(gid) for gid in fab.groups}

    total_distance = 0.0
    total_moves = 0.0
    total_interbay = 0.0
    total_stocker_ops = 0.0

    for product in fab.products.values():
        w = product.mix
        first_slots = slots_by_group[product.steps[0].group]
        last_slots = slots_by_group[product.steps[-1].group]

        # 투입 스토커 → 첫 설비: bay 경계 1회 통과
        total_distance += w * _mean(_entry_distance(geo, s) for s in first_slots)
        total_stocker_ops += w * 1.0
        # 마지막 설비 → 출하 스토커: bay 경계 1회 통과
        total_distance += w * _mean(_entry_distance(geo, s) for s in last_slots)
        total_stocker_ops += w * 1.0

        for cur, nxt in zip(product.steps, product.steps[1:]):
            dist, cross = _group_pair_expectation(
                dm, slots_by_group[cur.group], slots_by_group[nxt.group]
            )
            total_distance += w * dist
            total_interbay += w * cross
            total_stocker_ops += w * cross * 2.0  # 출발 bay 출고 + 도착 bay 입고

        total_moves += w * (product.step_count + 1)

    mean_move = total_distance / total_moves if total_moves else 0.0
    t = fab.transport
    return FlowReport(
        distance_m_per_lot=total_distance,
        moves_per_lot=total_moves,
        interbay_moves_per_lot=total_interbay,
        stocker_ops_per_lot=total_stocker_ops,
        mean_move_m=mean_move,
        handling_seconds_per_lot=total_moves * t.fixed_handling_s,
        travel_seconds_per_lot=total_distance / t.speed_mps,
        stocker_seconds_per_lot=total_stocker_ops * t.stocker_time_s,
        empty_seconds_per_lot=total_moves * mean_move * empty_ratio / t.speed_mps,
    )


def _mean(values) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0
