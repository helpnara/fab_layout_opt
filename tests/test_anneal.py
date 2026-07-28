"""대리지표 담금질과 M5 게이트 검증."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fablayout.core.analysis import flow_report
from fablayout.data.midfab import build_midfab, midfab_geometry
from fablayout.opt import baseline
from fablayout.opt.anneal import anneal
from fablayout.opt.moves import BayPlan
from fablayout.opt.surrogate import Surrogate


@pytest.fixture(scope="module")
def ctx():
    fab, geo = build_midfab(), midfab_geometry()
    return fab, geo, Surrogate.build(fab, geo)


def test_anneal_improves_a_bad_start(ctx) -> None:
    """흩뿌린 배치에서 출발하면 크게 좋아져야 한다 — 탐색이 실제로 작동하는지."""
    fab, geo, sur = ctx
    res = anneal(sur, geo, baseline.spread_layout(fab, geo), steps=600, seed=1)
    assert res.score < res.start_score
    assert res.gain > 0.5, f"개선 {res.gain:.1%}"


def test_anneal_never_returns_worse_than_start(ctx) -> None:
    """최선값을 따로 들고 다니므로 시작보다 나빠질 수 없다."""
    fab, geo, sur = ctx
    for seed in (0, 3, 9):
        res = anneal(sur, geo, baseline.family_layout(fab, geo), steps=200, seed=seed)
        assert res.score <= res.start_score + 1e-9
        assert res.history[-1] == pytest.approx(res.score)


def test_anneal_is_deterministic(ctx) -> None:
    """대리지표가 결정론적이므로 담금질도 시드가 같으면 같은 해가 나와야 한다.

    이것이 DES 위에서 직접 담금질하지 않는 이유다 — 잡음이 있으면 재현되지 않는다.
    """
    fab, geo, sur = ctx
    start = baseline.family_layout(fab, geo)
    a = anneal(sur, geo, start, steps=300, seed=7)
    b = anneal(sur, geo, start, steps=300, seed=7)
    assert a.score == b.score
    assert a.assignment.placement.keys() == b.assignment.placement.keys()
    assert all(a.assignment.placement[k] == b.assignment.placement[k]
               for k in a.assignment.placement)


def test_anneal_result_is_a_valid_layout(ctx) -> None:
    """모든 설비가 배치되고 slot이 겹치지 않아야 한다."""
    fab, geo, sur = ctx
    res = anneal(sur, geo, baseline.spread_layout(fab, geo), steps=500, seed=2)
    assert len(res.assignment.placement) == fab.total_tools
    assert len(res.assignment.occupied()) == fab.total_tools
    plan = BayPlan.from_assignment(res.assignment, geo)
    for bay, cap in plan.capacity.items():
        assert plan.used(bay) <= cap


def test_anneal_reduces_bay_crossings(ctx) -> None:
    """대리지표를 낮추면 실제 bay 교차도 줄어야 한다 — 지표가 무엇을 재는지 확인."""
    fab, geo, sur = ctx
    start = baseline.family_layout(fab, geo)
    res = anneal(sur, geo, start, steps=2000, seed=4)
    before = flow_report(fab, geo, start)
    after = flow_report(fab, geo, res.assignment)
    assert after.interbay_moves_per_lot < before.interbay_moves_per_lot


def test_restarts_are_at_least_as_good(ctx) -> None:
    fab, geo, sur = ctx
    start = baseline.spread_layout(fab, geo)
    one = anneal(sur, geo, start, steps=400, seed=6, restarts=1)
    two = anneal(sur, geo, start, steps=400, seed=6, restarts=2)
    assert two.score <= one.score + 1e-9
    assert two.steps == 2 * one.steps


# =========================================================================
# M5 게이트 — `scripts/calibrate_surrogate.py`가 남긴 결과를 검사한다
# =========================================================================

CALIBRATION = Path(__file__).resolve().parents[1] / "results" / "surrogate_calibration.json"


@pytest.fixture(scope="module")
def calibration():
    if not CALIBRATION.exists():
        pytest.skip("보정 결과 없음 — scripts/calibrate_surrogate.py를 먼저 실행할 것")
    return json.loads(CALIBRATION.read_text(encoding="utf-8"))


def test_gate_sample_covers_a_wide_quality_range(calibration) -> None:
    """좋은 배치들만 모으면 순위상관은 잡음만 잰다. 표본이 넓어야 판정이 성립한다."""
    hours = [r["cycle_hours"] for r in calibration["rows"]]
    assert len(hours) >= 12
    assert (max(hours) - min(hours)) / min(hours) > 0.15


def test_rank_correlation_misses_the_stated_gate(calibration) -> None:
    """게이트 기준 ρ ≥ 0.7을 넘지 못한다 — 이것이 실측이다.

    통과했다고 적어두면 M6이 잘못된 전제 위에 서게 되므로, 미달을 **명시적으로**
    고정해 둔다. 왜 미달인지는 다음 두 테스트가 말한다.
    """
    assert calibration["cv_rho"] < calibration["gate"], (
        f"교차검증 ρ={calibration['cv_rho']:.3f} — 게이트를 넘었다면 "
        f"결론(§26)을 다시 써야 한다"
    )


def test_most_pairs_are_beyond_des_resolution(calibration) -> None:
    """ρ가 낮은 이유. 표본 쌍의 대부분은 **DES 자신도** 구분하지 못한다.

    순위상관은 사이클타임 차이가 신뢰구간 안에 있는 쌍까지 채점하는데, 그 쌍의
    "정답"은 잡음이다. 즉 ρ는 대리지표의 성능이 아니라 DES의 분해능을 재고 있다.
    """
    assert calibration["resolvable_pairs"] < calibration["all_pairs"] * 0.5


def test_surrogate_orders_every_resolvable_pair(calibration) -> None:
    """구분 가능한 쌍에서는 대리지표가 순서를 맞힌다 — 필터로는 쓸 수 있다는 근거.

    이 값이 무너지면 대리지표를 걸러내는 용도로도 쓸 수 없고, M6에서 배치를 다루는
    방식 자체를 바꿔야 한다.
    """
    assert calibration["resolvable_accuracy"] >= 0.95, (
        f"{calibration['resolvable_accuracy']:.1%} "
        f"({calibration['resolvable_pairs']}쌍)"
    )


def test_stocker_term_is_the_weakest_predictor(calibration) -> None:
    """설계서(§7.2)는 스토커를 지배항으로 예상했다. 실측은 반대다.

    설계 예상이 틀렸다는 사실을 테스트로 고정해, 나중에 대리지표를 손볼 때 잘못된
    근거로 되돌아가지 않게 한다.
    """
    rho = calibration["term_rho"]
    assert rho["stocker"] == min(rho.values()), rho
    assert rho["split"] > rho["stocker"] * 2


def test_resolution_within_good_layouts_is_limited(calibration) -> None:
    """좋은 배치들 사이의 폭은 신뢰구간 수준이다 — M6의 설계를 정하는 사실.

    배치 미세조정은 DES로 검증할 수 없으므로, 대수 구성을 주 결정 변수로 두고
    배치는 나쁜 해를 피하는 수준으로 다룬다.
    """
    assert calibration["good_span_hours"] < 4 * calibration["good_ci"]
    assert calibration["good_resolvable_pairs"] < calibration["good_all_pairs"] * 0.2
