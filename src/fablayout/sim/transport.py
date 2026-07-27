"""반송 — 대수가 제한된 반송차 풀.

M1에서 확인한 대로 **배치가 처리량에 영향을 주는 경로가 여기다**. 배치가 나쁘면
bay 교차가 늘고 → 스토커 조작이 늘고 → 반송차가 모자라고 → 설비가 반송을 기다리며
유휴가 된다. 반송차 대수가 여유로우면 이 연결이 끊겨 배치가 무의미해진다.

모델링 범위와 단순화
    포함: 반송차 대수 제한, 빈차 회송, 스토커 입출고 비용(bay 경계 통과 시)
    제외: 궤도 구간 점유·추월·정체 (인터뷰 확정 사항)

**목적지 결정.** lot이 출발할 때 도착 설비가 아직 정해지지 않았다(디스패칭은 큐에서
일어난다). 실제 fab에서도 interbay 반송은 "도착 bay의 스토커까지"이고 최종 설비 배정은
그 다음이다. 그래서 이동 거리는 **출발 slot에서 도착 그룹의 가장 가까운 설비까지**로
잡고, 실제 설비 배정은 도착 후 큐에서 이루어진다.

**출발 설비는 막지 않는다.** 처리를 마친 lot은 설비를 즉시 비워주고 출력 포트에서
반송차를 기다린다. 설비가 반송차를 기다리며 잠기는 blocking은 모델링하지 않는다.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

from ..core.model import TransportSpec
from .lot import Lot
from .metrics import overlap

ENTRY = -1
"""투입/출하 스토커의 위치 인덱스. spine 왼쪽 끝(x=0)."""


class MoveRequest:
    """반송 요청 하나."""

    __slots__ = ("lot", "origin", "dest", "stocker_ops", "requested_at")

    def __init__(self, lot: Lot, origin: int, dest: int, stocker_ops: int,
                 requested_at: float) -> None:
        self.lot = lot
        self.origin = origin
        self.dest = dest
        self.stocker_ops = stocker_ops
        self.requested_at = requested_at


class TransportPool:
    """반송차 공유 풀.

    반송차 배정은 **빈차 이동거리가 가장 짧은 유휴 차량**(nearest-vehicle)이다.
    유휴 차량이 없으면 요청이 FIFO 큐에 쌓이고, 이 대기시간이 배치 품질의 결과로
    나타난다.
    """

    __slots__ = (
        "n", "spec", "dist", "pos", "free", "queue",
        "busy_minutes", "wait_minutes", "moves", "queued_moves",
        "stocker_ops_total", "distance_m", "w0", "w1",
    )

    def __init__(
        self,
        vehicles: int,
        spec: TransportSpec,
        dist: Callable[[int, int], float],
        w0: float,
        w1: float,
    ) -> None:
        if vehicles < 1:
            raise ValueError("반송차는 1대 이상이어야 한다")
        self.n = vehicles
        self.spec = spec
        self.dist = dist
        self.pos = [ENTRY] * vehicles          # 모두 투입 스토커에서 시작
        self.free = list(range(vehicles))
        self.queue: deque[MoveRequest] = deque()
        self.busy_minutes = 0.0
        self.wait_minutes = 0.0
        self.moves = 0
        self.queued_moves = 0
        """반송차가 없어 대기해야 했던 요청 수."""
        self.stocker_ops_total = 0
        self.distance_m = 0.0
        self.w0 = w0
        self.w1 = w1

    # ---- 요청 -----------------------------------------------------------

    def submit(self, now: float, req: MoveRequest) -> tuple[int, float] | None:
        """요청을 넣는다. 즉시 배정되면 (차량, 소요분), 대기하면 None."""
        if not self.free:
            self.queue.append(req)
            self.queued_moves += 1
            return None
        v = self._nearest_vehicle(req.origin)
        return v, self._dispatch(now, v, req)

    def release(self, now: float, vehicle: int) -> tuple[MoveRequest, int, float] | None:
        """차량을 반납한다. 대기 요청이 있으면 (요청, 차량, 소요분)."""
        self.free.append(vehicle)
        if not self.queue:
            return None
        req = self.queue.popleft()
        v = self._nearest_vehicle(req.origin)
        return req, v, self._dispatch(now, v, req)

    # ---- 내부 -----------------------------------------------------------

    def _nearest_vehicle(self, origin: int) -> int:
        best = self.free[0]
        best_d = self.dist(self.pos[best], origin)
        for v in self.free[1:]:
            d = self.dist(self.pos[v], origin)
            if d < best_d:
                best, best_d = v, d
        return best

    def _dispatch(self, now: float, vehicle: int, req: MoveRequest) -> float:
        self.free.remove(vehicle)
        empty_m = self.dist(self.pos[vehicle], req.origin)
        loaded_m = self.dist(req.origin, req.dest)
        seconds = (
            self.spec.empty_seconds(empty_m)
            + self.spec.move_seconds(loaded_m, req.stocker_ops)
        )
        duration = seconds / 60.0
        self.pos[vehicle] = req.dest

        self.busy_minutes += overlap(now, now + duration, self.w0, self.w1)
        if now >= self.w0:
            self.moves += 1
            self.stocker_ops_total += req.stocker_ops
            self.distance_m += loaded_m + empty_m
            self.wait_minutes += now - req.requested_at
        return duration

    # ---- 지표 -----------------------------------------------------------

    def utilization(self, window_minutes: float) -> float:
        cap = self.n * window_minutes
        return self.busy_minutes / cap if cap > 0 else 0.0

    @property
    def mean_wait_minutes(self) -> float:
        return self.wait_minutes / self.moves if self.moves else 0.0
