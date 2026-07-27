"""기하 모델 검증."""

from __future__ import annotations

import pytest

from fablayout.core.geometry import BaySpec, LayoutGeometry, SlotId, default_geometry


@pytest.fixture
def geo() -> LayoutGeometry:
    return default_geometry()


def test_default_geometry_shape(geo: LayoutGeometry) -> None:
    assert len(geo.bays) == 5
    assert geo.positions_per_side == 3
    # bay당 2벽 × 3위치 = 6 slot, 5 bay → 30 slot
    assert geo.slot_count == 30
    assert geo.bay_width_m == pytest.approx(11.0)  # 4.0*2 + 3.0
    assert geo.bay_length_m == pytest.approx(13.5)  # 3 * 4.5


def test_slot_enumeration_order(geo: LayoutGeometry) -> None:
    """bay → pos → side 순서. pos 0(spine 최근접)이 먼저 나와야 한다."""
    first_six = geo.slots[:6]
    assert [(s.bay, s.pos, s.side) for s in first_six] == [
        (1, 0, "L"), (1, 0, "R"),
        (1, 1, "L"), (1, 1, "R"),
        (1, 2, "L"), (1, 2, "R"),
    ]
    assert len(set(geo.slots)) == geo.slot_count


def test_pos0_is_nearest_to_spine(geo: LayoutGeometry) -> None:
    for bay in (1, 3, 4):
        reaches = [
            geo.spine_reach_m(SlotId(bay, "L", p)) for p in range(geo.positions_per_side)
        ]
        assert reaches == sorted(reaches)
        assert reaches[0] == pytest.approx(2.0 + 0.5 * 4.5)  # spine_width/2 + 반피치


def test_bays_are_perpendicular_to_spine(geo: LayoutGeometry) -> None:
    """같은 bay 안에서 pos가 달라지면 y만 변하고 x는 변하지 않아야 한다."""
    a = geo.track_point(SlotId(2, "L", 0))
    b = geo.track_point(SlotId(2, "L", 2))
    assert a[0] == pytest.approx(b[0])
    assert a[1] != pytest.approx(b[1])


def test_north_south_sign(geo: LayoutGeometry) -> None:
    north = geo.track_point(SlotId(1, "L", 0))  # bay 1 = 북측
    south = geo.track_point(SlotId(4, "L", 0))  # bay 4 = 남측
    assert north[1] > 0
    assert south[1] < 0
    assert north[1] == pytest.approx(-south[1])
    # 같은 column이므로 x는 동일
    assert north[0] == pytest.approx(south[0])


def test_sides_share_track_centerline(geo: LayoutGeometry) -> None:
    """L/R은 같은 궤도 중심선을 쓴다 (측면 오프셋은 호이스트가 처리)."""
    left = geo.track_point(SlotId(2, "L", 1))
    right = geo.track_point(SlotId(2, "R", 1))
    assert left == right
    # 설비 몸체는 통로 양쪽으로 갈린다
    lx = geo.tool_center(SlotId(2, "L", 1))[0]
    rx = geo.tool_center(SlotId(2, "R", 1))[0]
    assert lx < left[0] < rx
    assert rx - lx == pytest.approx(3.0 + 4.0)  # aisle + tool_depth


def test_portal_on_spine_edge(geo: LayoutGeometry) -> None:
    for bay in (1, 2, 3):
        assert geo.portal(bay)[1] == pytest.approx(2.0)
    for bay in (4, 5):
        assert geo.portal(bay)[1] == pytest.approx(-2.0)
    # portal x = bay 궤도 중심선
    assert geo.portal(3)[0] == pytest.approx(geo.bay_center_x(3))


def test_bay_column_spacing(geo: LayoutGeometry) -> None:
    step = geo.bay_center_x(2) - geo.bay_center_x(1)
    assert step == pytest.approx(11.0 + 3.0)  # bay_width + bay_gap
    assert geo.bay_center_x(3) - geo.bay_center_x(2) == pytest.approx(step)
    # 같은 column의 북/남 bay는 x가 같다
    assert geo.bay_center_x(1) == pytest.approx(geo.bay_center_x(4))


def test_extent_and_spine_length(geo: LayoutGeometry) -> None:
    x0, y0, x1, y1 = geo.extent()
    assert x0 == 0.0
    assert x1 == pytest.approx(3 * 14.0 - 3.0)  # 3열 − 마지막 gap
    assert y1 == pytest.approx(2.0 + 13.5)
    assert y0 == pytest.approx(-(2.0 + 13.5))
    assert geo.spine_length_m == pytest.approx(39.0)


def test_rejects_duplicate_bay_id() -> None:
    with pytest.raises(ValueError, match="bay 번호가 중복"):
        LayoutGeometry(bays=(BaySpec(1, 0, "N"), BaySpec(1, 1, "N")))


def test_rejects_overlapping_position() -> None:
    with pytest.raises(ValueError, match="같은 \\(column, bay_side\\)"):
        LayoutGeometry(bays=(BaySpec(1, 0, "N"), BaySpec(2, 0, "N")))


def test_rejects_zero_positions() -> None:
    with pytest.raises(ValueError, match="positions_per_side"):
        LayoutGeometry(bays=(BaySpec(1, 0, "N"),), positions_per_side=0)
