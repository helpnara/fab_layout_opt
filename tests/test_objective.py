"""목표 함수·예산·생산능력 탐색 검증 (M4)."""

from __future__ import annotations

import pytest

from fablayout.core.analysis import analytic_capacity
from fablayout.core.geometry import default_geometry
from fablayout.data.builtin import build_smallfab21
from fablayout.opt import baseline
from fablayout.opt.cost import VEHICLE, Budget, CostModel
from fablayout.opt.objective import Candidate, Objective, Reject
from fablayout.sim import SimConfig, simulate
from fablayout.sim.capacity import check_stability, find_capacity, wip_drift


@pytest.fixture(scope="module")
def ctx():
    fab, geo = build_smallfab21(), default_geometry()
    return fab, geo, baseline.functional_layout(fab, geo)


@pytest.fixture(scope="module")
def obj(ctx):
    fab, geo, _ = ctx
    return Objective.default(fab, geo, target_fraction=0.85, run_days=120, replications=3)


# =========================================================================
# 비용 모델
# =========================================================================


def test_capex_is_sum_of_units(ctx) -> None:
    fab, _, _ = ctx
    m = CostModel()
    expected = sum(m.unit_cost[g] * n for g, n in fab.tool_counts.items())
    expected += m.unit_cost[VEHICLE] * fab.transport.vehicles
    assert m.baseline_capex(fab) == pytest.approx(expected)


def test_scanner_dominates_capex(ctx) -> None:
    """스캐너가 가장 비싸다 — 대수 재구성의 여지를 결정하는 구조."""
    fab, _, _ = ctx
    m = CostModel()
    top = m.breakdown(fab.tool_counts, fab.transport.vehicles)[0]
    assert top[0] == "PHOTO"
    assert top[2] / m.baseline_capex(fab) > 0.30


def test_budget_same_as_baseline_fits_exactly(ctx) -> None:
    """기본 예산은 기준 구성과 정확히 같다 — "돈을 더 쓰지 않고" 실험."""
    fab, _, _ = ctx
    b = Budget.same_as_baseline(fab)
    assert b.fits(fab.tool_counts, fab.transport.vehicles)
    assert b.headroom(fab.tool_counts, fab.transport.vehicles) == pytest.approx(0.0)
    over = dict(fab.tool_counts)
    over["METRO"] += 1
    assert not b.fits(over, fab.transport.vehicles)


def test_cost_model_source_flags_placeholder() -> None:
    assert "자리표시자" in CostModel().source


# =========================================================================
# 지속 가능성 판정
# =========================================================================


def _stability_at(fab, geo, asg, fraction: float):
    bound = analytic_capacity(fab).capacity_lots_per_day
    rate = bound * fraction
    cfg = SimConfig(release_lots_per_day=rate, warmup_days=60, run_days=180,
                    collect_wip_series=True, wip_sample_minutes=1440.0)
    r = simulate(fab, cfg, asg, geo)
    return check_stability(r, rate, cfg.warmup_days, cfg.run_days)


def test_sustainable_below_capacity(ctx) -> None:
    fab, geo, asg = ctx
    v = _stability_at(fab, geo, asg, 0.75)
    assert v.sustainable, v.reason
    assert v.throughput_ratio > 0.99


def test_not_sustainable_above_capacity(ctx) -> None:
    """능력을 넘겨 투입하면 완료율이 떨어져 지속 불가로 판정되어야 한다."""
    fab, geo, asg = ctx
    v = _stability_at(fab, geo, asg, 1.15)
    assert not v.sustainable
    assert v.throughput_ratio < 0.97
    assert "완료율" in v.reason


