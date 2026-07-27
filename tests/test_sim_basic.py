"""DES 실행기 기본 검증 — 흐름 정확성, 관측 창, 정합성, 재현성."""

from __future__ import annotations

import pytest
from conftest import make_fab

from fablayout.core.analysis import analytic_capacity
from fablayout.data.builtin import build_smallfab21
from fablayout.sim import SimConfig, Simulation, replicate, simulate
from fablayout.sim.metrics import Replications, TimeIntegral, overlap


# ---- 시간 가중 적분 ------------------------------------------------------


def test_time_integral_basic() -> None:
    ti = TimeIntegral(0.0, 10.0)
    ti.set(0.0, 2.0)
    ti.set(5.0, 4.0)
    ti.finish(10.0)
    assert ti.area == pytest.approx(2 * 5 + 4 * 5)
    assert ti.mean() == pytest.approx(3.0)
    assert ti.peak == 4.0


def test_time_integral_clips_to_window() -> None:
    """워밍업 구간은 통계에서 빠져야 한다."""
    ti = TimeIntegral(10.0, 20.0)
    ti.set(0.0, 5.0)     # 창 이전
    ti.set(15.0, 1.0)
    ti.finish(30.0)      # 창 이후
    assert ti.area == pytest.approx(5 * 5 + 1 * 5)
    assert ti.mean() == pytest.approx(3.0)


def test_overlap() -> None:
    assert overlap(0, 10, 5, 20) == 5
    assert overlap(0, 3, 5, 20) == 0
    assert overlap(6, 8, 5, 20) == 2
    assert overlap(15, 25, 5, 20) == 5


# ---- 흐름 정확성 ---------------------------------------------------------


def test_every_lot_visits_every_step() -> None:
    """스텝 착수 횟수 = 완료 lot 수 × 그 그룹 방문 횟수 (보존 법칙)."""
    fab = make_fab([("A", 1, 10.0), ("B", 1, 10.0)], ["A", "B", "A"], cv=0.0)
    cfg = SimConfig(release_lots_per_day=20.0, warmup_days=0.0, run_days=20.0,
                    arrival="deterministic")
    sim = Simulation(fab, cfg)
    r = sim.run()
    # 라우트에서 A는 2회, B는 1회 방문한다
    assert r.groups["A"].starts == pytest.approx(2 * r.groups["B"].starts, rel=0.05)


def test_single_lot_no_contention_gives_exact_cycle_time() -> None:
    """경쟁이 없으면 사이클타임 = 처리시간 합. 흐름 로직의 가장 직접적인 검증."""
    fab = make_fab([("A", 1, 10.0), ("B", 1, 25.0)], ["A", "B", "A"], cv=0.0)
    cfg = SimConfig(release_lots_per_day=1.0, warmup_days=0.0, run_days=30.0,
                    arrival="deterministic", max_lots=1)
    r = simulate(fab, cfg)
    assert r.completed == 1
    assert r.cycle_times[0] == pytest.approx(10.0 + 25.0 + 10.0)
    assert r.x_factor == pytest.approx(1.0)


def test_throughput_tracks_release_below_capacity() -> None:
    """포화 전에는 처리량 = 투입률 (들어온 만큼 나간다)."""
    fab = make_fab([("A", 2, 30.0)], ["A"], cv=0.1)
    cfg = SimConfig(release_lots_per_day=40.0, warmup_days=5.0, run_days=60.0)
    r = simulate(fab, cfg)
    assert r.throughput_lots_per_day == pytest.approx(40.0, rel=0.05)


def test_throughput_saturates_above_capacity() -> None:
    """능력을 넘겨 투입하면 처리량은 능력에서 멈추고 WIP만 늘어난다."""
    fab = make_fab([("A", 1, 30.0)], ["A"], cv=0.1)  # 능력 = 48 lot/일
    cfg = SimConfig(release_lots_per_day=80.0, warmup_days=5.0, run_days=60.0)
    r = simulate(fab, cfg)
    assert r.throughput_lots_per_day < 50.0
    assert r.throughput_lots_per_day == pytest.approx(48.0, rel=0.08)
    assert r.utilizations()["A"] > 0.97


