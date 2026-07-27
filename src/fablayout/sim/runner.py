"""DES 실행기 — lot이 라우트를 따라 설비 그룹을 거치는 흐름.

M2 범위: 설비 그룹 · 큐 · 처리시간 변동 · 디스패칭 · 고정 투입. 아직 없는 것은
고장(M3), 배치 설비(M3), 반송(M3)이다. 이들이 붙을 자리는 아래에 표시해 두었다.

lot 한 스텝의 흐름::

    _arrive(lot)            큐에 넣고 (반송 도착 지점 — M3에서 여기로 진입)
      → _try_start(station) 빈 설비가 있으면 디스패칭 규칙으로 lot을 골라 착수
        → EV_PROC_END       처리 완료 시각에 이벤트 예약
          → _finish_step    설비를 반납하고 다음 스텝으로. 라우트 끝이면 출하

이벤트는 **처리 완료 하나뿐**이다. 큐 진입·착수는 이벤트가 아니라 함수 호출이라
힙 연산이 줄고, 이것이 SimPy 대비 속도 이점의 대부분을 차지한다.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from ..core.model import Assignment, Fab
from . import dispatch as dispatch_rules
from .engine import EV_PROC_END, EV_RELEASE, EventQueue
from .lot import Lot
from .metrics import GroupMetrics, SimResult, TimeIntegral, overlap
from .release import Arrival, FixedRateRelease
from .rng import RandomStreams, draw_process_times


@dataclass(frozen=True)
class SimConfig:
    """시뮬레이션 실행 설정."""

    release_lots_per_day: float
    """고정 투입률. 생산능력 탐색(M4)이 이 값을 바꿔가며 반복 호출한다."""
    warmup_days: float = 30.0
    run_days: float = 90.0
    seed: int = 20260727
    arrival: Arrival = "poisson"
    dispatch: str = "fifo"
    process_cv_override: float | None = None
    """모든 그룹의 처리시간 변동계수를 덮어쓴다. 검증 시나리오용 (None이면 그룹 값 사용)."""
    max_lots: int = 0
    """0이면 무제한. 성능 측정에서 규모를 고정할 때 쓴다."""
    collect_wip_series: bool = False
    """WIP 시계열을 남긴다. 대시보드용이며 기본은 끈다 (메모리)."""
    wip_sample_minutes: float = 60.0

    @property
    def warmup_minutes(self) -> float:
        return self.warmup_days * 1440.0

    @property
    def end_minutes(self) -> float:
        return (self.warmup_days + self.run_days) * 1440.0

    @property
    def window_minutes(self) -> float:
        return self.run_days * 1440.0


class _Station:
    """설비 그룹 하나 — 대체 가능한 설비 여러 대와 공용 큐."""

    __slots__ = (
        "gid", "n_tools", "tool_ids", "free", "queue",
        "busy_minutes", "queue_minutes", "queue_entries", "starts", "qlen",
    )

    def __init__(self, gid: str, tool_ids: tuple[str, ...], w0: float, w1: float) -> None:
        self.gid = gid
        self.tool_ids = tool_ids
        self.n_tools = len(tool_ids)
        self.free = list(range(self.n_tools))
        self.queue: deque[Lot] = deque()
        self.busy_minutes = 0.0
        self.queue_minutes = 0.0
        self.queue_entries = 0
        self.starts = 0
        self.qlen = TimeIntegral(w0, w1)


class Simulation:
    """DES 1회 실행.

    `assignment`와 `geo`는 M2에서 쓰이지 않는다(반송이 M3). 인터페이스에 미리 받아
    호출부가 M3에서 바뀌지 않게 한다.
    """

    def __init__(
        self,
        fab: Fab,
        config: SimConfig,
        assignment: Assignment | None = None,
        geo: object | None = None,
    ) -> None:
        self.fab = fab
        self.config = config
        self.assignment = assignment
        self.geo = geo

        self.eq = EventQueue()
        self.streams = RandomStreams(config.seed)
        self.rule = dispatch_rules.get(config.dispatch)
        self._fifo = config.dispatch == "fifo"

        w0, w1 = config.warmup_minutes, config.end_minutes
        self.stations: dict[str, _Station] = {}
        for gid, group in fab.groups.items():
            if group.count <= 0:
                continue
            self.stations[gid] = _Station(gid, group.instances(), w0, w1)

        # 라우트를 그룹 ID 튜플로 미리 펼쳐 둔다 (스텝마다 객체 접근을 피한다)
        self._routes: dict[str, tuple[str, ...]] = {
            pid: tuple(s.group for s in p.steps) for pid, p in fab.products.items()
        }
        self._means: dict[str, tuple[float, ...]] = {}
        self._cvs: dict[str, tuple[float, ...]] = {}
        for pid, p in fab.products.items():
            self._means[pid] = tuple(fab.groups[s.group].process_minutes for s in p.steps)
            cv = config.process_cv_override
            self._cvs[pid] = tuple(
                fab.groups[s.group].process_cv if cv is None else cv for s in p.steps
            )

        self.release = FixedRateRelease(
            config.release_lots_per_day,
            tuple(fab.products),
            tuple(p.mix for p in fab.products.values()),
            config.arrival,
        )

        self.wip = TimeIntegral(w0, w1)
        self._lot_seq = 0
        self._in_system = 0
        self._cycle_times: list[float] = []
        self._raw_process: list[float] = []
        self._censored = 0
        self._completions_in_window = 0
        self._released_in_window = 0
        self._wip_series: list[tuple[float, float]] = []
        self._live: dict[int, Lot] = {}

    # ---- 실행 -----------------------------------------------------------

    def run(self) -> SimResult:
        cfg = self.config
        eq = self.eq
        end = cfg.end_minutes
        w0 = cfg.warmup_minutes

        # 첫 lot은 t=0에 투입한다. 첫 간격만큼 뒤로 미루면 모든 결과가 그만큼
        # 평행이동하고, 등간격 투입에서 "하루 N개"라는 정의와도 어긋난다.
        eq.schedule(0.0, EV_RELEASE, None)
        if cfg.collect_wip_series:
            self._schedule_wip_samples()

        t0 = time.perf_counter()
        while eq:
            kind, payload = eq.pop()
            if eq.now > end:
                break
            if kind == EV_PROC_END:
                self._finish_step(payload)  # type: ignore[arg-type]
            elif kind == EV_RELEASE:
                self._release_lot()
            else:  # WIP 샘플 등 부수 이벤트
                self._sample_wip()
        wall = time.perf_counter() - t0

        now = min(eq.now, end)
        self.wip.finish(now)
        for st in self.stations.values():
            st.qlen.finish(now)

        # 창 안에 투입되었으나 끝나지 않은 lot = 검열 표본
        self._censored = sum(
            1 for lot in self._live.values() if lot.release_time >= w0
        )
        return self._build_result(wall)

    # ---- 이벤트 처리 ----------------------------------------------------

    def _release_lot(self) -> None:
        cfg = self.config
        eq = self.eq
        now = eq.now
        if cfg.max_lots and self._lot_seq >= cfg.max_lots:
            return

        pid = self.release.next_product(self.streams.release)
        self._lot_seq += 1
        lot = Lot(
            lot_id=self._lot_seq,
            pid=pid,
            route=self._routes[pid],
            process_times=draw_process_times(
                self.streams.lot_stream(self._lot_seq), self._means[pid], self._cvs[pid]
            ),
            release_time=now,
        )
        self._live[lot.lot_id] = lot
        self._in_system += 1
        self.wip.set(now, self._in_system)
        if now >= cfg.warmup_minutes:
            self._released_in_window += 1

        # 다음 투입 예약
        eq.schedule(self.release.next_gap(self.streams.release), EV_RELEASE, None)
        self._arrive(lot)

    def _arrive(self, lot: Lot) -> None:
        """lot이 현재 스텝의 설비 그룹 큐에 도착한다.

        M3에서 반송이 붙으면 EV_ARRIVE 이벤트가 이 함수를 호출하게 된다.
        """
        st = self.stations[lot.route[lot.step]]
        lot._step_enqueued = self.eq.now
        st.queue.append(lot)
        st.qlen.set(self.eq.now, len(st.queue))
        self._try_start(st)

    def _try_start(self, st: _Station) -> None:
        """빈 설비가 있는 만큼 큐에서 lot을 꺼내 착수시킨다."""
        eq = self.eq
        now = eq.now
        queue = st.queue
        free = st.free
        w0 = self.config.warmup_minutes
        w1 = self.config.end_minutes

        while free and queue:
            if self._fifo:
                lot = queue.popleft()
            else:
                lot = queue[self.rule.select(queue, now)]
                queue.remove(lot)
            st.qlen.set(now, len(queue))

            wait = now - lot._step_enqueued
            lot.queue_minutes += wait
            if now >= w0:
                st.queue_minutes += wait
                st.queue_entries += 1
                st.starts += 1

            tool_idx = free.pop()
            duration = lot.process_times[lot.step]
            lot.process_minutes += duration
            # M3: 여기서 고장 중단·재개를 처리하게 된다
            st.busy_minutes += overlap(now, now + duration, w0, w1)
            eq.schedule(duration, EV_PROC_END, (st, tool_idx, lot))

    def _finish_step(self, payload: tuple[_Station, int, Lot]) -> None:
        st, tool_idx, lot = payload
        now = self.eq.now
        st.free.append(tool_idx)

        lot.step += 1
        if lot.step >= len(lot.route):
            lot.done_time = now
            self._in_system -= 1
            self.wip.set(now, self._in_system)
            del self._live[lot.lot_id]
            w0 = self.config.warmup_minutes
            if now >= w0:
                self._completions_in_window += 1
                if lot.release_time >= w0:
                    self._cycle_times.append(lot.cycle_time)
                    self._raw_process.append(lot.raw_process_minutes())
        else:
            self._arrive(lot)  # M3: 반송 시간만큼 지연 후 도착하게 된다

        self._try_start(st)

    # ---- WIP 시계열 (선택) ----------------------------------------------

    def _schedule_wip_samples(self) -> None:
        t = 0.0
        step = self.config.wip_sample_minutes
        end = self.config.end_minutes
        while t <= end:
            self.eq.schedule_at(t, 99, None)
            t += step

    def _sample_wip(self) -> None:
        self._wip_series.append((self.eq.now / 1440.0, float(self._in_system)))

    # ---- 결과 -----------------------------------------------------------

    def _build_result(self, wall: float) -> SimResult:
        groups = {}
        for gid, st in self.stations.items():
            groups[gid] = GroupMetrics(
                gid=gid,
                tools=st.n_tools,
                busy_minutes=st.busy_minutes,
                queue_minutes=st.queue_minutes,
                queue_entries=st.queue_entries,
                starts=st.starts,
                mean_queue_len=st.qlen.mean(),
                peak_queue_len=st.qlen.peak,
            )
        return SimResult(
            window_minutes=self.config.window_minutes,
            warmup_minutes=self.config.warmup_minutes,
            released=self._released_in_window,
            completed=len(self._cycle_times),
            censored=self._censored,
            completions_in_window=self._completions_in_window,
            cycle_times=self._cycle_times,
            raw_process_minutes=self._raw_process,
            groups=groups,
            mean_wip=self.wip.mean(),
            peak_wip=self.wip.peak,
            wafers_per_lot=self.fab.wafers_per_lot,
            events=self.eq.popped,
            wall_seconds=wall,
            wip_series=self._wip_series,
        )


def simulate(
    fab: Fab,
    config: SimConfig,
    assignment: Assignment | None = None,
    geo: object | None = None,
) -> SimResult:
    """편의 함수."""
    return Simulation(fab, config, assignment, geo).run()


def replicate(
    fab: Fab,
    config: SimConfig,
    n: int = 3,
    assignment: Assignment | None = None,
    geo: object | None = None,
) -> list[SimResult]:
    """독립 시드로 n회 반복. 신뢰구간 산출용 (`metrics.Replications`)."""
    from dataclasses import replace

    out = []
    for i in range(n):
        out.append(simulate(fab, replace(config, seed=config.seed + i * 7919), assignment, geo))
    return out


ReleaseMode = Literal["fixed_rate"]
"""M2 지원 범위. capacity_search는 M4, conwip은 선택 항목."""