def test_throughput_ratio_is_the_discriminator(ctx) -> None:
    """완료율이 부하에 따라 단조롭게 갈린다 — 판정 기준으로 쓸 수 있는 근거.

    WIP 기울기·드리프트는 부하 85%(명백히 지속 가능)에서도 크게 나와 판별력이 없다.
    그래서 완료율을 1차 기준으로 삼았다.
    """
    fab, geo, asg = ctx
    lo = _stability_at(fab, geo, asg, 0.70)
    hi = _stability_at(fab, geo, asg, 1.20)
    assert lo.throughput_ratio > 0.99
    assert hi.throughput_ratio < 0.85
    assert lo.throughput_ratio - hi.throughput_ratio > 0.10


def test_wip_drift_is_scale_normalized() -> None:
    flat = [(float(d), 40.0) for d in range(60)]
    rising = [(float(d), 10.0 + 2.0 * d) for d in range(60)]
    assert abs(wip_drift(flat, 0.0)) < 1e-9
    assert wip_drift(rising, 0.0) > 0.3


def test_capacity_search_lands_below_analytic_bound(ctx) -> None:
    """지속 가능 최대는 해석적 상한 아래여야 한다 (상한은 대기를 무시한 낙관값)."""
    fab, geo, asg = ctx
    cfg = SimConfig(release_lots_per_day=1.0, warmup_days=50, run_days=120)
    res = find_capacity(fab, cfg, asg, geo, ramp=(0.70, 0.85, 0.95), bisection_rounds=2)
    assert 0 < res.capacity_lots_per_day <= res.analytic_bound
    assert res.first_failure_lots_per_day > res.capacity_lots_per_day
    assert 0.6 <= res.utilization_of_bound <= 1.0
    assert len(res.probes) >= 3


# =========================================================================
# 목표 함수
# =========================================================================


def test_baseline_is_feasible(obj, ctx) -> None:
    _, _, asg = ctx
    ev = obj.evaluate(obj.baseline_candidate(asg))
    assert ev.feasible, f"{ev.reject.value}: {ev.detail}"
    assert ev.cycle_hours > 0
    assert ev.throughput_ratio >= 0.97


def test_functional_beats_spread(obj, ctx) -> None:
    """기능별 배치가 흩뿌림보다 유의하게 낫다 — 목표 함수가 배치를 구별한다는 증거."""
    fab, geo, asg = ctx
    base = obj.baseline_candidate(asg)
    good = obj.evaluate(base)
    poor = obj.evaluate(Candidate(baseline.spread_layout(fab, geo),
                                  base.counts, base.vehicles))
    assert good.feasible and poor.feasible
    assert good.significantly_better_than(poor)


def test_paired_comparison_is_tighter_than_independent(obj, ctx) -> None:
    """쌍대 비교가 독립 신뢰구간 비교보다 정밀해야 한다.

    같은 시드로 평가했으므로 시드가 만든 변동이 상쇄된다. 이 이득이 없으면
    잡음에 묻혀 개선을 판별할 수 없다.
    """
    fab, geo, asg = ctx
    base = obj.baseline_candidate(asg)
    a = obj.evaluate(base)
    b = obj.evaluate(Candidate(baseline.spread_layout(fab, geo),
                               base.counts, base.vehicles))
    _, paired_ci = b.paired_delta(a)
    independent_ci = a.cycle_hours_ci + b.cycle_hours_ci
    assert paired_ci < independent_ci


def test_budget_violation_rejected_without_simulation(obj, ctx) -> None:
    """예산 초과는 DES를 돌리기 전에 걸러야 한다 (평가 비용 절약)."""
    _, _, asg = ctx
    counts = dict(obj.fab.tool_counts)
    counts["PHOTO"] += 2
    ev = obj.evaluate(Candidate(asg, counts, obj.fab.transport.vehicles))
    assert not ev.feasible
    assert ev.reject is Reject.BUDGET
    assert ev.runs == ()


def test_analytic_shortfall_rejected_without_simulation(obj, ctx) -> None:
    """해석적 상한이 목표에 못 미치면 확실히 불가능하므로 즉시 기각."""
    _, _, asg = ctx
    counts = dict(obj.fab.tool_counts)
    counts["PHOTO"] = 1          # 상한이 목표 아래로 떨어진다
    counts["IMPL_HC"] = 1        # 예산은 맞춘다
    counts["IMPL_MC"] = 1
    ev = obj.evaluate(Candidate(asg, counts, obj.fab.transport.vehicles))
    assert not ev.feasible
    assert ev.reject is Reject.ANALYTIC
    assert ev.runs == ()


