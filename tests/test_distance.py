"""거리 함수와 거리 행렬 검증.

거리 공리(비음수·대칭·삼각부등식)를 30×30 전수로 확인한다. 최적화가 이 행렬을
수백만 번 조회하므로, 여기서 틀리면 이후 모든 결과가 무의미해진다.
"""

from __future__ import annotations

import pytest

from fablayout.core.distance import DistanceMatrix, slot_distance_m
from fablayout.core.geometry import LayoutGeometry, SlotId, default_geometry


@pytest.fixture
def geo() -> LayoutGeometry:
    return default_geometry()


@pytest.fixture
def dm(geo: LayoutGeometry) -> DistanceMatrix:
    return DistanceMatrix.build(geo)


# ---- 거리 공리 ----------------------------------------------------------


def test_self_distance_zero(dm: DistanceMatrix) -> None:
    for s in dm.slots:
        assert dm.get(s, s) == 0.0


def test_nonnegative_and_symmetric(dm: DistanceMatrix) -> None:
    n = len(dm)
    for i in range(n):
        for j in range(n):
            d = dm.by_index(i, j)
            assert d >= 0.0
            assert d == pytest.approx(dm.by_index(j, i))


def test_triangle_inequality(dm: DistanceMatrix) -> None:
    n = len(dm)
    for i in range(n):
        for j in range(n):
            for k in range(n):
                assert dm.by_index(i, j) <= dm.by_index(i, k) + dm.by_index(k, j) + 1e-9


def test_lr_pairs_are_distance_identical(dm: DistanceMatrix, geo: LayoutGeometry) -> None:
    """L/R은 같은 궤도 지점을 쓰므로 거리상 구별되지 않는다 (의도된 단순화)."""
    a = SlotId(2, "L", 1)
    a_twin = SlotId(2, "R", 1)
    for s in geo.slots:
        assert dm.get(a, s) == pytest.approx(dm.get(a_twin, s))


# ---- 손계산 대조 --------------------------------------------------------
# 기본 기하: slot_pitch=4.5, spine_width=4.0, bay_width=11.0, bay_gap=3.0
#   bay_center_x: col0=5.5, col1=19.5, col2=33.5
#   |y| = 2.0 + (pos+0.5)*4.5  →  pos0=4.25, pos1=8.75, pos2=13.25


def test_same_bay_distance(geo: LayoutGeometry) -> None:
    """같은 bay: spine에 나가지 않고 bay 내부 궤도만 쓴다 → |y 차이|."""
    d = slot_distance_m(geo, SlotId(1, "L", 0), SlotId(1, "R", 2))
    assert d == pytest.approx(13.25 - 4.25)  # 9.0 = 2 피치


def test_same_bay_same_position_is_zero(geo: LayoutGeometry) -> None:
    d = slot_distance_m(geo, SlotId(1, "L", 1), SlotId(1, "R", 1))
    assert d == 0.0


def test_adjacent_bay_same_side(geo: LayoutGeometry) -> None:
    """bay1(col0,N) pos0 → bay2(col1,N) pos0: 4.25 + 14.0 + 4.25"""
    d = slot_distance_m(geo, SlotId(1, "L", 0), SlotId(2, "L", 0))
    assert d == pytest.approx(4.25 + 14.0 + 4.25)


def test_across_spine_same_column(geo: LayoutGeometry) -> None:
    """bay1(col0,N) ↔ bay4(col0,S): x 차이 0, 두 bay의 spine 진출거리 합만 남는다."""
    d = slot_distance_m(geo, SlotId(1, "L", 0), SlotId(4, "L", 0))
    assert d == pytest.approx(4.25 + 0.0 + 4.25)
    d2 = slot_distance_m(geo, SlotId(1, "L", 2), SlotId(4, "R", 2))
    assert d2 == pytest.approx(13.25 + 0.0 + 13.25)


def test_farthest_pair(dm: DistanceMatrix, geo: LayoutGeometry) -> None:
    """가장 먼 쌍: 양 끝 column(0 ↔ 2)의 최심부 = 13.25 + 28.0 + 13.25.

    col0에는 bay 1(N)과 bay 4(S)가, col2에는 bay 3(N)이 있으므로 동률 쌍이 여럿이다.
    bay 번호가 아니라 column으로 단정한다.
    """
    a, b, d = dm.farthest_pair()
    assert d == pytest.approx(13.25 + 28.0 + 13.25)
    assert {geo.bay_spec(a.bay).column, geo.bay_spec(b.bay).column} == {0, 2}
    assert a.pos == b.pos == geo.positions_per_side - 1
    assert dm.max_m == pytest.approx(d)


def test_same_bay_beats_cross_bay(geo: LayoutGeometry) -> None:
    """같은 bay 안이 다른 bay보다 항상 가깝다 — 그룹을 뭉치는 것의 근거."""
    within = slot_distance_m(geo, SlotId(2, "L", 0), SlotId(2, "L", 2))
    across = slot_distance_m(geo, SlotId(2, "L", 0), SlotId(1, "L", 2))
    assert within < across


# ---- 행렬 요약 ----------------------------------------------------------


def test_matrix_dimensions(dm: DistanceMatrix, geo: LayoutGeometry) -> None:
    assert len(dm) == geo.slot_count == 30
    assert all(len(row) == 30 for row in dm.matrix)


def test_mean_offdiag_positive(dm: DistanceMatrix) -> None:
    assert 0.0 < dm.mean_offdiag_m < dm.max_m


def test_matrix_matches_function(dm: DistanceMatrix, geo: LayoutGeometry) -> None:
    for a in geo.slots:
        for b in geo.slots:
            assert dm.get(a, b) == pytest.approx(slot_distance_m(geo, a, b))
