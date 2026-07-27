"""기준선 배치 생성 검증."""

from __future__ import annotations

import pytest

from fablayout.core.geometry import default_geometry
from fablayout.core.model import Assignment, Fab
from fablayout.data.builtin import build_smallfab21
from fablayout.opt import baseline


@pytest.fixture
def fab() -> Fab:
    return build_smallfab21()


@pytest.mark.parametrize("kind", ["functional", "spread", "random"])
def test_all_tools_placed_without_overlap(fab: Fab, kind: str) -> None:
    geo = default_geometry()
    asg = baseline.build(kind, fab, geo)
    assert len(asg.placement) == fab.total_tools == 21
    assert len(asg.occupied()) == 21  # Assignment.__post_init__ 가 중복을 막는다
    assert set(asg.tools) == set(fab.tool_instances())
    assert asg.occupied() <= set(geo.slots)


def test_functional_groups_stay_together(fab: Fab) -> None:
    """기능별 배치의 정의: 한 그룹의 설비는 같은 bay에 모인다."""
    geo = default_geometry()
    asg = baseline.functional_layout(fab, geo)
    for gid, n in fab.tool_counts.items():
        if n == 0:
            continue
        bays = asg.bay_of_group(gid)
        assert len(bays) == 1, f"{gid}가 bay {sorted(bays)}로 흩어졌다"


def test_functional_puts_frequent_groups_central(fab: Fab) -> None:
    """방문이 잦은 그룹이 spine 중앙 bay에 놓여야 한다."""
    geo = default_geometry()
    asg = baseline.functional_layout(fab, geo)
    visits = fab.weighted_visits()
    mid = geo.spine_length_m / 2

    busiest = max(visits, key=lambda g: visits[g])          # ETCH_DRY
    quietest = min((g for g in visits if fab.tool_counts[g]), key=lambda g: visits[g])

    def offset(gid: str) -> float:
        bay = next(iter(asg.bay_of_group(gid)))
        return abs(geo.bay_center_x(bay) - mid)

    assert offset(busiest) <= offset(quietest)


def test_functional_fills_from_spine_side(fab: Fab) -> None:
    """bay 안에서는 spine에 가까운 pos부터 채운다 (빈자리가 최심부에 남아야 한다)."""
    geo = default_geometry()
    asg = baseline.functional_layout(fab, geo)
    by_bay: dict[int, list[int]] = {}
    for slot in asg.occupied():
        by_bay.setdefault(slot.bay, []).append(slot.pos)
    for bay, positions in by_bay.items():
        n = len(positions)
        # n개를 pos 0부터 채우면 pos 합은 2*(0+0+1+1+...) 형태가 된다
        expected = sorted((i // 2) for i in range(n))
        assert sorted(positions) == expected, f"bay {bay}에 구멍이 있다"


def test_spread_scatters_groups(fab: Fab) -> None:
    """대안 기준선: 큰 그룹이 여러 bay로 흩어져야 한다."""
    geo = default_geometry()
    asg = baseline.spread_layout(fab, geo)
    photo_bays = asg.bay_of_group("PHOTO")  # 3대
    assert len(photo_bays) > 1


def test_random_is_deterministic_per_seed(fab: Fab) -> None:
    geo = default_geometry()
    a = baseline.random_layout(fab, geo, seed=42)
    b = baseline.random_layout(fab, geo, seed=42)
    c = baseline.random_layout(fab, geo, seed=43)
    assert a.placement == b.placement
    assert a.placement != c.placement


def test_counts_override_changes_tool_set(fab: Fab) -> None:
    geo = default_geometry()
    counts = dict(fab.tool_counts)
    counts["PVD"] = 3
    asg = baseline.functional_layout(fab, geo, counts)
    assert len(asg.placement) == 23
    assert len(asg.group_slots("PVD")) == 3


def test_slot_shortage_raises(fab: Fab) -> None:
    geo = default_geometry()
    counts = {gid: 6 for gid in fab.groups}  # 11 그룹 × 6 = 66 > 30 slot
    with pytest.raises(ValueError, match="slot 부족"):
        baseline.functional_layout(fab, geo, counts)


def test_assignment_rejects_duplicate_slot() -> None:
    geo = default_geometry()
    slot = geo.slots[0]
    with pytest.raises(ValueError, match="한 slot에 설비가 둘 이상"):
        Assignment(placement={"A#1": slot, "B#1": slot})


def test_unknown_baseline_kind(fab: Fab) -> None:
    with pytest.raises(ValueError, match="알 수 없는 기준선"):
        baseline.build("nope", fab, default_geometry())  # type: ignore[arg-type]


def test_bay_fill_order_prefers_paired_column(fab: Fab) -> None:
    """같은 column의 북/남 bay를 먼저 채워야 한다 (spine 이동거리 0인 최근접 쌍).

    기본 기하에서 bay 1(col0)·bay 4(col0)·bay 3(col2)이 중앙에서 모두 14m로 동률이다.
    bay 번호순으로 동률을 깨면 bay 1 다음에 28m 떨어진 bay 3을 골라 bay 4를 비워 두는,
    불필요하게 나쁜 기준선이 나온다.
    """
    geo = default_geometry()
    order = baseline._bay_centrality_order(geo)
    assert order[:2] == [2, 5], f"중앙 column(bay 2·5)부터 채워야 한다: {order}"
    assert order.index(4) < order.index(3), (
        f"bay 1과 같은 column인 bay 4가 먼 bay 3보다 앞서야 한다: {order}"
    )


def test_functional_layout_leaves_farthest_bay_empty(fab: Fab) -> None:
    """여유 slot은 spine에서 가장 먼 bay에 남아야 한다."""
    geo = default_geometry()
    asg = baseline.functional_layout(fab, geo)
    used = {s.bay for s in asg.occupied()}
    farthest = baseline._bay_centrality_order(geo)[-1]
    assert farthest not in used, f"가장 먼 bay {farthest}가 채워졌다 (사용: {sorted(used)})"


def test_functional_layout_is_compact(fab: Fab) -> None:
    """기준선의 bay 교차 부하가 대안 채움 순서보다 나쁘지 않아야 한다."""
    from fablayout.core.analysis import flow_report

    geo = default_geometry()
    func = flow_report(fab, geo, baseline.functional_layout(fab, geo))
    spread = flow_report(fab, geo, baseline.spread_layout(fab, geo))
    assert func.interbay_moves_per_lot < spread.interbay_moves_per_lot