def test_wip_diverges_when_oversaturated() -> None:
    """포화 초과에서 WIP가 선형 발산해야 한다 — 생산능력 탐색(M4)의 판정 근거."""
    fab = make_fab([("A", 1, 30.0)], ["A"], cv=0.1)
    short = simulate(fab, SimConfig(release_lots_per_day=80.0, warmup_days=0.0, run_days=20.0))
    long = simulate(fab, SimConfig(release_lots_per_day=80.0, warmup_days=0.0, run_days=60.0))
    assert long.mean_wip > short.mean_wip * 2


# ---- 관측 창 -------------------------------------------------------------


def test_warmup_excluded_from_statistics() -> None:
    fab = make_fab([("A", 2, 30.0)], ["A"], cv=0.1)
    cfg = SimConfig(release_lots_per_day=40.0, warmup_days=10.0, run_days=30.0)
    r = simulate(fab, cfg)
    assert r.window_minutes == pytest.approx(30 * 1440)
    # 창 안 투입 수가 투입률 × 창 길이와 맞아야 한다
    assert r.released == pytest.approx(40 * 30, rel=0.1)


def test_cycle_time_samples_are_window_scoped(fab_dataset=None) -> None:
    """사이클타임 표본은 창 안에서 투입되고 완료된 lot만."""
    fab = make_fab([("A", 2, 30.0)], ["A"], cv=0.1)
    cfg = SimConfig(release_lots_per_day=40.0, warmup_days=10.0, run_days=30.0)
    r = simulate(fab, cfg)
    assert 0 < r.completed <= r.released
    assert r.completed + r.censored <= r.released + 1


def test_low_load_x_factor_approaches_one() -> None:
    """저부하에서는 대기가 없어 사이클타임이 순수 처리시간에 수렴한다."""
    fab = build_smallfab21()
    cap = analytic_capacity(fab, batching=False).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 0.35, warmup_days=40, run_days=200))
    assert 1.0 <= r.x_factor <= 1.15, f"X-factor {r.x_factor:.3f}"


def test_x_factor_grows_with_load() -> None:
    fab = build_smallfab21()
    cap = analytic_capacity(fab, batching=False).capacity_lots_per_day
    lo = simulate(fab, SimConfig(release_lots_per_day=cap * 0.4, warmup_days=40, run_days=200))
    hi = simulate(fab, SimConfig(release_lots_per_day=cap * 0.9, warmup_days=40, run_days=200))
    assert hi.x_factor > lo.x_factor
    assert hi.mean_wip > lo.mean_wip


# ---- 해석적 상한과의 정합성 ---------------------------------------------


def test_des_never_exceeds_analytic_capacity() -> None:
    """DES 실측 처리량은 해석적 상한을 넘을 수 없다 — 반드시 성립해야 하는 부등식.

    상한 계산은 대기·변동을 무시하므로 낙관적이다. 실측이 이를 넘으면 둘 중 하나가
    틀린 것이다.
    """
    fab = build_smallfab21()
    cap = analytic_capacity(fab, batching=False).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 3, warmup_days=40, run_days=200))
    assert r.throughput_lots_per_day <= cap * 1.001, (
        f"실측 {r.throughput_lots_per_day:.3f} > 상한 {cap:.3f}"
    )


def test_utilization_never_exceeds_one() -> None:
    fab = build_smallfab21()
    cap = analytic_capacity(fab, batching=False).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 2, warmup_days=40, run_days=120))
    for gid, u in r.utilizations().items():
        assert 0.0 <= u <= 1.0 + 1e-9, f"{gid} 가동률 {u}"


def test_bottleneck_matches_analytic() -> None:
    """DES가 지목하는 병목이 해석적 계산과 일치해야 한다."""
    fab = build_smallfab21()
    rep = analytic_capacity(fab, batching=False)
    r = simulate(fab, SimConfig(release_lots_per_day=rep.capacity_lots_per_day * 0.9,
                                warmup_days=40, run_days=200))
    assert r.bottlenecks(1)[0][0] == rep.bottleneck.gid


