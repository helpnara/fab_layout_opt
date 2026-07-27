"""시뮬레이션 중의 lot 상태."""

from __future__ import annotations


class Lot:
    """웨이퍼 25장 단위의 반송·처리 객체.

    `process_times`는 투입 시점에 lot 전용 난수 스트림으로 **한 번에** 생성된다
    (`rng.py` 참조). 배치가 바뀌어도 같은 lot은 같은 처리시간을 겪으므로 배치 비교의
    분산이 줄어든다.
    """

    __slots__ = (
        "lot_id", "pid", "route", "process_times",
        "step", "release_time", "done_time",
        "queue_minutes", "process_minutes", "transport_minutes",
        "_step_enqueued",
    )

    def __init__(
        self,
        lot_id: int,
        pid: str,
        route: tuple[str, ...],
        process_times: list[float],
        release_time: float,
    ) -> None:
        self.lot_id = lot_id
        self.pid = pid
        self.route = route
        """스텝별 설비 그룹 ID."""
        self.process_times = process_times
        self.step = 0
        self.release_time = release_time
        self.done_time = -1.0
        self.queue_minutes = 0.0
        self.process_minutes = 0.0
        self.transport_minutes = 0.0
        self._step_enqueued = release_time

    @property
    def steps_total(self) -> int:
        return len(self.route)

    @property
    def done(self) -> bool:
        return self.step >= len(self.route)

    @property
    def current_group(self) -> str:
        return self.route[self.step]

    @property
    def cycle_time(self) -> float:
        return self.done_time - self.release_time

    def raw_process_minutes(self) -> float:
        """이 lot이 실제로 뽑은 처리시간의 합. X-factor의 분모."""
        return sum(self.process_times)

    def remaining_process_minutes(self) -> float:
        """남은 스텝의 처리시간 합. SRPT 규칙이 쓴다."""
        return sum(self.process_times[self.step:])

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Lot {self.lot_id} {self.pid} step {self.step}/{len(self.route)}>"