def test_slot_overflow_rejected(obj, ctx) -> None:
    _, _, asg = ctx
    counts = {gid: 6 for gid in obj.fab.groups}
    ev = obj.evaluate(Candidate(asg, counts, 2))
    assert not ev.feasible
    assert ev.reject in (Reject.SLOTS, Reject.BUDGET)


def test_infeasible_scores_worse_than_any_feasible(obj, ctx) -> None:
    """실행 불가능한 후보는 목적 함수가 무한대라 비교에서 자동으로 밀린다."""
    _, _, asg = ctx
    good = obj.evaluate(obj.baseline_candidate(asg))
    counts = dict(obj.fab.tool_counts)
    counts["PHOTO"] += 2
    bad = obj.evaluate(Candidate(asg, counts, 2))
    assert bad.score == float("inf")
    assert good.better_than(bad)
    assert good.significantly_better_than(bad)


def test_evaluation_cost_is_one_point(obj, ctx) -> None:
    """후보 평가는 목표 처리량 한 지점에서 반복 횟수만큼만 시뮬레이션한다.

    목표 함수를 "처리량 최대화"에서 "목표 처리량 제약 + 사이클타임 최소화"로 바꾼
    부수 효과다. 원래 설계는 후보마다 생산능력 탐색 7회 × 반복 3회 = 21회였다.
    """
    _, _, asg = ctx
    ev = obj.evaluate(obj.baseline_candidate(asg))
    assert len(ev.runs) == obj.replications
    rates = {r.window_minutes for r in ev.runs}
    assert len(rates) == 1


def test_vehicle_count_enters_capex(obj, ctx) -> None:
    _, _, asg = ctx
    base = obj.baseline_candidate(asg)
    more = Candidate(asg, base.counts, base.vehicles + 4)
    assert obj.budget.spend(more.counts, more.vehicles) > \
        obj.budget.spend(base.counts, base.vehicles)
    assert obj.evaluate(more).reject is Reject.BUDGET


# =========================================================================
# CLI
# =========================================================================


def test_cli_compare_runs() -> None:
    from fablayout.cli import main

    rc = main(["--run-days", "40", "--warmup-days", "20", "--reps", "2", "compare"])
    assert rc == 0


def test_cli_evaluate_runs() -> None:
    from fablayout.cli import main

    # 워밍업은 사이클타임의 4배 이상이어야 한다 (`check_stability` 참조)
    rc = main(["--run-days", "80", "--warmup-days", "60", "--reps", "2",
               "--target", "0.7", "evaluate"])
    assert rc == 0


def test_short_warmup_is_flagged(ctx) -> None:
    """워밍업이 짧으면 지속 가능한 후보도 기각되고, 그 사실이 판정에 표시되어야 한다.

    최적화기 안에서 이런 오판이 나면 좋은 후보를 무작위로 버리게 된다.
    """
    fab, geo, asg = ctx
    bound = analytic_capacity(fab).capacity_lots_per_day
    rate = bound * 0.70
    cfg = SimConfig(release_lots_per_day=rate, warmup_days=15, run_days=40,
                    collect_wip_series=True, wip_sample_minutes=1440.0)
    r = simulate(fab, cfg, asg, geo)
    v = check_stability(r, rate, cfg.warmup_days, cfg.run_days)
    assert not v.warmup_adequate
    assert v.warmup_cycles < 4.0
    if not v.sustainable:
        assert "워밍업" in v.reason


def test_cli_simulate_runs() -> None:
    from fablayout.cli import main

    assert main(["--run-days", "30", "--warmup-days", "10", "simulate",
                 "--rate", "4.0"]) == 0
