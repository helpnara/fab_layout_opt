"""지표 집계.

**관측 창(observation window)**: 워밍업 구간의 통계는 버린다. WIP=0에서 출발한 초기
구간은 큐가 비어 있어 사이클타임이 비현실적으로 짧게 나오기 때문이다.

사이클타임은 **투입과 완료가 모두 창 안에 있는 lot**만 집계한다. 완료 시각만으로
거르면 워밍업 중에 투입된(=큐가 짧았던) lot이 섞여 낙관 편향이 남고, 투입 시각만으로
거르면 창 끝에서 아직 끝나지 않은 긴 lot이 빠져 역시 낙관 편향이 생긴다. 후자는
완전히 없앨 수 없으므로(검열, censoring) 미완료 lot 수를 함께 보고한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt


class TimeIntegral:
    """계단 함수의 시간 가중 적분. 관측 창 밖은 잘라낸다.

    WIP·큐 길이처럼 "값이 이벤트 때만 바뀌는" 양의 평균을 정확히 구한다.
    """

    __slots__ = ("value", "area", "last_t", "w0", "w1", "peak")

    def __init__(self, w0: float, w1: float, initial: float = 0.0) -> None:
        self.w0 = w0
        self.w1 = w1
        self.value = initial
        self.area = 0.0
        self.last_t = 0.0
        self.peak = initial

    def set(self, t: float, value: float) -> None:
        self._accrue(t)
        self.value = value
        if value > self.peak:
            self.peak = value

    def add(self, t: float, delta: float) -> None:
        self.set(t, self.value + delta)

    def _accrue(self, t: float) -> None:
        lo = self.last_t if self.last_t > self.w0 else self.w0
        hi = t if t < self.w1 else self.w1
        if hi > lo:
            self.area += self.value * (hi - lo)
        self.last_t = t

    def finish(self, t: float) -> None:
        self._accrue(t)

    def mean(self) -> float:
        span = self.w1 - self.w0
        return self.area / span if span > 0 else 0.0


def overlap(a0: float, a1: float, w0: float, w1: float) -> float:
    """구간 [a0,a1]과 관측 창 [w0,w1]의 겹치는 길이."""
    lo = a0 if a0 > w0 else w0
    hi = a1 if a1 < w1 else w1
    return hi - lo if hi > lo else 0.0


@dataclass
class GroupMetrics:
    gid: str
    tools: int
    busy_minutes: float = 0.0
    """관측 창 안의 총 설비 점유 시간 (모든 설비 합)."""
    down_minutes: float = 0.0
    """고장으로 멈춰 있던 시간 합."""
    queue_minutes: float = 0.0
    """관측 창 안에 큐에 들어간 lot들의 대기시간 합."""
    queue_entries: int = 0
    starts: int = 0
    """착수한 lot 수 (배치면 구성원 수 합)."""
    batches: int = 0
    """착수 횟수. 단일 설비면 starts와 같다."""
    partial_batches: int = 0
    """배치가 다 차지 않은 채 타임아웃으로 착수한 횟수."""
    failures: int = 0
    mean_queue_len: float = 0.0
    peak_queue_len: float = 0.0

    def utilization(self, window_minutes: float) -> float:
        """설비 가동률 = 점유시간 / (대수 × 창 길이).

        분모가 달력시간이므로 고장으로 멈춘 시간도 분모에 포함된다. 즉 이 값은
        "설비가 실제로 일한 비율"이고, 고장을 감안한 실효 능력 대비 부하는
        `load_factor`가 나타낸다.
        """
        cap = self.tools * window_minutes
        return self.busy_minutes / cap if cap > 0 else 0.0

    def availability(self, window_minutes: float) -> float:
        """가동 가능했던 시간 비율 = 1 − 고장시간 / (대수 × 창 길이)."""
        cap = self.tools * window_minutes
        return 1.0 - self.down_minutes / cap if cap > 0 else 1.0

    def load_factor(self, window_minutes: float) -> float:
        """가동 가능 시간 대비 점유 비율. 1에 가까우면 실질 병목이다."""
        cap = self.tools * window_minutes - self.down_minutes
        return self.busy_minutes / cap if cap > 0 else 0.0

    @property
    def mean_batch_size(self) -> float:
        return self.starts / self.batches if self.batches else 0.0

    @property
    def mean_queue_wait(self) -> float:
        return self.queue_minutes / self.queue_entries if self.queue_entries else 0.0


@dataclass
class TransportMetrics:
    """반송 시스템 지표."""

    vehicles: int
    busy_minutes: float = 0.0
    wait_minutes: float = 0.0
    """반송차를 기다린 시간 합 (반송차가 모자라 생긴 지연)."""
    moves: int = 0
    queued_moves: int = 0
    """즉시 배정받지 못하고 대기해야 했던 요청 수."""
    stocker_ops: int = 0
    distance_m: float = 0.0

    def utilization(self, window_minutes: float) -> float:
        cap = self.vehicles * window_minutes
        return self.busy_minutes / cap if cap > 0 else 0.0

    @property
    def mean_wait_minutes(self) -> float:
        return self.wait_minutes / self.moves if self.moves else 0.0

    @property
    def queued_fraction(self) -> float:
        return self.queued_moves / self.moves if self.moves else 0.0


@dataclass
class SimResult:
    """시뮬레이션 1회의 결과."""

    window_minutes: float
    warmup_minutes: float
    released: int
    """관측 창 안에 투입된 lot 수."""
    completed: int
    """관측 창 안에 투입되고 완료까지 된 lot 수 (사이클타임 표본)."""
    censored: int
    """창 안에 투입되었으나 끝나지 않은 lot 수. 이 값이 크면 창이 짧다는 뜻이다."""
    completions_in_window: int
    """창 안에 완료된 lot 수 (투입 시각 무관). 처리량의 분자."""
    cycle_times: list[float] = field(default_factory=list)
    raw_process_minutes: list[float] = field(default_factory=list)
    transport_minutes: list[float] = field(default_factory=list)
    groups: dict[str, GroupMetrics] = field(default_factory=dict)
    transport: "TransportMetrics | None" = None
    mean_wip: float = 0.0
    peak_wip: float = 0.0
    wafers_per_lot: int = 25
    events: int = 0
    wall_seconds: float = 0.0
    wip_series: list[tuple[float, float]] = field(default_factory=list)

    # ---- 처리량 ---------------------------------------------------------

    @property
    def throughput_lots_per_day(self) -> float:
        if self.window_minutes <= 0:
            return 0.0
        return self.completions_in_window * 1440.0 / self.window_minutes

    @property
    def throughput_wafers_per_day(self) -> float:
        return self.throughput_lots_per_day * self.wafers_per_lot

    # ---- 사이클타임 -----------------------------------------------------

    @property
    def mean_cycle_hours(self) -> float:
        return _mean(self.cycle_times) / 60.0

    @property
    def median_cycle_hours(self) -> float:
        return _quantile(self.cycle_times, 0.5) / 60.0

    @property
    def p95_cycle_hours(self) -> float:
        return _quantile(self.cycle_times, 0.95) / 60.0

    @property
    def mean_raw_process_hours(self) -> float:
        return _mean(self.raw_process_minutes) / 60.0

    @property
    def mean_transport_hours(self) -> float:
        """lot 1개가 반송에 쓴 시간 (반송차 대기 포함)."""
        return _mean(self.transport_minutes) / 60.0

    @property
    def transport_share(self) -> float:
        """사이클타임 중 반송이 차지하는 비율."""
        ct = _mean(self.cycle_times)
        return _mean(self.transport_minutes) / ct if ct > 0 else 0.0

    @property
    def x_factor(self) -> float:
        """사이클타임 ÷ 순수 처리시간. 1.0이면 대기가 전혀 없다는 뜻."""
        raw = _mean(self.raw_process_minutes)
        return _mean(self.cycle_times) / raw if raw > 0 else 0.0

    # ---- 진단 -----------------------------------------------------------

    def utilizations(self) -> dict[str, float]:
        return {
            gid: g.utilization(self.window_minutes) for gid, g in self.groups.items()
        }

    def load_factors(self) -> dict[str, float]:
        return {
            gid: g.load_factor(self.window_minutes) for gid, g in self.groups.items()
        }

    @property
    def vehicle_utilization(self) -> float:
        return self.transport.utilization(self.window_minutes) if self.transport else 0.0

    def bottlenecks(self, top: int = 3) -> list[tuple[str, float]]:
        u = self.utilizations()
        return sorted(u.items(), key=lambda kv: -kv[1])[:top]

    def queue_hotspots(self, top: int = 3) -> list[tuple[str, float]]:
        """큐 대기시간 총합이 큰 그룹. 어디를 손봐야 하는지 알려준다."""
        items = [(gid, g.queue_minutes / 60.0) for gid, g in self.groups.items()]
        return sorted(items, key=lambda kv: -kv[1])[:top]

    def summary(self) -> str:  # pragma: no cover - 사람이 읽는 요약
        b = ", ".join(f"{g} {u:.0%}" for g, u in self.bottlenecks())
        t = ""
        if self.transport:
            t = (f" · 반송차 {self.vehicle_utilization:.0%}"
                 f"(대기 {self.transport.mean_wait_minutes:.1f}분)")
        return (
            f"처리량 {self.throughput_lots_per_day:.2f} lot/일 "
            f"({self.throughput_wafers_per_day:.0f} 웨이퍼/일) · "
            f"사이클타임 {self.mean_cycle_hours:.1f}h (p95 {self.p95_cycle_hours:.1f}h) · "
            f"X-factor {self.x_factor:.2f} · WIP {self.mean_wip:.1f} · "
            f"가동률 상위 [{b}]{t} · 표본 {self.completed}개 · "
            f"이벤트 {self.events:,}개 / {self.wall_seconds:.2f}s"
        )


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _quantile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


@dataclass
class Replications:
    """독립 시드 반복 결과. 신뢰구간을 붙여 보고한다."""

    runs: list[SimResult]

    def _vals(self, attr: str) -> list[float]:
        return [getattr(r, attr) for r in self.runs]

    def mean(self, attr: str) -> float:
        return _mean(self._vals(attr))

    def half_width(self, attr: str, z: float = 1.96) -> float:
        """95% 신뢰구간의 반폭."""
        xs = self._vals(attr)
        if len(xs) < 2:
            return 0.0
        return z * _stdev(xs) / sqrt(len(xs))

    def relative_error(self, attr: str) -> float:
        m = self.mean(attr)
        return self.half_width(attr) / m if m else 0.0

    def report(self, attr: str, fmt: str = ".2f") -> str:  # pragma: no cover
        return f"{self.mean(attr):{fmt}} ± {self.half_width(attr):{fmt}}"
