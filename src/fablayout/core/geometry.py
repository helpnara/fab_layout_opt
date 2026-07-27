"""Fab 클린룸 기하 모델.

Bay 구조 레이아웃을 좌표계로 표현한다. 실제 spine/bay fab과 같이 bay는 중앙 간선
통로(spine)에서 **수직으로** 뻗어 나가며, bay 중앙 통로 양벽에 설비를 붙인다.

        BAY 1        BAY 2        BAY 3          ← 북측 bay (spine에서 +y 방향)
      ┌───┬───┐    ┌───┬───┐    ┌───┬───┐
   p2 │ L │ R │    │ L │ R │    │ L │ R │       p = spine에서 먼 쪽으로 증가
   p1 │ L │ R │    │ L │ R │    │ L │ R │
   p0 │ L │ R │    │ L │ R │    │ L │ R │       p0 = spine에 가장 가까움
      └──╥╥───┘    └──╥╥───┘    └──╥╥───┘
  ════════╬╬══════════╬╬══════════╬╬═════════   ← SPINE (y = 0)
      ┌──╢╢───┐    ┌──╢╢───┐
   p0 │ L │ R │    │ L │ R │                    ← 남측 bay (-y 방향)
   p1 │ L │ R │    │ L │ R │
   p2 │ L │ R │    │ L │ R │
      └───┴───┘    └───┴───┘
        BAY 4        BAY 5

좌표계
    x: spine을 따르는 방향 (좌 → 우)
    y: spine 중심선을 0으로, 북측 +, 남측 −

반송 거리 모델 — 궤도 중심선 기준
    반송차(OHT)는 궤도 위를 달린다. 궤도는 (1) bay 중앙 통로 위, (2) spine 위에
    깔려 있다. 설비는 통로 양벽에 있으므로 궤도 중심선에서 측면으로 조금 떨어져
    있지만, 그 측면 오프셋은 **주행이 아니라 호이스트 승강**으로 처리된다. 따라서
    거리 계산에는 포함하지 않고 load/unload/handoff 시간에 흡수시킨다.

    결과적으로 같은 bay·같은 위치의 L/R 두 slot은 **주행 거리상 동일**하다.
    이는 의도된 단순화이며, 양벽 배치의 실질적 이점(같은 설비 대수를 절반 길이의
    bay에 넣어 전체 이동거리를 줄이는 것)은 그대로 반영된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Literal

Side = Literal["L", "R"]
BaySide = Literal["N", "S"]


@dataclass(frozen=True, slots=True, order=True)
class SlotId:
    """설비 1대가 들어가는 자리."""

    bay: int
    """1-based bay 번호."""
    side: Side
    """bay 중앙 통로 기준 좌(L) / 우(R)."""
    pos: int
    """0-based. 0이 spine에 가장 가깝다."""

    def __str__(self) -> str:
        return f"B{self.bay}{self.side}{self.pos}"


@dataclass(frozen=True, slots=True)
class BaySpec:
    """bay 하나의 배치 정보."""

    bay: int
    column: int
    """spine을 따라 왼쪽부터 0-based 열 번호. 북/남 bay가 같은 column을 공유할 수 있다."""
    bay_side: BaySide
    """spine 기준 북(N) / 남(S)."""
    name: str = ""


@dataclass(frozen=True, slots=True)
class LayoutGeometry:
    """클린룸 기하. 최적화 과정에서 불변이다 (결정 변수가 아님)."""

    bays: tuple[BaySpec, ...]
    positions_per_side: int = 3
    slot_pitch_m: float = 4.5
    """bay 길이 방향(y) 설비 간격."""
    tool_depth_m: float = 4.0
    """설비의 x 방향 크기."""
    aisle_width_m: float = 3.0
    """bay 중앙 통로 폭."""
    bay_gap_m: float = 3.0
    """인접 bay 사이 간격."""
    spine_width_m: float = 4.0

    _slots: tuple[SlotId, ...] = field(default_factory=tuple, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.positions_per_side < 1:
            raise ValueError("positions_per_side >= 1 이어야 한다")
        if len({b.bay for b in self.bays}) != len(self.bays):
            raise ValueError("bay 번호가 중복되었다")
        if len({(b.column, b.bay_side) for b in self.bays}) != len(self.bays):
            raise ValueError("같은 (column, bay_side)에 bay가 둘 이상 있다")
        object.__setattr__(self, "_slots", tuple(self._enumerate_slots()))

    def _enumerate_slots(self) -> Iterator[SlotId]:
        for b in sorted(self.bays, key=lambda s: s.bay):
            for pos in range(self.positions_per_side):
                for side in ("L", "R"):
                    yield SlotId(b.bay, side, pos)  # type: ignore[arg-type]

    # ---- 기본 속성 -------------------------------------------------------

    @property
    def slots(self) -> tuple[SlotId, ...]:
        """모든 slot. bay → pos → side 순으로 정렬되어 있다."""
        return self._slots

    @property
    def slot_count(self) -> int:
        return len(self._slots)

    @property
    def bay_width_m(self) -> float:
        """bay 하나의 x 방향 폭 (설비 2열 + 중앙 통로)."""
        return 2 * self.tool_depth_m + self.aisle_width_m

    @property
    def bay_length_m(self) -> float:
        """bay 하나의 y 방향 길이."""
        return self.positions_per_side * self.slot_pitch_m

    def bay_spec(self, bay: int) -> BaySpec:
        for b in self.bays:
            if b.bay == bay:
                return b
        raise KeyError(f"bay {bay} 없음")

    # ---- 좌표 -----------------------------------------------------------

    def bay_center_x(self, bay: int) -> float:
        """bay 중앙 통로(= 궤도 중심선)의 x 좌표."""
        col = self.bay_spec(bay).column
        return col * (self.bay_width_m + self.bay_gap_m) + self.bay_width_m / 2

    def portal(self, bay: int) -> tuple[float, float]:
        """bay 입구 — bay 궤도와 spine 궤도의 접속점."""
        sign = 1.0 if self.bay_spec(bay).bay_side == "N" else -1.0
        return (self.bay_center_x(bay), sign * self.spine_width_m / 2)

    def track_point(self, slot: SlotId) -> tuple[float, float]:
        """slot의 반송 궤도상 정차 지점. 거리 계산의 기준점이다."""
        sign = 1.0 if self.bay_spec(slot.bay).bay_side == "N" else -1.0
        y = sign * (self.spine_width_m / 2 + (slot.pos + 0.5) * self.slot_pitch_m)
        return (self.bay_center_x(slot.bay), y)

    def tool_center(self, slot: SlotId) -> tuple[float, float]:
        """설비 몸체의 중심 좌표. 도면 렌더링용 (거리 계산에는 쓰지 않는다)."""
        tx, ty = self.track_point(slot)
        offset = self.aisle_width_m / 2 + self.tool_depth_m / 2
        return (tx - offset if slot.side == "L" else tx + offset, ty)

    def spine_reach_m(self, slot: SlotId) -> float:
        """slot 정차 지점에서 spine 중심선까지의 거리."""
        return abs(self.track_point(slot)[1])

    # ---- 도면 경계 -------------------------------------------------------

    def extent(self) -> tuple[float, float, float, float]:
        """(x_min, y_min, x_max, y_max) — 클린룸 외곽."""
        n_cols = max(b.column for b in self.bays) + 1
        x_max = n_cols * (self.bay_width_m + self.bay_gap_m) - self.bay_gap_m
        has_n = any(b.bay_side == "N" for b in self.bays)
        has_s = any(b.bay_side == "S" for b in self.bays)
        top = self.spine_width_m / 2 + self.bay_length_m if has_n else self.spine_width_m / 2
        bottom = -(self.spine_width_m / 2 + self.bay_length_m) if has_s else -self.spine_width_m / 2
        return (0.0, bottom, x_max, top)

    @property
    def spine_length_m(self) -> float:
        _, _, x_max, _ = self.extent()
        return x_max


def default_geometry() -> LayoutGeometry:
    """내장 기본 레이아웃 `layout-5bay` — slot 30개.

    bay 5개 (북측 3 + 남측 2), bay당 2벽 × 3위치 = 6 slot.
    남서측 한 자리는 bay를 두지 않는다 — 실제 fab에서 유틸리티(용수·가스·배기) 구역이
    차지하는 공간에 해당한다.

    설비 21대 기준 채움률 70%로, 배치 교환과 소규모 증설이 모두 가능한 여유를 남긴다.
    """
    return LayoutGeometry(
        bays=(
            BaySpec(1, 0, "N", "북1"),
            BaySpec(2, 1, "N", "북2"),
            BaySpec(3, 2, "N", "북3"),
            BaySpec(4, 0, "S", "남1"),
            BaySpec(5, 1, "S", "남2"),
        ),
        positions_per_side=3,
    )
