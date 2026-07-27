"""배치 대리지표 — DES 없이 계산하는 사이클타임 근사.

**왜 필요한가.** M4에서 잰 DES 평가 비용은 후보 1개에 `midfab` 기준 약 20초이고, 그마저
쌍대 신뢰구간이 ±5~15시간을 남긴다. 찾는 개선이 그와 비슷한 규모다. DES를 직접 수천 번
돌리는 최적화는 시간으로도, 정확도로도 성립하지 않는다. 대리지표는 **결정론적이라
잡음이 0**이고 한 번 계산에 밀리초가 든다.

**세 항으로 구성한다.** M1~M4에서 실측으로 확인한 세 가지 힘이다.

    1. 스토커  bay 경계를 넘을 때마다 항방향 120초. 반송 부하의 지배항이다 (M1).
    2. 주행    거리 ÷ 속도. 스토커에 비하면 작지만 배치가 바꿀 수 있는 부분이다.
    3. 풀 분할 한 그룹을 k개 bay로 쪼개면 서버 풀이 k등분되어 대기가 늘어난다 (M3).
               M/M/c에서 대기시간은 대략 1/c에 비례하므로, k개로 쪼개면 대기 항이
               약 k배가 된다. `midfab` 실측에서 이 항이 스토커 항을 압도했다 —
               bay 교차를 85%→80%로 줄인 배치가 사이클타임은 8.7% 나빴다.

세 항의 가중치는 임의로 정할 수 없다. `scripts/calibrate_surrogate.py`가 무작위 배치
표본에 대해 DES 실측과의 순위상관을 최대화하도록 맞춘다. 상관이 충분히 높지 않으면
2단 최적화 구조 자체가 성립하지 않으므로, 그 검증이 M5의 게이트다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.analysis import ENTRY as _ENTRY_MARK  # noqa: F401  (문서적 참조)
from ..core.distance import DistanceMatrix
from ..core.geometry import LayoutGeometry
from ..core.model import Assignment, Fab


@dataclass(frozen=True, slots=True)
class SurrogateTerms:
    """대리지표의 항별 값. 어느 항이 점수를 지배하는지 진단할 수 있다."""

    stocker_minutes: float
    """bay 경계 통과로 생기는 스토커 시간 (lot당)."""
    travel_minutes: float
    """주행 시간 (lot당)."""
    split_penalty: float
    """서버 풀 분할로 늘어나는 대기 (lot당, 분 단위 근사)."""
    interbay_moves: float
    split_groups: int

    def score(self, w_stocker: float, w_travel: float, w_split: float) -> float:
        return (
            w_stocker * self.stocker_minutes
            + w_travel * self.travel_minutes
            + w_split * self.split_penalty
        )


@dataclass(frozen=True)
class Surrogate:
    """배치를 점수로 바꾼다. 낮을수록 좋다."""

    fab: Fab
    geo: LayoutGeometry
    dm: DistanceMatrix
    w_stocker: float = 1.0
    w_travel: float = 1.0
    w_split: float = 1.0

    _pair_flows: dict[tuple[str, str], float] = field(default_factory=dict, repr=False)
    _entry_flows: dict[str, float] = field(default_factory=dict, repr=False)
    _visits: dict[str, float] = field(default_factory=dict, repr=False)

    @classmethod
    def build(cls, fab: Fab, geo: LayoutGeometry, **weights) -> Surrogate:
        """연속 스텝 쌍의 빈도를 미리 집계한다.

        스텝 쌍을 매번 훑으면(제품 3종 × 116스텝) 초당 250회밖에 못 낸다. 그룹 쌍
        빈도로 접으면 항목이 수십 개로 줄어 탐색에 쓸 수 있는 속도가 된다.
        """
        from .construct import pair_flows

        entry: dict[str, float] = {}
        for p in fab.products.values():
            for gid in (p.steps[0].group, p.steps[-1].group):
                entry[gid] = entry.get(gid, 0.0) + p.mix
        return cls(
            fab=fab, geo=geo, dm=DistanceMatrix.build(geo),
            _pair_flows=pair_flows(fab), _entry_flows=entry,
            _visits=fab.weighted_visits(), **weights,
        )

    # ---- 항 계산 --------------------------------------------------------

    def terms(self, assignment: Assignment, counts: dict[str, int] | None = None) -> SurrogateTerms:
        fab, geo, dm = self.fab, self.geo, self.dm
        counts = counts or fab.tool_counts
        t = fab.transport

        slots_by_group = {gid: assignment.group_slots(gid) for gid in fab.groups}
        bays_by_group = {
            gid: {s.bay for s in slots} for gid, slots in slots_by_group.items()
        }

        # --- 1·2. 스토커와 주행: 그룹 쌍 빈도 × 그 쌍의 기대 거리·교차 확률
        interbay = 0.0
        distance_m = 0.0
        for (ga, gb), w in self._pair_flows.items():
            a, b = slots_by_group[ga], slots_by_group[gb]
            if not a or not b:
                continue
            n = len(a) * len(b)
            interbay += w * sum(1 for x in a for y in b if x.bay != y.bay) / n
            distance_m += w * sum(dm.get(x, y) for x in a for y in b) / n
        for gid, w in self._entry_flows.items():
            sl = slots_by_group[gid]
            if sl:
                distance_m += w * sum(
                    abs(geo.track_point(x)[0]) + abs(geo.track_point(x)[1]) for x in sl
                ) / len(sl)

        stocker_ops = interbay * 2.0 + 2.0 * 1.0     # 교차 2회 + 투입/출하 각 1회
        stocker_minutes = stocker_ops * t.stocker_time_s / 60.0
        travel_minutes = distance_m / t.speed_mps / 60.0

        # --- 3. 풀 분할: 그룹을 k개 bay로 쪼개면 대기 항이 대략 k배가 된다.
        # 늘어나는 몫만 세므로 (k−1)에 비례한다. 부하가 큰 그룹일수록 손해가 크다.
        split = 0.0
        n_split = 0
        visits = self._visits or fab.weighted_visits()
        for gid, group in fab.groups.items():
            if counts[gid] <= 0:
                continue
            k = len(bays_by_group[gid])
            if k <= 1:
                continue
            n_split += 1
            per_lot = group.process_minutes / group.batch_size
            split += visits[gid] * per_lot * (k - 1) / max(counts[gid], 1)

        return SurrogateTerms(
            stocker_minutes=stocker_minutes,
            travel_minutes=travel_minutes,
            split_penalty=split,
            interbay_moves=interbay,
            split_groups=n_split,
        )

    def score(self, assignment: Assignment, counts: dict[str, int] | None = None) -> float:
        return self.terms(assignment, counts).score(
            self.w_stocker, self.w_travel, self.w_split
        )


def spearman(xs: list[float], ys: list[float]) -> float:
    """순위상관. 대리지표는 값이 아니라 **순서**만 맞으면 되므로 이것으로 잰다."""
    if len(xs) != len(ys) or len(xs) < 3:
        return 0.0

    def ranks(vs: list[float]) -> list[float]:
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        out = [0.0] * len(vs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0
