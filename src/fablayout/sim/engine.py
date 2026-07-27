"""이산사건 시뮬레이션 이벤트 큐.

시간 단위는 **분(minute)** 이다. 설비 처리시간이 분 단위로 주어지므로 그대로 쓴다.
반송 파라미터는 초 단위라 사용하는 쪽에서 환산한다 (M3).

SimPy 대신 자체 구현을 쓰는 이유는 속도다. 생성자(generator) 기반 프로세스 모델은
이벤트마다 프레임 전환 비용이 붙는데, 이 프로젝트는 최적화 후보 검증에 DES를 수백 회
돌려야 하므로 그 비용이 직접적인 제약이 된다. 대신 논리 오류 위험이 커지므로,
같은 소형 모델을 SimPy로도 작성해 결과가 일치하는지 확인한다
(`tests/test_sim_simpy.py`).

**결정론성**: 같은 seed면 항상 같은 결과가 나와야 한다. 동시각 이벤트가 힙에서 임의
순서로 나오면 이것이 깨지므로, 단조 증가 일련번호를 두 번째 정렬 키로 쓴다.
"""

from __future__ import annotations

from heapq import heappop, heappush

# 이벤트 종류 — 작은 정수로 두어 디스패치를 if/elif 사슬로 처리한다.
EV_RELEASE = 0
"""lot 투입."""
EV_PROC_END = 1
"""설비 처리 완료."""
EV_ARRIVE = 2
"""반송 완료 후 다음 설비 도착 (M3에서 사용. M2는 반송시간 0이라 즉시 호출)."""
EV_TOOL_DOWN = 3
"""설비 고장 (M3)."""
EV_TOOL_UP = 4
"""수리 완료 (M3)."""
EV_BATCH_TIMEOUT = 5
"""배치 형성 타임아웃 (M3)."""

EVENT_NAMES = {
    EV_RELEASE: "RELEASE",
    EV_PROC_END: "PROC_END",
    EV_ARRIVE: "ARRIVE",
    EV_TOOL_DOWN: "TOOL_DOWN",
    EV_TOOL_UP: "TOOL_UP",
    EV_BATCH_TIMEOUT: "BATCH_TIMEOUT",
}


class EventQueue:
    """(시각, 일련번호, 종류, 페이로드) 최소 힙.

    일련번호는 동시각 이벤트의 순서를 삽입 순으로 고정한다 — 재현성의 근거다.
    """

    __slots__ = ("_heap", "_seq", "now", "popped")

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, int, object]] = []
        self._seq = 0
        self.now = 0.0
        self.popped = 0
        """처리한 이벤트 수. 성능 측정과 규모 진단에 쓴다."""

    def __len__(self) -> int:
        return len(self._heap)

    def schedule(self, delay: float, kind: int, payload: object) -> None:
        """`delay`분 뒤의 이벤트를 넣는다."""
        if delay < 0.0:
            raise ValueError(f"음수 지연: {delay}")
        self._seq += 1
        heappush(self._heap, (self.now + delay, self._seq, kind, payload))

    def schedule_at(self, when: float, kind: int, payload: object) -> None:
        if when < self.now:
            raise ValueError(f"과거 시각 예약: {when} < {self.now}")
        self._seq += 1
        heappush(self._heap, (when, self._seq, kind, payload))

    def pop(self) -> tuple[int, object]:
        """가장 이른 이벤트를 꺼내고 시계를 그 시각으로 옮긴다."""
        when, _, kind, payload = heappop(self._heap)
        self.now = when
        self.popped += 1
        return kind, payload

    def clear(self) -> None:
        self._heap.clear()
