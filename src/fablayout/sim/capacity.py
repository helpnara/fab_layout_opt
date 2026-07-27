"""지속 가능한 최대 처리량 탐색.

**과투입으로 능력을 재면 안 된다.** M3에서 확인했듯, 상한의 130%를 투입하면 처리량이
오히려 낮게 측정된다 — WIP가 발산하며 사이클타임이 수십 일로 늘어나 관측 창 안에
완료되지 못한 lot이 쌓이기 때문이다. 실제로는 94% 투입에서 더 높은 처리량을 지속했다.

그래서 **아래에서 접근한다**: 투입률을 올려가며 "지속 가능한가"를 판정하고, 실패하기
직전 지점을 이분 탐색으로 좁힌다.

지속 가능 판정 — **완료율이 사실상 유일한 판별자다**

    1. 완료율   처리량 ≥ 투입률 × (표본 크기로 보정한 허용치)
       실측 분포가 깨끗하게 갈린다: 부하 60~97%에서 0.995~1.023, 105%에서 0.890,
       120%에서 0.685. 발산하면 나가는 양이 들어오는 양을 못 따라간다.
    2. WIP 드리프트 — **총체적 발산만** 잡는 느슨한 보조 장치 (기본 50%)
    3. 사이클타임 상한 (선택)

**허용치는 표본 크기에 따라 달라져야 한다.** 완료 수가 적으면 완료율 자체가 크게
흔들려, 고정 임계는 지속 가능한 후보를 오기각한다. 부하 70%(명백히 지속 가능)에서
실측한 완료율 최솟값은 이렇게 움직였다.

    관측 창 40일  (완료 174개)   최소 0.887
    관측 창 80일  (완료 357개)   최소 0.918
    관측 창 180일 (완료 788개)   최소 0.958
    관측 창 360일 (완료 1596개)  최소 0.971

편차가 대략 1/√N에 비례하므로 허용치를 `max(고정치, 2/√N)`로 잡는다. 이렇게 해도
부하 105%(완료율 0.890)·120%(0.685)는 여전히 걸러진다.

WIP 추세로 정밀 판정하려던 시도는 버렸다. 처음엔 회귀 기울기의 부호로, 다음엔 기울기의
t검정으로 판정하려 했으나 둘 다 판별력이 없었다 — 부하 85%(완료율 1.018로 명백히 지속
가능)에서 이미 t=5.6, 구간 드리프트 9.5%가 나온다. 이유는 두 가지다.

    (a) 포화 근처에서는 정상 상태 도달이 매우 느려, 300일 관측으로도 "느린 정착"과
        "느린 발산"이 구분되지 않는다.
    (b) 일 단위 WIP 표본은 자기상관이 강해 회귀 표준오차가 크게 과소평가된다.
        t값이 커 보이는 것은 신뢰의 근거가 아니다.

목표 함수가 "목표 처리량을 만족하는가"를 묻는 이상, 답할 수 있는 질문은 "그 투입률을
관측 기간 동안 실제로 소화했는가"다. 완료율이 바로 그것을 측정한다.

목표 함수가 "예산 제약 하에서 목표 처리량을 만족하며 사이클타임 최소화"로 정해진 뒤,
이 탐색은 **최적화 내부 루프에서 빠졌다**. 후보 평가는 목표 처리량 한 지점에서만
시뮬레이션하면 되기 때문이다(`opt/objective.py`). 여기서는 시나리오 진단과 목표
처리량 설정에 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from ..core.model import Assignment, Fab
from ..core.geometry import LayoutGeometry
from .metrics import SimResult
from .runner import SimConfig, simulate


@dataclass(frozen=True)
class StabilityVerdict:
    sustainable: bool
    throughput_ratio: float
    """달성 처리량 ÷ 투입률. 1에 가까우면 들어온 만큼 나간다."""
    wip_slope_per_day: float
    wip_drift: float
    """관측 창 후반 3분위 평균 − 2분위 평균, WIP 평균으로 정규화. 총체적 발산 감지용."""
    cycle_hours: float
    warmup_cycles: float = 0.0
    """워밍업 길이 ÷ 사이클타임. 정상 상태 도달 여부의 척도."""
    warmup_adequate: bool = True
    reason: str = ""


def wip_slope(series: list[tuple[float, float]], from_day: float) -> float:
    """WIP 시계열 후반 구간의 회귀 기울기 (개/일). 보고용 참고값."""
    pts = [(t, w) for t, w in series if t >= from_day]
    n = len(pts)
    if n < 8:
        return 0.0
    mx = sum(t for t, _ in pts) / n
    my = sum(w for _, w in pts) / n
    sxx = sum((t - mx) ** 2 for t, _ in pts)
    if sxx <= 0:
        return 0.0
    return sum((t - mx) * (w - my) for t, w in pts) / sxx


def wip_drift(series: list[tuple[float, float]], from_day: float) -> float:
    """관측 구간을 3등분해 (3분위 평균 − 2분위 평균) ÷ 전체 평균.

    회귀 기울기와 달리 규모에 대해 정규화되어 있어 임계값을 정하기 쉽다. 다만 판별력이
    약하므로 총체적 발산(50% 이상)만 걸러내는 데 쓴다.
    """
    pts = [w for t, w in series if t >= from_day]
    n = len(pts)
    if n < 9:
        return 0.0
    mean = sum(pts) / n
    if mean <= 0:
        return 0.0
    mid = sum(pts[n // 3:2 * n // 3]) / (2 * n // 3 - n // 3)
    late = sum(pts[2 * n // 3:]) / (n - 2 * n // 3)
    return (late - mid) / mean


def check_stability(
    result: SimResult,
    release_lots_per_day: float,
    warmup_days: float,
    run_days: float,
    throughput_tolerance: float = 0.97,
    cycle_time_cap_hours: float | None = None,
    drift_limit: float = 0.50,
    min_warmup_cycles: float = 4.0,
    noise_factor: float = 2.0,
) -> StabilityVerdict:
    """한 번의 실행 결과가 그 투입률을 지속 가능한지 판정한다.

    **워밍업이 짧으면 지속 가능한 후보도 불가능으로 오판된다.** WIP가 0에서 출발해
    정상 상태까지 차오르는 동안에는 완료가 투입을 못 따라가므로 완료율이 낮게 나온다.
    실측 사례: 워밍업 20일 · 사이클타임 6일(=3.3 사이클)에서 완료율 0.944로 기각됐으나,
    같은 구성이 워밍업 60일에서는 1.017로 통과했다.

    최적화기 안에서 이런 오판이 나면 좋은 후보를 무작위로 버리게 되므로, 워밍업이
    사이클타임의 `min_warmup_cycles`배에 못 미치면 판정에 경고를 붙인다.

    `throughput_tolerance`는 **하한**이다. 실제 허용치는 완료 표본 수 N에 맞춰
    `max(1 − throughput_tolerance, noise_factor/√N)`만큼 느슨해진다.
    """
    from math import sqrt

    ratio = result.throughput_lots_per_day / release_lots_per_day
    n_done = max(result.completions_in_window, 1)
    slack = max(1.0 - throughput_tolerance, noise_factor / sqrt(n_done))
    effective_tolerance = 1.0 - slack
    from_day = warmup_days
    slope = wip_slope(result.wip_series, from_day + run_days * 0.5)
    drift = wip_drift(result.wip_series, from_day)
    ct = result.mean_cycle_hours
    cycles = (warmup_days * 24.0 / ct) if ct > 0 else 0.0
    adequate = cycles >= min_warmup_cycles

    reasons = []
    if ratio < effective_tolerance:
        why = f"완료율 {ratio:.3f} < {effective_tolerance:.3f} (표본 {n_done}개)"
        if not adequate:
            why += (f" (워밍업이 사이클타임의 {cycles:.1f}배뿐 — "
                    f"{min_warmup_cycles:.0f}배 이상 권장. 정상 상태 미도달일 수 있다)")
        reasons.append(why)
    if drift > drift_limit:
        reasons.append(f"WIP 발산 (드리프트 {drift:+.0%})")
    if cycle_time_cap_hours is not None and ct > cycle_time_cap_hours:
        reasons.append(f"사이클타임 {ct:.0f}h > 상한 {cycle_time_cap_hours:.0f}h")

    return StabilityVerdict(
        sustainable=not reasons,
        throughput_ratio=ratio,
        wip_slope_per_day=slope,
        wip_drift=drift,
        cycle_hours=ct,
        warmup_cycles=cycles,
        warmup_adequate=adequate,
        reason=" · ".join(reasons),
    )


@dataclass(frozen=True)
class CapacityResult:
    capacity_lots_per_day: float
    """지속 가능한 최대 투입률 (이분 탐색 하한)."""
    first_failure_lots_per_day: float
    analytic_bound: float
    cycle_hours_at_capacity: float
    probes: tuple[tuple[float, StabilityVerdict], ...]
    """시도한 (투입률, 판정) 목록. 진단용."""

    @property
    def capacity_wafers_per_day(self) -> float:
        return self.capacity_lots_per_day * 25

    @property
    def utilization_of_bound(self) -> float:
        return self.capacity_lots_per_day / self.analytic_bound if self.analytic_bound else 0.0


def find_capacity(
    fab: Fab,
    base_config: SimConfig,
    assignment: Assignment | None = None,
    geo: LayoutGeometry | None = None,
    analytic_bound: float | None = None,
    ramp: tuple[float, ...] = (0.70, 0.82, 0.90, 0.95),
    bisection_rounds: int = 3,
    cycle_time_cap_hours: float | None = None,
) -> CapacityResult:
    """조대 램프 + 이분 탐색으로 지속 가능한 최대 처리량을 찾는다.

    DES 실행 횟수 = len(ramp) + bisection_rounds (기본 7회).
    """
    from ..core.analysis import analytic_capacity

    bound = analytic_bound or analytic_capacity(fab).capacity_lots_per_day
    probes: list[tuple[float, StabilityVerdict]] = []

    def probe(rate: float) -> StabilityVerdict:
        cfg = replace(
            base_config,
            release_lots_per_day=rate,
            collect_wip_series=True,
            wip_sample_minutes=1440.0,
        )
        r = simulate(fab, cfg, assignment, geo)
        v = check_stability(
            r, rate, cfg.warmup_days, cfg.run_days,
            cycle_time_cap_hours=cycle_time_cap_hours,
        )
        probes.append((rate, v))
        return v

    lo = 0.0
    lo_cycle = 0.0
    hi = bound * ramp[-1] * 1.15
    for frac in ramp:
        rate = bound * frac
        v = probe(rate)
        if v.sustainable:
            lo, lo_cycle = rate, v.cycle_hours
        else:
            hi = rate
            break

    for _ in range(bisection_rounds):
        if hi - lo < bound * 0.005:
            break
        mid = (lo + hi) / 2
        v = probe(mid)
        if v.sustainable:
            lo, lo_cycle = mid, v.cycle_hours
        else:
            hi = mid

    return CapacityResult(
        capacity_lots_per_day=lo,
        first_failure_lots_per_day=hi,
        analytic_bound=bound,
        cycle_hours_at_capacity=lo_cycle,
        probes=tuple(probes),
    )
