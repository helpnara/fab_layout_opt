"""M3 기능 검증 — 배치 설비 · 설비 고장 · 반송.

M2의 기본 엔진 불변식은 `test_sim_basic.py`가 M3 기능을 끈 상태로 확인한다. 여기서는
켠 상태의 거동이 물리적으로 맞는지 본다.
"""

from __future__ import annotations

import pytest
from conftest import make_fab

from fablayout.core.analysis import analytic_capacity, flow_report
from fablayout.core.geometry import default_geometry
from fablayout.core.model import Fab, Product, Step, ToolGroup, TransportSpec
from fablayout.data.builtin import build_smallfab21
from fablayout.opt import baseline
from fablayout.sim import SimConfig, replicate, simulate
from fablayout.sim.metrics import Replications


def batch_fab(count: int, batch_size: int, minutes: float, visits: int = 1) -> Fab:
    g = ToolGroup("B", "batch", count, minutes, "batch", batch_size, 1e9, 0.0, 0.0, "test")
    steps = tuple(Step(i, "B", 1) for i in range(visits))
    return Fab("batchtest", {"B": g}, {"P": Product("P", "P", steps, 1.0)},
               25, TransportSpec(), "test")


def failing_fab(mtbf_h: float, mttr_h: float, minutes: float = 60.0) -> Fab:
    g = ToolGroup("F", "fail", 1, minutes, "single", 1, mtbf_h, mttr_h, 0.0, "test")
    return Fab("failtest", {"F": g}, {"P": Product("P", "P", (Step(0, "F", 1),), 1.0)},
               25, TransportSpec(), "test")


# =========================================================================
# 배치 설비
# =========================================================================


def test_batch_never_exceeds_size() -> None:
    fab = batch_fab(count=1, batch_size=4, minutes=60.0)
    r = simulate(fab, SimConfig(release_lots_per_day=80.0, warmup_days=5, run_days=60,
                                breakdowns=False, transport=False))
    g = r.groups["B"]
    assert g.mean_batch_size <= 4.0 + 1e-9
    assert g.starts == pytest.approx(g.batches * g.mean_batch_size, rel=1e-9)


def test_saturated_batches_are_always_full() -> None:
    """능력을 넘겨 투입하면 큐가 늘 차 있어 배치가 항상 가득 찬다."""
    fab = batch_fab(count=1, batch_size=4, minutes=60.0)   # 능력 96 lot/일
    r = simulate(fab, SimConfig(release_lots_per_day=150.0, warmup_days=10, run_days=90,
                                breakdowns=False, transport=False))
    g = r.groups["B"]
    assert g.mean_batch_size == pytest.approx(4.0)
    assert g.partial_batches == 0


def test_batch_fill_rate_rises_with_load() -> None:
    """부하가 오를수록 배치 충전율이 올라간다 (모을 lot이 많아지므로)."""
    fab = batch_fab(count=1, batch_size=4, minutes=60.0)
    common = dict(warmup_days=10, run_days=90, breakdowns=False, transport=False)
    sizes = [
        simulate(fab, SimConfig(release_lots_per_day=r, **common)).groups["B"].mean_batch_size
        for r in (40.0, 80.0, 120.0)
    ]
    assert sizes[0] < sizes[1] < sizes[2] <= 4.0


def test_low_load_uses_partial_batches() -> None:
    """저부하에서는 타임아웃으로 부분 배치가 나가야 한다. 안 그러면 lot이 영구 대기한다."""
    fab = batch_fab(count=1, batch_size=6, minutes=60.0)
    r = simulate(fab, SimConfig(release_lots_per_day=4.0, warmup_days=5, run_days=120,
                                breakdowns=False, transport=False,
                                batch_max_wait_minutes=60.0))
    g = r.groups["B"]
    assert g.mean_batch_size < 6.0
    assert g.partial_batches > 0
    assert r.completed > 0, "부분 배치가 없으면 lot이 영원히 나오지 않는다"


def test_batch_timeout_bounds_waiting() -> None:
    """대기시간이 타임아웃 + 처리시간 근처에서 멈춰야 한다."""
    fab = batch_fab(count=1, batch_size=8, minutes=30.0)
    max_wait = 45.0
    r = simulate(fab, SimConfig(release_lots_per_day=2.0, warmup_days=5, run_days=200,
                                breakdowns=False, transport=False,
                                batch_max_wait_minutes=max_wait))
    # 투입이 매우 드물어 거의 항상 타임아웃으로 나간다
    assert r.groups["B"].mean_queue_wait <= max_wait + 1.0