# ---- 재현성과 공통난수 ---------------------------------------------------


def test_same_seed_gives_identical_results() -> None:
    fab = build_smallfab21()
    cfg = SimConfig(release_lots_per_day=1.0, warmup_days=10, run_days=60)
    a, b = simulate(fab, cfg), simulate(fab, cfg)
    assert a.cycle_times == b.cycle_times
    assert a.completions_in_window == b.completions_in_window
    assert a.mean_wip == b.mean_wip
    assert a.events == b.events


def test_different_seed_gives_different_results() -> None:
    from dataclasses import replace

    fab = build_smallfab21()
    cfg = SimConfig(release_lots_per_day=1.0, warmup_days=10, run_days=60)
    a = simulate(fab, cfg)
    b = simulate(fab, replace(cfg, seed=cfg.seed + 1))
    assert a.cycle_times != b.cycle_times


def test_lot_process_times_are_layout_independent() -> None:
    """공통난수: 같은 lot은 어떤 조건에서도 같은 처리시간을 겪어야 한다.

    이것이 깨지면 배치 A와 B의 비교에서 배치 효과와 난수 노이즈가 섞인다.
    """
    from fablayout.sim.rng import RandomStreams, draw_process_times

    means, cvs = (60.0, 45.0, 40.0), (0.15, 0.15, 0.15)
    s1, s2 = RandomStreams(123), RandomStreams(123)
    for lot_id in (1, 7, 999):
        a = draw_process_times(s1.lot_stream(lot_id), means, cvs)
        b = draw_process_times(s2.lot_stream(lot_id), means, cvs)
        assert a == b
    # 다른 lot은 달라야 한다
    assert draw_process_times(s1.lot_stream(1), means, cvs) != draw_process_times(
        s1.lot_stream(2), means, cvs
    )


def test_dispatch_rule_changes_outcome() -> None:
    """디스패칭 규칙이 실제로 결과를 바꾸는지 (플러그인 지점이 살아 있는지)."""
    from dataclasses import replace

    fab = make_fab([("A", 1, 30.0), ("B", 1, 10.0)], ["A", "B"], cv=0.5)
    cfg = SimConfig(release_lots_per_day=44.0, warmup_days=10, run_days=120)
    fifo = simulate(fab, cfg)
    lifo = simulate(fab, replace(cfg, dispatch="lifo"))
    # 평균 사이클타임은 비슷해도 분산(p95)은 LIFO가 훨씬 크다
    assert lifo.p95_cycle_hours > fifo.p95_cycle_hours


def test_srpt_beats_fifo_on_mean_cycle_time() -> None:
    """최소잔여작업 규칙은 평균 사이클타임을 줄인다 (대기행렬 이론의 고전적 결과)."""
    from dataclasses import replace

    fab = make_fab([("A", 1, 20.0)], ["A", "A", "A"], cv=1.0)
    cfg = SimConfig(release_lots_per_day=20.0, warmup_days=20, run_days=300)
    fifo = simulate(fab, cfg)
    srpt = simulate(fab, replace(cfg, dispatch="srpt"))
    assert srpt.mean_cycle_hours <= fifo.mean_cycle_hours


def test_unknown_dispatch_rule_raises() -> None:
    fab = make_fab([("A", 1, 10.0)], ["A"])
    with pytest.raises(KeyError, match="알 수 없는 디스패칭"):
        simulate(fab, SimConfig(release_lots_per_day=1.0, dispatch="nope"))


# ---- 반복과 신뢰구간 -----------------------------------------------------


def test_replications_produce_confidence_interval() -> None:
    fab = make_fab([("A", 2, 30.0)], ["A"], cv=0.3)
    cfg = SimConfig(release_lots_per_day=40.0, warmup_days=10, run_days=90)
    reps = Replications(replicate(fab, cfg, n=4))
    m = reps.mean("throughput_lots_per_day")
    assert m == pytest.approx(40.0, rel=0.06)
    assert reps.half_width("throughput_lots_per_day") >= 0.0
    assert reps.relative_error("throughput_lots_per_day") < 0.10


