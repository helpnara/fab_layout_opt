"""대기행렬 이론(M/G/1)과의 대조.

엔진이 "돌아간다"와 "맞다"는 다르다. 큐 대기시간이 이론값과 맞는지 확인하는 것이
가장 직접적인 정확성 검증이다.

**Pollaczek–Khinchine 공식** — 포아송 도착(M), 임의 분포 서비스(G), 서버 1대(1):

    Wq = λ·E[S²] / (2·(1 − ρ)),   ρ = λ·E[S]

서비스 분포가 무엇이든 성립하므로 **로그정규 처리시간 구현을 그대로 검증**한다.
지수분포로 바꿔 M/M/1을 쓰면 실제 코드 경로가 아닌 것을 재는 셈이 된다.

E[S²] = (cv² + 1)·E[S]² 이므로, cv가 커질수록 같은 가동률에서도 대기가 급증한다 —
"변동이 대기를 만든다"는 이 관계가 배치 최적화의 배경이기도 하다.
"""

from __future__ import annotations

import pytest
from conftest import m2_config, single_server_fab

from fablayout.sim import simulate
from fablayout.sim.rng import lognormal_second_moment


def pk_waiting_minutes(lam_per_min: float, mean_s: float, cv: float) -> float:
    """Pollaczek–Khinchine 큐 대기시간."""
    rho = lam_per_min * mean_s
    if rho >= 1.0:
        raise ValueError("ρ < 1 이어야 정상 상태가 존재한다")
    return lam_per_min * lognormal_second_moment(mean_s, cv) / (2.0 * (1.0 - rho))


def _measure_wait(mean_s: float, cv: float, rho: float, days: float, seed: int) -> float:
    lam = rho / mean_s                       # lot/분
    fab = single_server_fab(mean_s, cv)
    r = simulate(fab, m2_config(
        release_lots_per_day=lam * 1440.0,
        warmup_days=days * 0.15,
        run_days=days,
        seed=seed,
        arrival="poisson",
    ))
    return r.groups["S"].mean_queue_wait


@pytest.mark.parametrize(
    "cv, rho, tol",
    [
        (0.15, 0.70, 0.15),   # 데이터셋 기본 변동
        (0.50, 0.70, 0.15),
        (1.00, 0.60, 0.15),   # 지수분포와 같은 변동계수
        (0.30, 0.85, 0.20),   # 고부하 — 수렴이 느려 허용오차를 넓힌다
    ],
)
def test_queue_wait_matches_pollaczek_khinchine(cv: float, rho: float, tol: float) -> None:
    mean_s = 60.0
    expected = pk_waiting_minutes(rho / mean_s, mean_s, cv)
    # 독립 시드 평균으로 표본오차를 줄인다
    seeds = (11, 22, 33, 44, 55)
    measured = sum(_measure_wait(mean_s, cv, rho, 900.0, s) for s in seeds) / len(seeds)
    assert measured == pytest.approx(expected, rel=tol), (
        f"cv={cv} ρ={rho}: 실측 {measured:.1f}분 vs 이론 {expected:.1f}분 "
        f"({measured / expected - 1:+.1%})"
    )


def test_waiting_scales_with_service_variability() -> None:
    """같은 가동률에서 대기시간 비율 = E[S²] 비율이어야 한다.

    P–K에서 λ와 ρ가 같으면 Wq는 E[S²]에만 비례한다. cv 0.15 → 1.00이면
    (1+1²)/(1+0.15²) = 1.96배가 이론값이다. "변동이 대기를 만든다"를
    부등식이 아니라 배수로 확인한다.
    """
    lo = _measure_wait(60.0, 0.15, 0.7, 600.0, seed=7)
    hi = _measure_wait(60.0, 1.00, 0.7, 600.0, seed=7)
    theoretical = lognormal_second_moment(60.0, 1.00) / lognormal_second_moment(60.0, 0.15)
    assert theoretical == pytest.approx(1.956, rel=1e-3)
    assert hi / lo == pytest.approx(theoretical, rel=0.15), (
        f"실측 {hi / lo:.2f}배 vs 이론 {theoretical:.2f}배"
    )


def test_utilization_matches_rho() -> None:
    """실측 가동률이 이론 ρ = λ·E[S]와 맞아야 한다."""
    mean_s, rho = 60.0, 0.75
    fab = single_server_fab(mean_s, cv=0.3)
    r = simulate(fab, m2_config(
        release_lots_per_day=(rho / mean_s) * 1440.0,
        warmup_days=60, run_days=600, seed=5,
    ))
    assert r.utilizations()["S"] == pytest.approx(rho, rel=0.03)


def test_littles_law() -> None:
    """리틀의 법칙: WIP = 처리량 × 사이클타임. 지표 집계의 내적 정합성 검증."""
    mean_s, rho = 60.0, 0.7
    fab = single_server_fab(mean_s, cv=0.4)
    r = simulate(fab, m2_config(
        release_lots_per_day=(rho / mean_s) * 1440.0,
        warmup_days=60, run_days=900, seed=3,
    ))
    lam_per_min = r.throughput_lots_per_day / 1440.0
    expected_wip = lam_per_min * (r.mean_cycle_hours * 60.0)
    assert r.mean_wip == pytest.approx(expected_wip, rel=0.10), (
        f"WIP {r.mean_wip:.2f} vs λ·CT {expected_wip:.2f}"
    )


def test_lognormal_draw_has_requested_mean_and_cv() -> None:
    """로그정규 파라미터 환산이 맞는지 — 틀리면 모든 처리시간이 편향된다."""
    import random
    from statistics import mean, stdev

    from fablayout.sim.rng import draw_process_times

    rng = random.Random(0)
    xs = draw_process_times(rng, (40.0,) * 40000, (0.35,) * 40000)
    assert mean(xs) == pytest.approx(40.0, rel=0.02)
    assert stdev(xs) / mean(xs) == pytest.approx(0.35, rel=0.05)