def test_batching_multiplies_capacity() -> None:
    """배치 크기 B면 처리 능력이 대략 B배가 된다."""
    fab = batch_fab(count=1, batch_size=4, minutes=60.0)
    common = dict(release_lots_per_day=200.0, warmup_days=10, run_days=90,
                  breakdowns=False, transport=False)
    with_batch = simulate(fab, SimConfig(batching=True, **common))
    without = simulate(fab, SimConfig(batching=False, **common))
    ratio = with_batch.throughput_lots_per_day / without.throughput_lots_per_day
    assert ratio == pytest.approx(4.0, rel=0.05), f"능력 배수 {ratio:.2f}"


def test_batch_duration_is_longest_member() -> None:
    """배치 소요시간은 구성원 중 최댓값 — 로는 한 레시피를 한 번 돌린다."""
    from fablayout.sim.batching import batch_duration

    assert batch_duration([10.0, 25.0, 13.0]) == 25.0
    assert batch_duration([7.0]) == 7.0


def test_dataset_batch_groups_active() -> None:
    """내장 데이터셋의 배치 그룹 3개가 실제로 배치로 동작하는지."""
    fab, geo = build_smallfab21(), default_geometry()
    asg = baseline.functional_layout(fab, geo)
    cap = analytic_capacity(fab).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 0.9, warmup_days=40,
                                run_days=120), asg, geo)
    assert r.groups["DIFF_FURN"].mean_batch_size > 4.0     # batch_size 6
    assert r.groups["CLEAN"].mean_batch_size > 1.5         # batch_size 4
    assert r.groups["PHOTO"].mean_batch_size == pytest.approx(1.0)


# =========================================================================
# 설비 고장
# =========================================================================


def test_saturated_tool_utilization_equals_availability() -> None:
    """포화된 설비의 가동률은 정확히 A = MTBF/(MTBF+MTTR)이어야 한다.

    고장간격을 가동시간 기준으로 세면, 쉬지 않고 도는 설비는 가동시간 B와 고장시간
    D = (B/MTBF)·MTTR 를 겪고 B + D = 달력시간이 되어 B/T = A가 된다. 이 등식이
    성립하지 않으면 고장 프로세스가 틀린 것이다.
    """
    mtbf, mttr = 100.0, 10.0
    fab = failing_fab(mtbf, mttr)
    a_theory = mtbf / (mtbf + mttr)
    r = simulate(fab, SimConfig(release_lots_per_day=100.0, warmup_days=200,
                                run_days=3000, transport=False, seed=5))
    g = r.groups["F"]
    assert g.utilization(r.window_minutes) == pytest.approx(a_theory, rel=0.03)
    assert g.availability(r.window_minutes) == pytest.approx(a_theory, rel=0.05)
    assert g.load_factor(r.window_minutes) == pytest.approx(1.0, rel=0.03)


def test_downtime_scales_with_busy_time() -> None:
    """고장은 가동시간 기준이므로, 부하가 절반이면 고장시간도 대략 절반이어야 한다.

    달력시간 기준으로 구현하면 노는 설비도 고장 나 이 관계가 깨진다.
    """
    mtbf, mttr = 100.0, 10.0
    fab = failing_fab(mtbf, mttr)
    common = dict(warmup_days=200, run_days=3000, transport=False, seed=11)
    hi = simulate(fab, SimConfig(release_lots_per_day=18.0, **common))
    lo = simulate(fab, SimConfig(release_lots_per_day=9.0, **common))
    hi_down = 1 - hi.groups["F"].availability(hi.window_minutes)
    lo_down = 1 - lo.groups["F"].availability(lo.window_minutes)
    ratio = hi_down / lo_down
    assert ratio == pytest.approx(2.0, rel=0.15), f"고장시간 비 {ratio:.2f}"
    # 이론값: 고장시간/달력 = 가동률 × MTTR/MTBF
    u = lo.groups["F"].utilization(lo.window_minutes)
    assert lo_down == pytest.approx(u * mttr / mtbf, rel=0.15)


def test_idle_tool_does_not_fail() -> None:
    """전혀 일하지 않는 설비는 고장 나지 않는다."""
    fab = make_fab([("A", 1, 10.0), ("IDLE", 1, 10.0)], ["A"], cv=0.0)
    fab.groups["IDLE"].__class__  # 라우트에 없으므로 착수 0
    r = simulate(fab, SimConfig(release_lots_per_day=50.0, warmup_days=5, run_days=200,
                                transport=False))
    assert r.groups["IDLE"].starts == 0
    assert r.groups["IDLE"].failures == 0
    assert r.groups["IDLE"].down_minutes == 0.0


def test_breakdowns_reduce_throughput() -> None:
    fab = failing_fab(50.0, 10.0)
    common = dict(release_lots_per_day=40.0, warmup_days=50, run_days=600,
                  transport=False, seed=3)
    with_bd = simulate(fab, SimConfig(breakdowns=True, **common))
    without = simulate(fab, SimConfig(breakdowns=False, **common))
    assert with_bd.throughput_lots_per_day < without.throughput_lots_per_day
    assert with_bd.groups["F"].failures > 0
    assert without.groups["F"].failures == 0


