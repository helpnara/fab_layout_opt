"""최적화 목표 함수.

    **capex 예산 제약 하에서, 목표 처리량을 만족하면서 사이클타임 최소화**

    minimize   평균 사이클타임
    s.t.       capex(설비 대수, 반송차 대수) ≤ 예산
               지속 가능한 처리량 ≥ 목표 처리량
               slot 배타 (배치가 물리적으로 성립)

**왜 처리량이 목적이 아니라 제약인가.** M3 실측에서 배치는 처리량을 바꾸지 못했다.
같은 투입률에서 세 배치안의 처리량이 신뢰구간 안에서 동일했고(5.93/5.93/5.95 lot/일)
사이클타임만 14% 차이가 났다. 병목이 설비이지 반송이 아니기 때문이다. 처리량을
목적으로 두면 최적화기는 배치에 무차별해지고 설비 대수만 조정하게 된다.

역할이 이렇게 나뉜다.

    처리량을 움직이는 것    설비 대수, 반송차 대수 — 돈을 쓰는 결정 → **제약**
    사이클타임을 움직이는 것 배치 — 돈을 쓰지 않고 얻는 것 → **목적**

**평가 비용.** 목표 처리량 한 지점에서만 시뮬레이션하면 되므로 후보당 DES 실행이
반복 횟수(기본 3회)뿐이다. 원래 설계(생산능력 탐색 7회 × 반복 3회 = 21회)보다 7배
싸다 — 목표 함수를 바꾼 부수 효과다.

**기각은 단계적으로.** 비싼 순서대로 나중에 검사한다.

    1. slot 수용 가능?          즉시
    2. capex ≤ 예산?            즉시
    3. 해석적 상한 ≥ 목표?      즉시 (대기를 무시한 낙관적 상한이므로, 이걸 못 넘으면 확실히 불가)
    4. DES에서 목표를 지속?     비쌈
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from math import sqrt

from ..core.analysis import analytic_capacity
from ..core.geometry import LayoutGeometry
from ..core.model import Assignment, Fab
from ..sim.capacity import check_stability
from ..sim.metrics import Replications
from ..sim.runner import SimConfig, simulate
from .cost import Budget


class Reject(str, Enum):
    """기각 사유. 어느 제약에 걸렸는지 알아야 이웃 연산자를 고칠 수 있다."""

    SLOTS = "slot 부족"
    BUDGET = "예산 초과"
    ANALYTIC = "해석적 상한 미달"
    THROUGHPUT = "목표 처리량 미달"
    NONE = ""


@dataclass(frozen=True)
class Candidate:
    """최적화의 한 해(solution)."""

    assignment: Assignment
    counts: dict[str, int]
    vehicles: int

    @property
    def total_tools(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class Evaluation:
    """후보 1개의 평가 결과."""

    feasible: bool
    reject: Reject
    cycle_hours: float = float("inf")
    """목적 함수 값. 실행 가능하지 않으면 무한대."""
    cycle_hours_ci: float = 0.0
    throughput: float = 0.0
    throughput_ratio: float = 0.0
    x_factor: float = 0.0
    wip: float = 0.0
    capex: float = 0.0
    vehicle_utilization: float = 0.0
    analytic_bound: float = 0.0
    detail: str = ""
    runs: tuple = field(default=(), repr=False)

    @property
    def score(self) -> float:
        """낮을수록 좋다. 실행 불가능한 후보는 무한대라 비교에서 자동으로 밀린다."""
        return self.cycle_hours

    def better_than(self, other: Evaluation) -> bool:
        return self.score < other.score

    def paired_delta(self, other: Evaluation) -> tuple[float, float]:
        """`self`와 `other`의 **시드별 짝지은** 사이클타임 차이 (평균, 95% CI 반폭).

        음수면 self가 더 짧다(더 좋다). 두 후보를 같은 시드로 평가했으므로 시드가
        만든 변동이 상쇄되어, 각자의 신뢰구간을 따로 비교할 때보다 약 2배 정밀하다
        (실측: 독립 ±17.1h → 쌍대 ±8.6h).

        시뮬레이션 최적화에서 이 구분은 결정적이다. 잡음 섞인 평가를 탐욕적으로 받아들이면
        운 좋은 평가만 골라 내려가는 편향(winner's curse)이 생겨, 개선하지 않고도
        개선한 것처럼 보인다.
        """
        if not (self.runs and other.runs) or len(self.runs) != len(other.runs):
            return (self.cycle_hours - other.cycle_hours,
                    self.cycle_hours_ci + other.cycle_hours_ci)
        d = [a.mean_cycle_hours - b.mean_cycle_hours
             for a, b in zip(self.runs, other.runs)]
        n = len(d)
        mean = sum(d) / n
        if n < 2:
            return mean, 0.0
        var = sum((x - mean) ** 2 for x in d) / (n - 1)
        return mean, 1.96 * sqrt(var / n)

    def significantly_better_than(self, other: Evaluation) -> bool:
        """쌍대 차이가 신뢰구간을 넘을 때만 개선으로 인정한다."""
        if not (self.feasible and other.feasible):
            return self.feasible and not other.feasible
        delta, ci = self.paired_delta(other)
        return delta < -ci

    def summary(self) -> str:  # pragma: no cover
        if not self.feasible:
            return f"기각 [{self.reject.value}] {self.detail}"
        return (
            f"사이클타임 {self.cycle_hours:.1f}±{self.cycle_hours_ci:.1f}h · "
            f"처리량 {self.throughput:.2f} lot/일 · X-factor {self.x_factor:.2f} · "
            f"WIP {self.wip:.0f} · capex {self.capex:.1f} · "
            f"반송차 {self.vehicle_utilization:.0%}"
        )


@dataclass(frozen=True)
class Objective:
    """목표 함수와 제약의 묶음."""

    fab: Fab
    geo: LayoutGeometry
    budget: Budget
    target_lots_per_day: float
    """만족해야 할 처리량. 이 지점에서 사이클타임을 잰다."""
    sim_config: SimConfig
    replications: int = 3
    throughput_tolerance: float = 0.97
    analytic_margin: float = 1.0
    """해석적 상한이 목표의 몇 배 이상이어야 통과시킬지. 1.0이면 상한만 넘으면 통과."""

    @classmethod
    def default(
        cls,
        fab: Fab,
        geo: LayoutGeometry,
        target_fraction: float = 0.90,
        warmup_days: float = 60.0,
        run_days: float = 240.0,
        seed: int = 20260727,
        replications: int = 3,
    ) -> Objective:
        """기준 구성 기준의 기본 설정.

        목표 처리량은 해석적 상한의 `target_fraction`. 예산은 기준 구성과 동일하게 잡아
        "돈을 더 쓰지 않고 배치로 얼마나 개선되는가"를 기본 실험으로 둔다.
        """
        bound = analytic_capacity(fab).capacity_lots_per_day
        return cls(
            fab=fab,
            geo=geo,
            budget=Budget.same_as_baseline(fab),
            target_lots_per_day=bound * target_fraction,
            sim_config=SimConfig(
                release_lots_per_day=bound * target_fraction,
                warmup_days=warmup_days,
                run_days=run_days,
                seed=seed,
                collect_wip_series=True,
                wip_sample_minutes=1440.0,
            ),
            replications=replications,
        )

    # ---- 평가 -----------------------------------------------------------

    def evaluate(self, cand: Candidate) -> Evaluation:
        """싼 제약부터 순서대로 검사하고, 통과한 후보만 DES로 평가한다."""
        capex = self.budget.spend(cand.counts, cand.vehicles)

        if cand.total_tools > self.geo.slot_count:
            return Evaluation(False, Reject.SLOTS, capex=capex,
                              detail=f"설비 {cand.total_tools}대 > slot {self.geo.slot_count}개")
        if capex > self.budget.limit + 1e-9:
            return Evaluation(False, Reject.BUDGET, capex=capex,
                              detail=f"{capex:.1f} > 예산 {self.budget.limit:.1f}")

        fab = self._fab_for(cand)
        bound = analytic_capacity(fab).capacity_lots_per_day
        if bound < self.target_lots_per_day * self.analytic_margin:
            return Evaluation(False, Reject.ANALYTIC, capex=capex, analytic_bound=bound,
                              detail=f"상한 {bound:.2f} < 목표 "
                                     f"{self.target_lots_per_day:.2f} lot/일")

        cfg = replace(self.sim_config, release_lots_per_day=self.target_lots_per_day)
        runs = [
            simulate(fab, replace(cfg, seed=cfg.seed + i * 7919), cand.assignment, self.geo)
            for i in range(self.replications)
        ]
        reps = Replications(runs)
        throughput = reps.mean("throughput_lots_per_day")
        ratio = throughput / self.target_lots_per_day

        # 반복 중 하나라도 지속 불가면 그 구성은 목표를 못 지킨다
        verdicts = [
            check_stability(r, self.target_lots_per_day, cfg.warmup_days, cfg.run_days,
                            throughput_tolerance=self.throughput_tolerance)
            for r in runs
        ]
        failed = [v for v in verdicts if not v.sustainable]
        common = dict(
            capex=capex, analytic_bound=bound, throughput=throughput,
            throughput_ratio=ratio, runs=tuple(runs),
            wip=reps.mean("mean_wip"),
            x_factor=reps.mean("x_factor"),
            vehicle_utilization=reps.mean("vehicle_utilization"),
        )
        if failed:
            return Evaluation(False, Reject.THROUGHPUT,
                              detail=failed[0].reason, **common)

        return Evaluation(
            True, Reject.NONE,
            cycle_hours=reps.mean("mean_cycle_hours"),
            cycle_hours_ci=reps.half_width("mean_cycle_hours"),
            **common,
        )

    def _fab_for(self, cand: Candidate) -> Fab:
        """후보의 대수 구성·반송차 대수를 반영한 Fab."""
        from ..core.model import ToolGroup, TransportSpec

        groups = {
            gid: replace(g, count=cand.counts[gid]) if hasattr(g, "__replace__") else
            ToolGroup(g.gid, g.name, cand.counts[gid], g.process_minutes, g.mode,
                      g.batch_size, g.mtbf_h, g.mttr_h, g.process_cv, g.family)
            for gid, g in self.fab.groups.items()
        }
        t = self.fab.transport
        return Fab(
            name=self.fab.name,
            groups=groups,
            products=self.fab.products,
            wafers_per_lot=self.fab.wafers_per_lot,
            transport=TransportSpec(
                cand.vehicles, t.speed_mps, t.load_time_s, t.unload_time_s,
                t.handoff_time_s, t.stocker_time_s,
            ),
            source=self.fab.source,
        )

    def baseline_candidate(self, assignment: Assignment) -> Candidate:
        return Candidate(assignment, dict(self.fab.tool_counts), self.fab.transport.vehicles)
