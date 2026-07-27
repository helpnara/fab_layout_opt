"""SimPy 교차검증 — 자체 엔진의 논리 오류를 잡는 가장 확실한 수단.

같은 모델을 검증된 외부 라이브러리로 다시 짜서 결과가 일치하는지 본다. 이론값 대조
(`test_sim_mg1.py`)는 단일 설비 단일 스텝까지만 가능하므로, **다단계 라우트·복수
설비·재진입**이 있는 모델은 이 방법으로만 확인할 수 있다.

핵심 설계: **양쪽을 완전 결정론으로 만든다.** 등간격 투입 + 처리시간 변동 0이면
두 시뮬레이터는 같은 사건 열을 만들어야 하고, 통계적 비교가 아니라 lot 단위 완료
시각까지 일치해야 한다. 확률적으로 비교하면 표본오차에 가려 미묘한 로직 오류를
놓친다.

동시각 이벤트의 타이 처리는 두 엔진이 다를 수 있으므로 처리시간을 서로소인 소수로
잡아 정확한 동률이 생기지 않게 한다.
"""

from __future__ import annotations

import pytest
from conftest import make_fab

from fablayout.sim import SimConfig, simulate

simpy = pytest.importorskip("simpy", reason="SimPy는 개발 의존성이다")


# 서로소 소수 — 동시각 타이를 피한다
GROUPS = [("A", 2, 13.0), ("B", 1, 17.0), ("C", 2, 23.0)]
ROUTE = ["A", "B", "C", "A", "B"]          # A와 B를 재방문 (재진입)
GAP = 11.0                                  # 등간격 투입 (분)
N_LOTS = 400


def _simpy_completion_times(
    groups: list[tuple[str, int, float]],
    route: list[str],
    gap: float,
    n_lots: int,
) -> list[float]:
    """같은 모델을 SimPy로. FIFO, 결정론적 처리시간, 등간격 투입."""
    env = simpy.Environment()
    caps = {gid: simpy.Resource(env, capacity=n) for gid, n, _ in groups}
    minutes = {gid: m for gid, _, m in groups}
    done: list[float] = []

    def lot_proc(env):
        for gid in route:
            with caps[gid].request() as req:
                yield req
                yield env.timeout(minutes[gid])
        done.append(env.now)

    def source(env):
        for _ in range(n_lots):
            env.process(lot_proc(env))
            yield env.timeout(gap)

    env.process(source(env))
    env.run()
    return sorted(done)


def _own_completion_times(
    groups: list[tuple[str, int, float]],
    route: list[str],
    gap: float,
    n_lots: int,
) -> list[float]:
    """자체 엔진에서 lot별 완료 시각을 뽑는다."""
    fab = make_fab(groups, route, cv=0.0)
    cfg = SimConfig(
        release_lots_per_day=1440.0 / gap,
        warmup_days=0.0,
        run_days=10_000.0,
        arrival="deterministic",
        max_lots=n_lots,
    )
    from fablayout.sim.runner import Simulation

    sim = Simulation(fab, cfg)
    finished: list[float] = []
    original = sim._finish_step

    def spy(payload):
        lot = payload[2]
        original(payload)
        if lot.done:
            finished.append(lot.done_time)

    sim._finish_step = spy  # type: ignore[method-assign]
    sim.run()
    return sorted(finished)


def test_completion_times_match_simpy_exactly() -> None:
    """결정론 모델에서 lot별 완료 시각이 SimPy와 일치해야 한다."""
    mine = _own_completion_times(GROUPS, ROUTE, GAP, N_LOTS)
    theirs = _simpy_completion_times(GROUPS, ROUTE, GAP, N_LOTS)
    assert len(mine) == len(theirs) == N_LOTS
    for i, (a, b) in enumerate(zip(mine, theirs)):
        assert a == pytest.approx(b, abs=1e-6), f"lot {i}: 자체 {a} vs SimPy {b}"


def test_matches_simpy_under_congestion() -> None:
    """병목이 심한 조건에서도 일치하는지 — 큐 관리 로직을 직접 겨냥한다."""
    groups = [("A", 1, 29.0), ("B", 3, 7.0)]
    route = ["A", "B", "A", "B", "A"]
    gap = 97.0                                   # A 3회 방문 × 29분 = 87분 < 97분
    mine = _own_completion_times(groups, route, gap, 300)
    theirs = _simpy_completion_times(groups, route, gap, 300)
    for a, b in zip(mine, theirs):
        assert a == pytest.approx(b, abs=1e-6)


def test_matches_simpy_when_saturated() -> None:
    """능력 초과 투입 — 큐가 길게 쌓이는 상태에서의 일치."""
    groups = [("A", 1, 31.0), ("B", 2, 19.0)]
    route = ["A", "B", "A"]
    gap = 41.0                                   # A 2회 × 31 = 62분 > 41분 → 포화
    mine = _own_completion_times(groups, route, gap, 200)
    theirs = _simpy_completion_times(groups, route, gap, 200)
    for a, b in zip(mine, theirs):
        assert a == pytest.approx(b, abs=1e-6)


def test_aggregate_metrics_match_simpy() -> None:
    """집계 지표(사이클타임·처리량) 수준에서도 일치하는지."""
    theirs = _simpy_completion_times(GROUPS, ROUTE, GAP, N_LOTS)
    releases = [i * GAP for i in range(N_LOTS)]
    simpy_mean_ct = sum(d - r for d, r in zip(theirs, releases)) / N_LOTS

    fab = make_fab(GROUPS, ROUTE, cv=0.0)
    r = simulate(fab, SimConfig(
        release_lots_per_day=1440.0 / GAP,
        warmup_days=0.0,
        run_days=10_000.0,
        arrival="deterministic",
        max_lots=N_LOTS,
    ))
    assert r.completed == N_LOTS
    assert r.mean_cycle_hours * 60.0 == pytest.approx(simpy_mean_ct, rel=1e-6)