def test_processing_resumes_with_remaining_time() -> None:
    """고장이 나도 처리는 처음부터가 아니라 잔여시간만큼만 재개된다.

    lot 1개를 아주 긴 처리에 넣고 고장을 자주 내면, 완료시각이
    처리시간 + 고장시간 합과 맞아야 한다 (재시작이면 훨씬 길어진다).
    """
    fab = failing_fab(mtbf_h=1.0, mttr_h=0.5, minutes=600.0)
    r = simulate(fab, SimConfig(release_lots_per_day=1.0, warmup_days=0.0,
                                run_days=30, transport=False, max_lots=1,
                                arrival="deterministic", seed=9))
    assert r.completed == 1
    g = r.groups["F"]
    ct = r.cycle_times[0]
    assert g.failures > 3, "고장이 여러 번 나야 의미 있는 검증이 된다"
    assert ct == pytest.approx(600.0 + g.down_minutes, rel=1e-6), (
        f"사이클타임 {ct:.1f} vs 처리 600 + 고장 {g.down_minutes:.1f}"
    )


# =========================================================================
# 반송
# =========================================================================


def _dataset(vehicles: int = 2):
    fab = build_smallfab21(vehicles=vehicles)
    geo = default_geometry()
    return fab, geo, baseline.functional_layout(fab, geo)


def test_transport_off_without_layout() -> None:
    """배치 정보가 없으면 반송은 꺼진다 (M2 경로)."""
    fab = build_smallfab21()
    r = simulate(fab, SimConfig(release_lots_per_day=3.0, warmup_days=10, run_days=30))
    assert r.transport is None
    assert r.vehicle_utilization == 0.0


def test_move_count_matches_route_length() -> None:
    """반송 횟수 = 스텝 수 + 1 (투입 → 첫 설비, 마지막 설비 → 출하)."""
    fab, geo, asg = _dataset()
    cap = analytic_capacity(fab).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 0.6, warmup_days=40,
                                run_days=120), asg, geo)
    expected_per_lot = fab.weighted_moves_per_lot() + 1.0
    actual = r.transport.moves / r.completions_in_window
    assert actual == pytest.approx(expected_per_lot, rel=0.05)


def test_stocker_ops_counted() -> None:
    fab, geo, asg = _dataset()
    cap = analytic_capacity(fab).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 0.6, warmup_days=40,
                                run_days=120), asg, geo)
    per_move = r.transport.stocker_ops / r.transport.moves
    assert 0.0 < per_move <= 2.0, "이동당 스토커 조작은 0~2회"
    # 기능별 배치에서도 bay 교차가 상당하므로 평균이 1을 넘는다
    assert per_move > 1.0


def test_more_vehicles_reduce_waiting() -> None:
    geo = default_geometry()
    cap = analytic_capacity(build_smallfab21()).capacity_lots_per_day
    cfg = SimConfig(release_lots_per_day=cap * 0.9, warmup_days=40, run_days=150)
    waits = []
    for v in (1, 2, 4):
        fab = build_smallfab21(vehicles=v)
        asg = baseline.functional_layout(fab, geo)
        r = simulate(fab, cfg, asg, geo)
        waits.append(r.transport.mean_wait_minutes)
    assert waits[0] > waits[1] > waits[2]


def test_vehicle_utilization_matches_analytic_estimate() -> None:
    """DES 실측 반송차 가동률이 해석적 추정과 대략 맞아야 한다.

    해석적 추정(`flow_report`)은 빈차 회송을 계수 0.6으로 근사하고 목적지를 그룹 평균
    위치로 잡으므로 정확히 같을 수는 없다. 30% 이내면 두 계산이 같은 세계를 보고 있다는
    뜻이고, 크게 어긋나면 어느 한쪽이 틀린 것이다.
    """
    fab, geo, asg = _dataset()
    cap = analytic_capacity(fab).capacity_lots_per_day
    rate = cap * 0.9
    r = simulate(fab, SimConfig(release_lots_per_day=rate, warmup_days=40,
                                run_days=180), asg, geo)
    predicted = flow_report(fab, geo, asg).vehicle_utilization(
        r.throughput_lots_per_day, fab.transport.vehicles
    )
    assert r.vehicle_utilization == pytest.approx(predicted, rel=0.30), (
        f"DES {r.vehicle_utilization:.1%} vs 해석적 {predicted:.1%}"
    )


