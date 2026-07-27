"""난수 스트림 분리와 공통난수(CRN).

**왜 스트림을 나누는가.** 배치 A와 배치 B를 비교할 때 난수 소비 순서가 달라지면
"배치 효과"와 "난수 노이즈"가 섞여 구분되지 않는다. 배치를 바꾸면 설비 선택 순서가
바뀌므로, 공용 스트림 하나를 쓰면 이 일이 반드시 일어난다.

**해법 — lot 고정 처리시간.** lot의 모든 스텝 처리시간을 **투입 시점에 그 lot 전용
스트림으로 한 번에 생성**한다. 그러면 lot 17번의 23번째 스텝 처리시간은 배치가 어떻든
항상 같은 값이다. 배치 비교에서 분산이 크게 줄어든다(공통난수, CRN).

    lot 스트림 seed = hash(master_seed, lot_id)

투입 간격·고장·수리는 각각 별도 스트림을 쓴다.
"""

from __future__ import annotations

import random
from math import exp, log, sqrt


def lognormal_params(mean: float, cv: float) -> tuple[float, float]:
    """평균과 변동계수로부터 로그정규 분포의 (mu, sigma)를 구한다.

    `random.lognormvariate(mu, sigma)`는 **로그의** 평균/표준편차를 받으므로 환산이
    필요하다. 이걸 빼먹으면 실제 평균이 의도한 값보다 커진다.
    """
    if cv <= 0.0:
        raise ValueError("cv > 0 이어야 한다")
    sigma2 = log(1.0 + cv * cv)
    sigma = sqrt(sigma2)
    mu = log(mean) - sigma2 / 2.0
    return mu, sigma


def lognormal_second_moment(mean: float, cv: float) -> float:
    """E[S²] = Var + mean². M/G/1 이론값 대조에 쓴다."""
    return (cv * cv + 1.0) * mean * mean


class RandomStreams:
    """목적별로 분리된 난수 스트림."""

    __slots__ = ("master_seed", "release", "breakdown", "repair", "misc")

    def __init__(self, seed: int) -> None:
        self.master_seed = seed
        self.release = random.Random(seed * 6364136223846793005 + 1)
        self.breakdown = random.Random(seed * 6364136223846793005 + 2)
        self.repair = random.Random(seed * 6364136223846793005 + 3)
        self.misc = random.Random(seed * 6364136223846793005 + 4)

    def lot_stream(self, lot_id: int) -> random.Random:
        """lot 전용 스트림. lot_id만으로 결정되므로 배치가 바뀌어도 불변이다."""
        return random.Random((self.master_seed << 20) ^ (lot_id * 2654435761))


def draw_process_times(
    rng: random.Random,
    means: tuple[float, ...],
    cvs: tuple[float, ...],
) -> list[float]:
    """라우트 전체의 처리시간을 한 번에 뽑는다.

    cv가 0이면 결정론적 값을 그대로 쓴다 (검증용 시나리오에서 필요).
    """
    out: list[float] = []
    for mean, cv in zip(means, cvs):
        if cv <= 0.0:
            out.append(mean)
        else:
            mu, sigma = lognormal_params(mean, cv)
            out.append(exp(rng.gauss(mu, sigma)))
    return out
