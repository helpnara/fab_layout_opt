"""배치 설비의 배치 형성.

확산로·세정 장비는 lot을 여러 개 모아 한 번에 처리한다. 모으는 대기시간이 생기고,
저부하일수록 그 대기가 길어진다 — 능력은 남는데 사이클타임이 나빠지는 구간이 생기는
이유다.

정책 `greedy_full`
    1. 큐에 batch_size개가 모이면 즉시 착수
    2. 부족한 상태로 `max_wait`가 지나면 **부분 배치**로 착수
    3. 서로 다른 제품의 lot을 같은 배치에 섞을 수 있다 (동일 그룹 = 동일 레시피 가정)

부분 배치 착수는 반드시 필요하다. 순수 full-batch만 허용하면 저부하 구간에서 lot이
영구히 대기할 수 있다.

**배치 처리시간**은 구성원 중 가장 긴 값을 쓴다. 로(爐)는 한 레시피를 한 번 돌리므로
lot마다 다른 시간이 나올 수 없고, 배치는 가장 오래 걸리는 구성원에 맞춰야 한다.
"""

from __future__ import annotations

DEFAULT_MAX_WAIT_MINUTES = 60.0
"""배치가 다 차지 않아도 착수하는 대기 상한."""


def batch_duration(process_times: list[float]) -> float:
    """배치 구성원들의 처리시간에서 배치 1회 소요시간을 정한다."""
    return max(process_times)