def test_lot_is_processed_in_the_bay_it_was_delivered_to() -> None:
    """배달된 bay의 설비만 그 lot을 처리한다 — 물리적 일관성.

    이 규칙이 없으면 반송차가 A bay에 내려놓은 lot을 B bay 설비가 처리하는,
    이동 없는 순간이동이 생긴다.
    """
    fab = build_smallfab21()
    geo = default_geometry()
    asg = baseline.spread_layout(fab, geo)   # 그룹이 여러 bay에 흩어진 배치
    from fablayout.sim.runner import Simulation

    sim = Simulation(fab, SimConfig(release_lots_per_day=3.0, warmup_days=0.0,
                                    run_days=40), asg, geo)
    violations: list[str] = []
    original = sim._start_processing

    def spy(st, tool_idx, members, remaining):
        bay = sim._slot_bay[st.slot_idx[tool_idx]]
        for lot in members:
            if lot.dest_bay != bay:
                violations.append(f"{lot.lot_id}: 배달 bay {lot.dest_bay} ≠ 처리 bay {bay}")
        original(st, tool_idx, members, remaining)

    sim._start_processing = spy  # type: ignore[method-assign]
    sim.run()
    assert not violations, violations[:5]


def test_scattered_layout_avoids_severe_imbalance() -> None:
    """부하 인지 배달: 그룹이 흩어져도 한 bay만 포화되면 안 된다.

    거리만 보고 가장 가까운 bay로 보내면 심한 불균형이 생겨 흩어진 배치가 실제보다
    훨씬 나빠 보인다.
    """
    fab = build_smallfab21()
    geo = default_geometry()
    asg = baseline.spread_layout(fab, geo)
    cap = analytic_capacity(fab).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 0.85, warmup_days=40,
                                run_days=150), asg, geo)
    # 흩어진 배치라도 처리량이 투입률을 따라가야 한다
    assert r.throughput_lots_per_day == pytest.approx(cap * 0.85, rel=0.08)


# =========================================================================
# 통합
# =========================================================================


def test_full_model_never_exceeds_analytic_capacity() -> None:
    """모든 기능을 켠 상태에서도 실측 ≤ 해석적 상한."""
    fab, geo, asg = _dataset()
    cap = analytic_capacity(fab).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 1.1, warmup_days=60,
                                run_days=300), asg, geo)
    assert r.throughput_lots_per_day <= cap * 1.001


def test_full_model_x_factor_in_realistic_range() -> None:
    """양산 fab의 X-factor는 통상 2~4다."""
    fab, geo, asg = _dataset()
    cap = analytic_capacity(fab).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 0.92, warmup_days=40,
                                run_days=180), asg, geo)
    assert 1.5 <= r.x_factor <= 5.0, f"X-factor {r.x_factor:.2f}"


def test_layout_changes_cycle_time_not_throughput() -> None:
    """**M3의 핵심 실측 결과.**

    이 fab에서 배치는 사이클타임과 WIP를 바꾸지만 처리량은 바꾸지 못한다. 병목이
    설비(확산로·증착)이지 반송이 아니기 때문이다. 최적화 목표 함수 설계에 직결되는
    사실이라 테스트로 고정해 둔다 — 뒤집히면 결론을 다시 봐야 한다.
    """
    fab = build_smallfab21()
    geo = default_geometry()
    cap = analytic_capacity(fab).capacity_lots_per_day
    # 신뢰구간이 차이보다 작아야 결론을 낼 수 있다 — 짧은 실행·적은 반복으로는 부족하다
    cfg = SimConfig(release_lots_per_day=cap * 0.94, warmup_days=60, run_days=300)

    good = Replications(replicate(fab, cfg, n=5,
                                  assignment=baseline.functional_layout(fab, geo), geo=geo))
    poor = Replications(replicate(fab, cfg, n=5,
                                  assignment=baseline.spread_layout(fab, geo), geo=geo))

    # 처리량은 같다 (둘 다 투입률을 따라간다)
    assert good.mean("throughput_lots_per_day") == pytest.approx(
        poor.mean("throughput_lots_per_day"), rel=0.03
    )
    # 사이클타임은 유의하게 다르다
    gap = poor.mean("mean_cycle_hours") - good.mean("mean_cycle_hours")
    assert gap > 0, "기능별 배치가 흩뿌림보다 사이클타임이 짧아야 한다"
    assert gap > good.half_width("mean_cycle_hours") + poor.half_width("mean_cycle_hours"), (
        f"차이 {gap:.0f}h가 신뢰구간 안이라 유의하지 않다"
    )
    assert poor.mean("mean_wip") > good.mean("mean_wip")


def test_full_model_is_deterministic() -> None:
    fab, geo, asg = _dataset()
    cfg = SimConfig(release_lots_per_day=5.0, warmup_days=20, run_days=60)
    a = simulate(fab, cfg, asg, geo)
    b = simulate(fab, cfg, asg, geo)
    assert a.cycle_times == b.cycle_times
    assert a.events == b.events
    assert a.transport.moves == b.transport.moves
    assert a.groups["DIFF_FURN"].batches == b.groups["DIFF_FURN"].batches
