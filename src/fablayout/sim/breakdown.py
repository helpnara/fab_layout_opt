"""설비 고장과 수리.

**고장간격은 가동시간(busy time) 기준으로 센다.** 달력시간 기준으로 하면 놀고 있는
설비도 고장이 나고, 그러면 저부하 설비의 실측 가동률이 이론값 A = MTBF/(MTBF+MTTR)
아래로 내려가 계산이 어긋난다. 실제 장비도 대부분의 고장 모드가 사용량에 따라
발생한다.

처리 중 고장이 나면 **처리를 중단하고 수리 후 잔여시간만큼 재개**한다(잔여시간 보존).
lot을 버리는 폐기(scrap)나 처음부터 다시 하는 재작업(rework)은 범위 밖이다.

    고장간격  Exp(1/MTBF)      가동시간 기준
    수리시간  로그정규(MTTR, cv=0.4)
"""

from __future__ import annotations

import random

from .rng import lognormal_params

REPAIR_CV = 0.4
"""수리시간 변동계수. 수리는 원인에 따라 편차가 크므로 처리시간보다 크게 잡는다."""


def time_to_failure(rng: random.Random, mtbf_hours: float) -> float:
    """다음 고장까지의 **가동시간**(분). 무고장 설비는 무한대."""
    if mtbf_hours >= 1e8:
        return float("inf")
    return rng.expovariate(1.0 / (mtbf_hours * 60.0))


def repair_minutes(rng: random.Random, mttr_hours: float) -> float:
    if mttr_hours <= 0.0:
        return 0.0
    mu, sigma = lognormal_params(mttr_hours * 60.0, REPAIR_CV)
    from math import exp

    return exp(rng.gauss(mu, sigma))
