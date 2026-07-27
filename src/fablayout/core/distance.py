"""slot 간 반송 거리 및 거리 행렬.

반송차는 bay 중앙 궤도와 spine 궤도만 달린다. 따라서 두 slot 사이 거리는:

    같은 bay      : |y_a − y_b|                        (bay 내부 궤도만 사용)
    다른 bay      : |y_a| + |x_a − x_b| + |y_b|        (spine 중심선 경유)

여기서 y는 spine 중심선(y=0) 기준 좌표이므로 |y|가 곧 "spine까지 나오는 거리"다.
같은 column의 북측/남측 bay 사이 이동은 |x_a − x_b| = 0 이 되어 두 bay의
spine 진출 거리 합만 남는다 — spine을 가로지르는 것이 올바르게 표현된다.

이 거리 함수는 거리 공리(비음수·대칭·삼각부등식)를 만족한다. `tests/test_distance.py`가
전수 검증한다.

**레이아웃 기하가 결정 변수가 아니므로 거리 행렬은 최적화 전체에서 불변이다.**
배치를 바꾸는 것은 "어느 설비가 어느 slot에 있는지"를 바꾸는 것일 뿐이다. 대리지표
평가가 빠른 근본 이유가 이것이다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import LayoutGeometry, SlotId


def slot_distance_m(geo: LayoutGeometry, a: SlotId, b: SlotId) -> float:
    """두 slot 사이 반송 주행 거리 (m)."""
    if a.bay == b.bay:
        return abs(geo.track_point(a)[1] - geo.track_point(b)[1])
    ax, ay = geo.track_point(a)
    bx, by = geo.track_point(b)
    return abs(ay) + abs(ax - bx) + abs(by)


@dataclass(frozen=True, slots=True)
class DistanceMatrix:
    """slot 간 거리 행렬. 기하가 정해지면 한 번 계산해 재사용한다."""

    slots: tuple[SlotId, ...]
    index: dict[SlotId, int]
    matrix: tuple[tuple[float, ...], ...]

    @classmethod
    def build(cls, geo: LayoutGeometry) -> DistanceMatrix:
        slots = geo.slots
        index = {s: i for i, s in enumerate(slots)}
        rows = tuple(
            tuple(slot_distance_m(geo, a, b) for b in slots) for a in slots
        )
        return cls(slots=slots, index=index, matrix=rows)

    def __len__(self) -> int:
        return len(self.slots)

    def get(self, a: SlotId, b: SlotId) -> float:
        return self.matrix[self.index[a]][self.index[b]]

    def by_index(self, i: int, j: int) -> float:
        return self.matrix[i][j]

    # ---- 요약 통계 (데이터셋·레이아웃 점검용) ----------------------------

    @property
    def max_m(self) -> float:
        return max(max(row) for row in self.matrix)

    @property
    def mean_offdiag_m(self) -> float:
        n = len(self.slots)
        if n < 2:
            return 0.0
        total = sum(sum(row) for row in self.matrix)
        return total / (n * (n - 1))

    def farthest_pair(self) -> tuple[SlotId, SlotId, float]:
        best = (self.slots[0], self.slots[0], -1.0)
        for i, a in enumerate(self.slots):
            for j in range(i + 1, len(self.slots)):
                d = self.matrix[i][j]
                if d > best[2]:
                    best = (a, self.slots[j], d)
        return best
