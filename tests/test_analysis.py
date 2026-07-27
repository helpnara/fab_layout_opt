"""해석적 능력·흐름 계산 검증."""

from __future__ import annotations

import pytest

from fablayout.core.analysis import analytic_capacity, flow_report
from fablayout.core.distance import DistanceMatrix
from fablayout.core.geometry import default_geometry
from fablayout.core.model import Fab
from fablayout.data.builtin import build_smallfab21
from fablayout.opt import baseline


@pytest.fixture
def fab() -> Fab:
    return build_smallfab21()


def test_capacity_formula(fab: Fab) -> None:
    """PHOTO: 방문 9.1회 × 60분 = 546분/lot, 3대 × 1440분 × 0.90 ÷ 546"""
    rep = analytic_capacity(fab)
    photo = rep.by_gid("PHOTO")
    assert photo.workload_min_per_lot == pytest.approx(546.0, rel=1e-6)
    expected = 3 * 1440 * fab.groups["PHOTO"].availability / 546.0
    assert photo.capacity_lots_per_day == pytest.approx(expected)


def test_batch_group_workload_divided(fab: Fab) -> None:
    """DIFF_FURN: 방문 9.2회 × (240분 ÷ 6 lot) = 368분/lot"""
    rep = analytic_capacity(fab)
    assert rep.by_gid("DIFF_FURN").workload_min_per_lot == pytest.approx(368.0, rel=1e-6)


def test_groups_sorted_bottleneck_first(fab: Fab) -> None:
    rep = analytic_capacity(fab)
    caps = [g.capacity_lots_per_day for g in rep.groups]
    assert caps == sorted(caps)
    assert rep.bottleneck is rep.groups[0]
    assert rep.capacity_lots_per_day == pytest.approx(caps[0])


def test_near_bottleneck_structure(fab: Fab) -> None:
    """데이터셋 설계 의도: 근접 병목이 여러 개여야 최적화 여지가 있다.

    병목이 하나만 압도적이면 답이 자명해지고, 전부 여유면 배치가 무관해진다.
    """
    rep = analytic_capacity(fab)
    near = rep.near_bottleneck(within=0.20)
    assert len(near) >= 3, f"근접 병목이 {len(near)}개뿐 — 데이터셋 재조정 필요"
    # 반대로 전부가 병목이어도 안 된다 (여유 그룹이 있어야 현실적)
    assert len(near) <= len(rep.groups) - 3


def test_capacity_is_positive_and_finite(fab: Fab) -> None:
    rep = analytic_capacity(fab)
    assert 0 < rep.capacity_lots_per_day < 100
    assert rep.capacity_wafers_per_day == pytest.approx(
        rep.capacity_lots_per_day * 25
    )


def test_more_tools_never_reduce_capacity(fab: Fab) -> None:
    base = analytic_capacity(fab).capacity_lots_per_day
    counts = dict(fab.tool_counts)
    counts["PVD"] += 1
    counts["CVD"] += 1
    boosted = analytic_capacity(fab, counts).capacity_lots_per_day
    assert boosted >= base


def test_zero_tools_gives_zero_capacity(fab: Fab) -> None:
    counts = dict(fab.tool_counts)
    counts["PHOTO"] = 0
    rep = analytic_capacity(fab, counts)
    assert rep.capacity_lots_per_day == 0.0


def test_utilization_at_capacity_is_one(fab: Fab) -> None:
    rep = analytic_capacity(fab)
    assert rep.bottleneck.utilization_at(rep.capacity_lots_per_day) == pytest.approx(1.0)


def test_raw_process_hours_reported(fab: Fab) -> None:
    rep = analytic_capacity(fab)
    assert rep.raw_process_hours == pytest.approx(fab.raw_process_hours())
    assert 60 < rep.raw_process_hours < 120  # 소규모 fab의 통상 범위


# ---- 흐름 / 반송 --------------------------------------------------------


def test_flow_report_basics(fab: Fab) -> None:
    geo = default_geometry()
    dm = DistanceMatrix.build(geo)
    asg = baseline.functional_layout(fab, geo)
    fr = flow_report(fab, geo, asg, dm)

    # 반송 횟수 = 스텝 수 + 1 (투입 → 첫 설비, 마지막 설비 → 출하)
    assert fr.moves_per_lot == pytest.approx(0.7 * 85 + 0.3 * 69)
    assert fr.distance_m_per_lot > 0
    assert fr.mean_move_m == pytest.approx(fr.distance_m_per_lot / fr.moves_per_lot)
    assert 0 < fr.mean_move_m < dm.max_m + geo.spine_length_m
    assert fr.vehicle_seconds_per_lot == pytest.approx(
        fr.loaded_seconds_per_lot + fr.empty_seconds_per_lot
    )
    assert fr.loaded_seconds_per_lot == pytest.approx(
        fr.handling_seconds_per_lot
        + fr.travel_seconds_per_lot
        + fr.stocker_seconds_per_lot
    )


