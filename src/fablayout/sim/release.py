"""웨이퍼 투입 정책.

M2에서는 **고정 투입률**만 구현한다. 생산능력 탐색(§6.8)은 이 정책의 투입률을
바꿔가며 DES를 반복 실행하는 상위 절차이며 M4에서 붙인다. CONWIP은 §11의 선택
항목으로 남긴다.

투입 간격 분포
    poisson       : 지수분포 간격. 대기행렬 이론과 대조하려면 이것이 필요하다
                    (M/G/1 검증). 변동이 커서 사이클타임이 길게 나온다.
    deterministic : 등간격. 실제 fab의 투입 계획에 가깝다.
"""

from __future__ import annotations

import random
from typing import Literal

Arrival = Literal["poisson", "deterministic"]


class FixedRateRelease:
    """일정한 평균 투입률로 lot을 넣는다."""

    __slots__ = ("lots_per_day", "arrival", "_mean_gap", "_products", "_weights")

    def __init__(
        self,
        lots_per_day: float,
        products: tuple[str, ...],
        mix: tuple[float, ...],
        arrival: Arrival = "poisson",
    ) -> None:
        if lots_per_day <= 0:
            raise ValueError("투입률 > 0 이어야 한다")
        self.lots_per_day = lots_per_day
        self.arrival = arrival
        self._mean_gap = 1440.0 / lots_per_day
        self._products = products
        self._weights = mix

    @property
    def mean_gap_minutes(self) -> float:
        return self._mean_gap

    def next_gap(self, rng: random.Random) -> float:
        if self.arrival == "deterministic":
            return self._mean_gap
        return rng.expovariate(1.0 / self._mean_gap)

    def next_product(self, rng: random.Random) -> str:
        if len(self._products) == 1:
            return self._products[0]
        return rng.choices(self._products, weights=self._weights, k=1)[0]
