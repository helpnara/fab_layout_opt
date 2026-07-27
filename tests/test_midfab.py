"""중규모 데이터셋·흐름 구성·대리지표 검증 (탐색공간 확대)."""

from __future__ import annotations

import pytest

from fablayout.core.analysis import analytic_capacity, flow_report
from fablayout.data.midfab import (
    build_midfab,
    midfab_geometry,
    required_counts,
)
from fablayout.opt import baseline
from fablayout.opt.construct import flow_layout, group_flow_totals, pair_flows, top_pairs
from fablayout.opt.cost import CostModel
from fablayout.opt.surrogate import Surrogate, spearman


@pytest.fixture(scope="module")
def ctx():
    fab, geo = build_midfab(), midfab_geometry()
    return fab, geo


# =========================================================================
# 데이터셋 구조
# =========================================================================


def test_scale_is_larger_than_smallfab(ctx) -> None:
    """탐색공간을 키운 것이 목적이므로 규모가 실제로 커져야 한다."""
    from fablayout.core.geometry import default_geometry
    from fablayout.data.builtin import build_smallfab21

    fab, geo = ctx
    small, sgeo = build_smallfab21(), default_geometry()
    assert fab.total_tools > small.total_tools * 3
    assert geo.slot_count > sgeo.slot_count * 3
    assert len(geo.bays) > len(sgeo.bays) * 2
    assert geo.spine_length_m > sgeo.spine_length_m * 1.8
    assert len(fab.groups) > len(small.groups)
    assert len(fab.products) > len(small.products)


def test_tools_fit_with_headroom(ctx) -> None:
    fab, geo = ctx
    assert fab.total_tools <= geo.slot_count
    fill = fab.total_tools / geo.slot_count
    assert 0.6 <= fill <= 0.9, f"채움률 {fill:.0%}"


def test_track_is_the_most_visited_group(ctx) -> None:
    """트랙이 최다 방문이어야 한다 — 배치 결정이 의미를 갖는 구조의 핵심.

    lot이 `트랙 → 노광 → 트랙`으로 오가므로 트랙 방문은 노광의 2배다. 그룹별로만
    뭉치는 배치는 이 왕복을 전부 bay 교차로 만든다.
    """
    fab, _ = ctx
    visits = fab.weighted_visits()
    assert max(visits, key=lambda g: visits[g]) == "TRACK"
    photo = visits["PHOTO_ARF"] + visits["PHOTO_KRF"]
    assert visits["TRACK"] == pytest.approx(2 * photo)


def test_track_photo_is_the_top_flow_pair(ctx) -> None:
    fab, _ = ctx
    a, b, _ = top_pairs(fab, 1)[0]
    assert {a, b} == {"TRACK", "PHOTO_ARF"}


def test_near_bottleneck_structure(ctx) -> None:
    """근접 병목이 여럿이어야 최적화 여지가 있다."""
    fab, _ = ctx
    rep = analytic_capacity(fab)
    near = rep.near_bottleneck(0.20)
    assert len(near) >= 5, f"근접 병목 {len(near)}개"
    assert len(near) <= len(rep.groups) - 4


def test_counts_scale_with_target() -> None:
    """설비 대수는 목표 처리량에서 역산된다 — 규모를 바꿀 수 있어야 한다."""
    small = required_counts(10.0)
    big = required_counts(30.0)
    assert sum(big.values()) > sum(small.values()) * 2
    for gid in small:
        assert big[gid] >= small[gid]


def test_capacity_matches_target(ctx) -> None:
    """역산한 대수가 목표 처리량을 실제로 감당해야 한다."""
    fab, _ = ctx
    bound = analytic_capacity(fab).capacity_lots_per_day
    assert bound >= 20.0
    assert bound <= 20.0 * 1.35, "여유가 너무 크면 병목 구조가 흐려진다"


def test_routes_are_reentrant(ctx) -> None:
    fab, _ = ctx
    for p in fab.products.values():
        counts = p.visit_counts()
        assert counts["TRACK"] >= 8
        assert p.layer_count >= 8
        assert p.step_count >= 70


def test_source_flags_synthetic(ctx) -> None:
    fab, _ = ctx
    assert "원본 수치 아님" in fab.source


def test_geometry_is_symmetric(ctx) -> None:
    _, geo = ctx
    north = [b for b in geo.bays if b.bay_side == "N"]
    south = [b for b in geo.bays if b.bay_side == "S"]
    assert len(north) == len(south)


# =========================================================================
# 기준선과 흐름 구성
# =========================================================================


@pytest.mark.parametrize("kind", ["functional", "family", "spread", "random"])
def test_baselines_place_everything(ctx, kind: str) -> None:
    fab, geo = ctx
    asg = baseline.build(kind, fab, geo, seed=3)
    assert len(asg.placement) == fab.total_tools
    assert len(asg.occupied()) == fab.total_tools