def test_stocker_ops_counting(fab: Fab) -> None:
    """스토커 조작 횟수 = 통과하는 bay 경계 수. bay 교차 이동은 2회, 투입/출하는 1회."""
    geo = default_geometry()
    fr = flow_report(fab, geo, baseline.functional_layout(fab, geo))
    # 투입 1 + 출하 1 + bay 교차 × 2
    assert fr.stocker_ops_per_lot == pytest.approx(2.0 + 2 * fr.interbay_moves_per_lot)
    assert 0 < fr.interbay_moves_per_lot < fr.moves_per_lot
    assert fr.stocker_seconds_per_lot == pytest.approx(
        fr.stocker_ops_per_lot * fab.transport.stocker_time_s
    )


def test_intrabay_move_has_no_stocker_cost(fab: Fab) -> None:
    """같은 bay 안 이동에는 스토커 조작이 없다 — bay 배치의 근본 동기."""
    t = fab.transport
    intra = t.move_seconds(10.0, stocker_ops=0)
    inter = t.move_seconds(10.0, stocker_ops=2)
    assert inter - intra == pytest.approx(2 * t.stocker_time_s)
    assert inter > 4 * intra  # 분 단위 대 초 단위


def test_stocker_dominates_vehicle_load(fab: Fab) -> None:
    """스토커 항이 지배적이어야 한다 — 그래야 bay 교차 횟수가 최적화 레버가 된다."""
    geo = default_geometry()
    fr = flow_report(fab, geo, baseline.functional_layout(fab, geo))
    assert fr.stocker_seconds_per_lot > fr.travel_seconds_per_lot
    assert fr.stocker_seconds_per_lot > fr.handling_seconds_per_lot
    # 배치가 손댈 수 있는 비율이 절반을 넘어야 최적화가 의미를 가진다
    assert fr.layout_controllable_fraction > 0.5


def test_functional_layout_beats_random(fab: Fab) -> None:
    """기능별 배치는 무작위보다 이동거리와 bay 교차가 모두 적어야 한다."""
    geo = default_geometry()
    dm = DistanceMatrix.build(geo)
    func = flow_report(fab, geo, baseline.functional_layout(fab, geo), dm)
    randoms = [
        flow_report(fab, geo, baseline.random_layout(fab, geo, seed=s), dm)
        for s in range(20)
    ]
    assert func.distance_m_per_lot < sum(
        r.distance_m_per_lot for r in randoms
    ) / len(randoms)
    assert func.interbay_moves_per_lot < sum(
        r.interbay_moves_per_lot for r in randoms
    ) / len(randoms)


def test_vehicle_utilization_in_sensitive_band(fab: Fab) -> None:
    """SPEC §5.4: 반송차 가동률이 민감 구간(55~85%)에 있어야 배치가 처리량에 영향을 준다.

    너무 낮으면 반송이 여유로워 배치가 무관해지고, 너무 높으면 반송이 병목이 되어
    설비 배치가 아니라 반송차 증설이 답이 된다. 이 테스트가 깨지면 스토커 시간이나
    반송차 대수를 재조정해야 한다는 신호다.
    """
    geo = default_geometry()
    asg = baseline.functional_layout(fab, geo)
    fr = flow_report(fab, geo, asg)
    rate = analytic_capacity(fab).capacity_lots_per_day
    util = fr.vehicle_utilization(rate, fab.transport.vehicles)
    assert 0.55 <= util <= 0.85, f"반송차 가동률 {util:.1%} — 시나리오 재조정 필요"


def test_layout_quality_moves_vehicle_load(fab: Fab) -> None:
    """배치 품질 차이가 반송차 부하 차이로 나타나야 한다 (처리량 영향의 전달 경로)."""
    geo = default_geometry()
    dm = DistanceMatrix.build(geo)
    rate = analytic_capacity(fab).capacity_lots_per_day
    v = fab.transport.vehicles
    good = flow_report(fab, geo, baseline.functional_layout(fab, geo), dm)
    poor = flow_report(fab, geo, baseline.spread_layout(fab, geo), dm)
    gap = poor.vehicle_utilization(rate, v) - good.vehicle_utilization(rate, v)
    assert gap > 0.05, f"배치에 따른 가동률 차이가 {gap:.1%}뿐 — 레버가 너무 약하다"


def test_more_vehicles_lower_utilization(fab: Fab) -> None:
    geo = default_geometry()
    fr = flow_report(fab, geo, baseline.functional_layout(fab, geo))
    rate = analytic_capacity(fab).capacity_lots_per_day
    assert fr.vehicle_utilization(rate, 4) < fr.vehicle_utilization(rate, 2)