def test_replications_use_independent_seeds() -> None:
    fab = make_fab([("A", 1, 30.0)], ["A"], cv=0.4)
    cfg = SimConfig(release_lots_per_day=40.0, warmup_days=5, run_days=40)
    runs = replicate(fab, cfg, n=3)
    assert len({tuple(r.cycle_times[:5]) for r in runs}) == 3


# ---- 기타 ----------------------------------------------------------------


def test_deterministic_arrivals_have_no_release_variability() -> None:
    fab = make_fab([("A", 4, 10.0)], ["A"], cv=0.0)
    cfg = SimConfig(release_lots_per_day=48.0, warmup_days=0.0, run_days=10.0,
                    arrival="deterministic")
    r = simulate(fab, cfg)
    assert r.released == pytest.approx(480, abs=2)
    # 여유 설비 + 무변동 → 대기 없음
    assert r.x_factor == pytest.approx(1.0, abs=1e-6)


def test_wip_series_optional() -> None:
    fab = make_fab([("A", 2, 30.0)], ["A"], cv=0.2)
    cfg = SimConfig(release_lots_per_day=40.0, warmup_days=2, run_days=8,
                    collect_wip_series=True, wip_sample_minutes=720.0)
    r = simulate(fab, cfg)
    assert len(r.wip_series) > 10
    assert all(t >= 0 for t, _ in r.wip_series)
    # 기본은 꺼져 있어야 한다
    assert simulate(fab, SimConfig(release_lots_per_day=40.0, run_days=8)).wip_series == []


def test_zero_release_rate_rejected() -> None:
    fab = make_fab([("A", 1, 10.0)], ["A"])
    with pytest.raises(ValueError, match="투입률"):
        simulate(fab, SimConfig(release_lots_per_day=0.0))


def test_queue_metrics_reported() -> None:
    fab = make_fab([("A", 1, 30.0)], ["A"], cv=0.3)
    r = simulate(fab, SimConfig(release_lots_per_day=40.0, warmup_days=10, run_days=60))
    g = r.groups["A"]
    assert g.queue_entries > 0
    assert g.mean_queue_wait > 0
    assert g.mean_queue_len > 0
    assert g.peak_queue_len >= g.mean_queue_len
    assert r.queue_hotspots(1)[0][0] == "A"


def test_first_lot_released_at_time_zero() -> None:
    """첫 투입은 t=0. 한 간격 뒤로 미루면 모든 결과가 그만큼 평행이동한다."""
    fab = make_fab([("A", 1, 10.0)], ["A"], cv=0.0)
    cfg = SimConfig(release_lots_per_day=24.0, warmup_days=0.0, run_days=1.0,
                    arrival="deterministic", max_lots=1)
    r = simulate(fab, cfg)
    assert r.completed == 1
    assert r.cycle_times[0] == pytest.approx(10.0)
    assert r.completions_in_window == 1


def test_max_lots_caps_release() -> None:
    fab = make_fab([("A", 4, 10.0)], ["A"], cv=0.0)
    cfg = SimConfig(release_lots_per_day=100.0, warmup_days=0.0, run_days=50.0,
                    arrival="deterministic", max_lots=37)
    r = simulate(fab, cfg)
    assert r.completed == 37


def test_engine_throughput_regression() -> None:
    """엔진 속도 회귀 감시.

    실측은 37만 이벤트/초 수준이다. 여기서 크게 떨어지면 최적화 파이프라인의
    후보 수를 다시 잡아야 하므로, 넉넉한 하한(10만)으로 급격한 퇴행만 잡는다.
    CI 머신 성능 편차를 감안해 절대 하한은 낮게 둔다.
    """
    fab = build_smallfab21()
    cap = analytic_capacity(fab, batching=False).capacity_lots_per_day
    r = simulate(fab, SimConfig(release_lots_per_day=cap * 0.9,
                                warmup_days=30, run_days=600))
    assert r.events > 5000, "측정 표본이 너무 작다"
    rate = r.events / r.wall_seconds
    assert rate > 100_000, f"엔진 속도 {rate:,.0f} 이벤트/초 — 심각한 퇴행"