def test_family_layout_colocates_track_and_scanner(ctx) -> None:
    """공정 계열 배치는 트랙과 스캐너를 같은 구역에 둔다 — 실무에 가까운 기준선."""
    fab, geo = ctx
    asg = baseline.family_layout(fab, geo)
    assert asg.bay_of_group("TRACK") & asg.bay_of_group("PHOTO_ARF")


def test_family_crosses_fewer_bays_than_functional(ctx) -> None:
    """계열별 배치가 그룹별 배치보다 bay 교차가 적어야 한다."""
    fab, geo = ctx
    fam = flow_report(fab, geo, baseline.family_layout(fab, geo))
    fun = flow_report(fab, geo, baseline.functional_layout(fab, geo))
    assert fam.interbay_fraction < fun.interbay_fraction


def test_flow_layout_keeps_groups_intact(ctx) -> None:
    """흐름 구성이 그룹을 쪼개면 서버 풀이 분할되어 오히려 나빠진다.

    bay보다 큰 그룹은 어쩔 수 없이 나뉘지만, 그 외에는 통째로 유지되어야 한다.
    """
    fab, geo = ctx
    asg = flow_layout(fab, geo)
    per_bay = 2 * geo.positions_per_side
    for gid, n in fab.tool_counts.items():
        if n and n <= per_bay:
            assert len(asg.bay_of_group(gid)) == 1, f"{gid}가 쪼개졌다"


def test_flow_layout_beats_random_on_crossings(ctx) -> None:
    fab, geo = ctx
    flow = flow_report(fab, geo, flow_layout(fab, geo))
    rand = flow_report(fab, geo, baseline.random_layout(fab, geo, seed=3))
    assert flow.interbay_moves_per_lot < rand.interbay_moves_per_lot


def test_pair_flows_are_symmetric_and_weighted(ctx) -> None:
    fab, _ = ctx
    flows = pair_flows(fab)
    assert all(a < b for a, b in flows), "쌍은 정렬된 형태로 한 번만 세야 한다"
    totals = group_flow_totals(fab)
    assert totals["TRACK"] == max(totals.values())


def test_capex_within_reasonable_range(ctx) -> None:
    fab, _ = ctx
    m = CostModel()
    capex = m.baseline_capex(fab)
    assert capex > 0
    # 노광이 가장 비싼 항목이어야 한다 (대수 재구성의 구조를 결정)
    assert m.breakdown(fab.tool_counts, fab.transport.vehicles)[0][0].startswith("PHOTO")


# =========================================================================
# 대리지표
# =========================================================================


def test_surrogate_terms_are_positive(ctx) -> None:
    fab, geo = ctx
    sur = Surrogate.build(fab, geo)
    t = sur.terms(baseline.family_layout(fab, geo))
    assert t.stocker_minutes > 0
    assert t.travel_minutes > 0
    assert t.split_penalty >= 0
    assert 0 < t.interbay_moves


def test_surrogate_penalizes_splitting(ctx) -> None:
    """그룹을 흩뿌린 배치의 분할 항이 뭉친 배치보다 커야 한다."""
    fab, geo = ctx
    sur = Surrogate.build(fab, geo)
    tight = sur.terms(baseline.functional_layout(fab, geo))
    loose = sur.terms(baseline.spread_layout(fab, geo))
    assert loose.split_penalty > tight.split_penalty * 3
    assert loose.split_groups > tight.split_groups


def test_surrogate_is_deterministic(ctx) -> None:
    """대리지표는 잡음이 0이어야 한다 — 그것이 DES 대신 쓰는 이유다."""
    fab, geo = ctx
    sur = Surrogate.build(fab, geo)
    asg = baseline.family_layout(fab, geo)
    assert sur.score(asg) == sur.score(asg)


def test_surrogate_is_fast(ctx) -> None:
    """DES 1회(약 5초)보다 훨씬 빨라야 스크리닝에 쓸 수 있다."""
    import time

    fab, geo = ctx
    sur = Surrogate.build(fab, geo)
    asg = baseline.family_layout(fab, geo)
    t0 = time.perf_counter()
    for _ in range(20):
        sur.score(asg)
    per_call = (time.perf_counter() - t0) / 20
    assert per_call < 0.05, f"{per_call * 1000:.1f}ms/회"


def test_spearman_basic() -> None:
    assert spearman([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert abs(spearman([1, 2, 3], [2, 2, 2])) < 1e-9      # 분산 0
    assert spearman([1], [1]) == 0.0                        # 표본 부족


def test_spearman_handles_ties() -> None:
    assert spearman([1, 1, 2, 2], [1, 1, 2, 2]) == pytest.approx(1.0)
