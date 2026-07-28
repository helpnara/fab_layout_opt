"""대리지표 위에서 도는 담금질(simulated annealing).

**DES 위에서 직접 담금질하지 않는다.** M4에서 잰 바로는 후보 1개 평가에 `midfab`
기준 17초가 들고, 그러고도 쌍대 신뢰구간이 ±13시간 남는다. 찾는 개선이 그와
비슷한 규모다. 그런 목적함수 위에서 수천 번 이동하는 담금질은 **시간으로도,
정확도로도 성립하지 않는다** — 잡음을 타고 내려가 개선하지 않고도 개선한 것처럼
보이는 편향(winner's curse)이 남는다.

그래서 담금질은 **결정론적인 대리지표** 위에서 돈다. 잡음이 0이므로 "좋아졌다"가
항상 진짜 좋아진 것이고, 한 번 계산에 밀리초가 든다. DES는 마지막에 상위 소수만
검증하는 데 쓴다 (`scripts/expand_space.py`).

    1단  대리지표 담금질   수천~수만 이동, 잡음 0, 초 단위
    2단  DES 검증          상위 k개만, 공통난수 + 쌍대 비교

이 구조가 성립하려면 대리지표의 **순위**가 DES와 맞아야 한다. 그 검증이
`scripts/calibrate_surrogate.py`의 게이트다(순위상관 ρ ≥ 0.7).

이동은 `opt/moves.py`의 **그룹 단위** 연산이다. 설비 두 대를 맞바꾸는 tool 단위
이동으로는 탐색이 되지 않는다 — 거의 항상 그룹을 쪼개고, 서버 풀 분할 벌점이 그
손해를 즉시 되돌린다.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ..core.geometry import LayoutGeometry
from ..core.model import Assignment
from .moves import BayPlan, neighbor
from .surrogate import Surrogate


@dataclass(frozen=True)
class AnnealResult:
    """담금질 결과. `history`는 그때까지의 최선값 궤적이다."""

    plan: BayPlan
    assignment: Assignment
    score: float
    start_score: float
    history: list[float]
    accepted: int
    steps: int

    @property
    def gain(self) -> float:
        """대리지표 기준 개선율. 양수면 좋아진 것이다."""
        return (self.start_score - self.score) / self.start_score if self.start_score else 0.0


def anneal(
    surrogate: Surrogate,
    geo: LayoutGeometry,
    start: Assignment,
    counts: dict[str, int] | None = None,
    steps: int = 4000,
    t0: float | None = None,
    t1_ratio: float = 0.02,
    seed: int = 0,
    restarts: int = 1,
) -> AnnealResult:
    """대리지표를 최소화하는 배치를 찾는다.

    온도는 초기값에서 기하적으로 식힌다. 초기 온도를 지정하지 않으면 시작 점수의
    2%로 잡는다 — 점수의 절대 크기가 데이터셋마다 다르므로 상대값으로 두어야
    한 설정이 여러 규모에서 통한다.
    """
    plan0 = BayPlan.from_assignment(start, geo)
    s0 = surrogate.score(start, counts)
    hi = t0 if t0 is not None else max(s0 * 0.02, 1e-6)
    lo = hi * t1_ratio

    best_plan, best = plan0, s0
    history = [s0]
    accepted = 0

    for r in range(restarts):
        rng = random.Random(seed + r * 104_729)
        cur_plan, cur = plan0, s0
        for i in range(steps):
            temp = hi * (lo / hi) ** (i / max(steps - 1, 1))
            nxt = neighbor(cur_plan, rng)
            if nxt is None:
                continue
            score = surrogate.score(nxt.to_assignment(geo), counts)
            delta = score - cur
            if delta <= 0 or rng.random() < math.exp(-delta / temp):
                cur_plan, cur = nxt, score
                accepted += 1
                if cur < best:
                    best_plan, best = cur_plan, cur
            history.append(best)

    return AnnealResult(
        plan=best_plan,
        assignment=best_plan.to_assignment(geo),
        score=best,
        start_score=s0,
        history=history,
        accepted=accepted,
        steps=steps * restarts,
    )
