"""DES 실행기 — lot이 라우트를 따라 설비 그룹을 거치는 흐름.

M2에서 설비 그룹·큐·처리시간 변동·디스패칭·투입을 구현했고, M3에서 **배치 설비 ·
설비 고장 · 반송**을 추가했다.

lot 한 스텝의 흐름::

    _request_move(lot)      반송 요청 (반송차가 없으면 대기)
      → EV_ARRIVE           도착 → 큐 진입
        → _try_start        빈 설비가 있으면 디스패칭 규칙으로 착수
          (배치 설비면 batch_size개가 모이거나 max_wait 경과 시 착수)
          → EV_PROC_END     정상 완료
          → EV_TOOL_DOWN    가동시간 기준 고장 → EV_TOOL_UP 후 잔여시간 재개
            → _finish_step  설비 반납, 다음 스텝으로. 라우트 끝이면 출하 스토커로

반송·고장·배치를 모두 끄면(assignment/geo 미지정, 무고장, 단일 설비) M2와 동일하게
동작한다. 검증 테스트가 그 경로를 계속 쓰므로 두 경로 모두 살아 있어야 한다.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from ..core.distance import DistanceMatrix
from ..core.geometry import LayoutGeometry
from ..core.model import Assignment, Fab
from . import dispatch as dispatch_rules
from .batching import DEFAULT_MAX_WAIT_MINUTES, batch_duration
from .breakdown import repair_minutes, time_to_failure
from .engine import (
    EV_ARRIVE,
    EV_BATCH_TIMEOUT,
    EV_PROC_END,
    EV_RELEASE,
    EV_TOOL_UP,
    EventQueue,
)
from .lot import Lot
from .metrics import GroupMetrics, SimResult, TimeIntegral, TransportMetrics, overlap
from .release import Arrival, FixedRateRelease
from .rng import RandomStreams, draw_process_times
from .transport import ENTRY, MoveRequest, TransportPool

EV_WIP_SAMPLE = 99

_TIME_EPS = 1e-6
"""시간 비교 허용오차(분 = 0.06밀리초). 부동소수점 오차로 임계값을 못 넘어
같은 시각에 이벤트를 무한 재예약하는 것을 막는다."""


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
    """모든 그룹의 처리시간 변동계수를 덮어쓴다. 검증 시나리오용."""
    breakdowns: bool = True
    """설비 고장 사용 여부. 끄면 M2와 같은 무고장 동작."""
    batching: bool = True
    """배치 설비의 배치 형성 사용 여부. 끄면 배치 설비도 lot 하나씩 처리한다."""
    batch_max_wait_minutes: float = DEFAULT_MAX_WAIT_MINUTES
    transport: bool = True
    """반송 사용 여부. `assignment`와 `geo`가 주어졌을 때만 켤 수 있다."""
    max_lots: int = 0
    """0이면 무제한."""
    collect_wip_series: bool = False
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


class _BayUnit:
    """한 bay 안에 있는, 같은 그룹의 설비들과 그 bay의 큐.

    **lot은 배달된 bay 안에서만 처리된다.** 반송차가 lot을 어느 bay에 내려놓으면
    그 bay의 intrabay 시스템이 담당하고, 다른 bay의 설비로 넘기려면 interbay 반송이
    한 번 더 필요하다. 그룹의 설비가 여러 bay에 흩어져 있으면 서버 풀이 쪼개지므로
    대기가 늘어난다(pooling 효과) — 동종 설비를 한 bay에 모으는 실무 관행의 근거이자,
    배치 최적화가 다루는 실질적 상충이다.
    """

    __slots__ = ("bay", "tools", "free", "queue", "timeout_armed")

    def __init__(self, bay: int, tools: list[int]) -> None:
        self.bay = bay
        self.tools = tools
        self.free = list(tools)
        self.queue: deque[Lot] = deque()
        self.timeout_armed = False


class _Station:
    """설비 그룹 하나 — bay별 하위 단위로 나뉜다."""

    __slots__ = (
        "gid", "n_tools", "tool_ids", "slot_idx", "units", "unit_of_tool",
        "is_batch", "batch_size", "mtbf_h", "mttr_h", "ttf", "down",
        "busy_minutes", "down_minutes", "queue_minutes", "queue_entries",
        "starts", "batches", "partial_batches", "failures", "qlen",
    )

    def __init__(self, gid: str, tool_ids: tuple[str, ...], w0: float, w1: float) -> None:
        self.gid = gid
        self.tool_ids = tool_ids
        self.n_tools = len(tool_ids)
        self.slot_idx: list[int] = [ENTRY] * self.n_tools
        # 반송이 꺼져 있으면 bay 구분이 없으므로 단위 하나에 전부 담는다
        self.units: dict[int, _BayUnit] = {0: _BayUnit(0, list(range(self.n_tools)))}
        self.unit_of_tool: list[int] = [0] * self.n_tools
        self.is_batch = False
        self.batch_size = 1
        self.mtbf_h = 1e9
        self.mttr_h = 0.0
        self.ttf = [float("inf")] * self.n_tools
        self.down = [False] * self.n_tools
        self.busy_minutes = 0.0
        self.down_minutes = 0.0
        self.queue_minutes = 0.0
        self.queue_entries = 0
        self.starts = 0
        self.batches = 0
        self.partial_batches = 0
        self.failures = 0
        self.qlen = TimeIntegral(w0, w1)

    def partition_by_bay(self, slot_bay: list[int]) -> None:
        """설비를 실제 bay별로 나눈다 (반송이 켜진 경우)."""
        by_bay: dict[int, list[int]] = {}
        for t in range(self.n_tools):
            by_bay.setdefault(slot_bay[self.slot_idx[t]], []).append(t)
        self.units = {bay: _BayUnit(bay, tools) for bay, tools in by_bay.items()}
        for bay, tools in by_bay.items():
            for t in tools:
                self.unit_of_tool[t] = bay

    @property
    def queue_length(self) -> int:
        return sum(len(u.queue) for u in self.units.values())


class Simulation:
    """DES 1회 실행."""

    def __init__(
        self,
        fab: Fab,
        config: SimConfig,
        assignment: Assignment | None = None,
        geo: LayoutGeometry | None = None,
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
        self._w0, self._w1 = w0, w1

        self.stations: dict[str, _Station] = {}
        for gid, group in fab.groups.items():
            if group.count <= 0:
                continue
            st = _Station(gid, group.instances(), w0, w1)
            st.is_batch = config.batching and group.mode == "batch"
            st.batch_size = group.batch_size if st.is_batch else 1
            st.mtbf_h = group.mtbf_h
            st.mttr_h = group.mttr_h
            if config.breakdowns:
                st.ttf = [
                    time_to_failure(self.streams.breakdown, group.mtbf_h)
                    for _ in range(st.n_tools)
                ]
            self.stations[gid] = st

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

        self.pool: TransportPool | None = None
        if config.transport and assignment is not None and geo is not None:
            self._setup_transport(assignment, geo, w0, w1)

        self.wip = TimeIntegral(w0, w1)
        self._lot_seq = 0
        self._in_system = 0
        self._cycle_times: list[float] = []
        self._raw_process: list[float] = []
        self._transport_times: list[float] = []
        self._censored = 0
        self._completions_in_window = 0
        self._released_in_window = 0
        self._wip_series: list[tuple[float, float]] = []
        self._live: dict[int, Lot] = {}

    # ---- 반송 준비 -------------------------------------------------------

    def _setup_transport(
        self, assignment: Assignment, geo: LayoutGeometry, w0: float, w1: float
    ) -> None:
        dm = DistanceMatrix.build(geo)
        self._dm = dm
        slots = dm.slots
        # ENTRY(투입/출하 스토커, spine 왼쪽 끝)까지의 거리
        entry = []
        for s in slots:
            x, y = geo.track_point(s)
            entry.append(abs(x) + abs(y))
        self._entry_dist = entry
        self._slot_bay = [s.bay for s in slots]

        for gid, st in self.stations.items():
            st.slot_idx = [
                dm.index[assignment.placement[inst]] for inst in st.tool_ids
            ]

        for st in self.stations.values():
            st.partition_by_bay(self._slot_bay)

        # 출발 지점 × 목적지 bay마다 (도착 설비, 이동 소요분)을 미리 계산해 둔다.
        # 배달할 bay는 거리만이 아니라 그 bay의 대기까지 보고 고른다 (`_choose_unit`).
        spec = self.fab.transport
        self._options: dict[str, dict[int, tuple[tuple[int, int, float], ...]]] = {}
        for gid, st in self.stations.items():
            table: dict[int, tuple[tuple[int, int, float], ...]] = {}
            for origin in list(range(len(slots))) + [ENTRY]:
                opts = []
                for bay, unit in st.units.items():
                    best = min(
                        (st.slot_idx[t] for t in unit.tools),
                        key=lambda d: self._dist(origin, d),
                    )
                    move = spec.move_seconds(
                        self._dist(origin, best), self._stocker_ops(origin, best)
                    ) / 60.0
                    opts.append((bay, best, move))
                table[origin] = tuple(opts)
            self._options[gid] = table

        self._group_mean_minutes = {
            gid: self.fab.groups[gid].process_minutes for gid in self.stations
        }

        self.pool = TransportPool(
            self.fab.transport.vehicles, self.fab.transport, self._dist, w0, w1
        )

    def _dist(self, a: int, b: int) -> float:
        if a == b:
            return 0.0
        if a == ENTRY:
            return self._entry_dist[b]
        if b == ENTRY:
            return self._entry_dist[a]
        return self._dm.by_index(a, b)

    def _stocker_ops(self, origin: int, dest: int) -> int:
        """통과하는 bay 경계 수. 같은 bay면 0, 다른 bay면 2, 스토커 출입은 1."""
        if origin == ENTRY or dest == ENTRY:
            return 1
        return 0 if self._slot_bay[origin] == self._slot_bay[dest] else 2

    # ---- 실행 -----------------------------------------------------------

    def run(self) -> SimResult:
        cfg = self.config
        eq = self.eq
        end = cfg.end_minutes

        eq.schedule(0.0, EV_RELEASE, None)
        if cfg.collect_wip_series:
            self._schedule_wip_samples()

        t0 = time.perf_counter()
        while eq:
            kind, payload = eq.pop()
            if eq.now > end:
                break
            if kind == EV_PROC_END:
                self._finish_processing(payload)      # type: ignore[arg-type]
            elif kind == EV_ARRIVE:
                self._on_arrive(payload)              # type: ignore[arg-type]
            elif kind == EV_RELEASE:
                self._release_lot()
            elif kind == EV_TOOL_UP:
                self._on_tool_up(payload)             # type: ignore[arg-type]
            elif kind == EV_BATCH_TIMEOUT:
                self._on_batch_timeout(payload)       # type: ignore[arg-type]
            else:
                self._sample_wip()
        wall = time.perf_counter() - t0

        now = min(eq.now, end)
        self.wip.finish(now)
        for st in self.stations.values():
            st.qlen.finish(now)
        self._censored = sum(
            1 for lot in self._live.values() if lot.release_time >= self._w0
        )
        return self._build_result(wall)

    # ---- 투입 -----------------------------------------------------------

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
        if now >= self._w0:
            self._released_in_window += 1

        eq.schedule(self.release.next_gap(self.streams.release), EV_RELEASE, None)
        self._depart(lot, ENTRY)

    # ---- 반송 -----------------------------------------------------------

    def _depart(self, lot: Lot, origin: int) -> None:
        """lot을 현재 위치에서 다음 스텝의 설비 그룹으로 보낸다.

        목적지는 그 그룹의 설비 중 가장 가까운 것이 있는 **bay**다. 도착 후에는 그
        bay의 설비만 이 lot을 처리할 수 있다 (`_BayUnit` 참조).
        """
        if self.pool is None:
            self._enqueue(lot)
            return
        gid = lot.route[lot.step]
        dest_bay, dest = self._choose_unit(gid, origin)
        lot.dest_bay = dest_bay
        req = MoveRequest(lot, origin, dest, self._stocker_ops(origin, dest), self.eq.now)
        assigned = self.pool.submit(self.eq.now, req)
        if assigned is not None:
            vehicle, duration = assigned
            self.eq.schedule(duration, EV_ARRIVE, (lot, vehicle, req.requested_at))

    def _choose_unit(self, gid: str, origin: int) -> tuple[int, int]:
        """어느 bay로 배달할지 고른다 — **최소 예상 지연**(least expected delay).

        거리만 보고 가장 가까운 bay를 고르면, 흩어진 배치에서 한 bay만 포화되고
        나머지가 노는 심한 부하 불균형이 생긴다. 실제 fab의 디스패처도 거리와 대기를
        함께 본다. 여기서는

            점수 = 이동 소요시간 + (대기 lot 수 + 사용 중 설비 수) ÷ 설비 수 × 평균 처리시간

        으로 "이 lot이 처리를 시작하기까지 걸릴 시간"을 근사한다. 그룹의 설비가 한
        bay에 모여 있으면 선택지가 하나뿐이라 이 계산은 건너뛴다.
        """
        st = self.stations[gid]
        opts = self._options[gid][origin]
        if len(opts) == 1:
            bay, dest, _ = opts[0]
            return bay, dest
        mean_proc = self._group_mean_minutes[gid]
        best_bay, best_dest, best_score = opts[0][0], opts[0][1], float("inf")
        for bay, dest, move in opts:
            unit = st.units[bay]
            ahead = len(unit.queue) + (len(unit.tools) - len(unit.free))
            score = move + ahead / len(unit.tools) * mean_proc
            if score < best_score:
                best_bay, best_dest, best_score = bay, dest, score
        return best_bay, best_dest

    def _ship(self, lot: Lot, origin: int) -> None:
        """마지막 설비에서 출하 스토커로. 반송이 꺼져 있으면 즉시 완료."""
        if self.pool is None:
            self._complete(lot)
            return
        req = MoveRequest(lot, origin, ENTRY, self._stocker_ops(origin, ENTRY), self.eq.now)
        assigned = self.pool.submit(self.eq.now, req)
        if assigned is not None:
            vehicle, duration = assigned
            self.eq.schedule(duration, EV_ARRIVE, (lot, vehicle, req.requested_at))

    def _on_arrive(self, payload: tuple[Lot, int, float]) -> None:
        lot, vehicle, requested_at = payload
        now = self.eq.now
        lot.transport_minutes += now - requested_at

        assert self.pool is not None
        nxt = self.pool.release(now, vehicle)
        if nxt is not None:
            req, v2, duration = nxt
            self.eq.schedule(duration, EV_ARRIVE, (req.lot, v2, req.requested_at))

        if lot.done:
            self._complete(lot)
        else:
            self._enqueue(lot)

    # ---- 큐와 착수 -------------------------------------------------------

    def _enqueue(self, lot: Lot) -> None:
        st = self.stations[lot.route[lot.step]]
        unit = st.units.get(lot.dest_bay) or next(iter(st.units.values()))
        lot._step_enqueued = self.eq.now
        unit.queue.append(lot)
        st.qlen.set(self.eq.now, st.queue_length)
        self._try_start(st, unit)

    def _try_start(self, st: _Station, unit: _BayUnit) -> None:
        """빈 설비가 있는 만큼 그 bay의 큐에서 lot(또는 배치)을 꺼내 착수시킨다."""
        eq = self.eq
        now = eq.now
        queue = unit.queue

        while unit.free and queue:
            if st.is_batch:
                members = self._form_batch(st, unit, now)
                if members is None:
                    return
            else:
                if self._fifo:
                    members = [queue.popleft()]
                else:
                    lot = queue[self.rule.select(queue, now)]
                    queue.remove(lot)
                    members = [lot]
            st.qlen.set(now, st.queue_length)

            in_window = now >= self._w0
            for lot in members:
                wait = now - lot._step_enqueued
                lot.queue_minutes += wait
                if in_window:
                    st.queue_minutes += wait
                    st.queue_entries += 1
            if in_window:
                st.starts += len(members)
                st.batches += 1
                if st.is_batch and len(members) < st.batch_size:
                    st.partial_batches += 1

            duration = batch_duration([lot.process_times[lot.step] for lot in members])
            for lot in members:
                lot.process_minutes += duration
            tool_idx = unit.free.pop()
            self._start_processing(st, tool_idx, members, duration)

    def _form_batch(self, st: _Station, unit: _BayUnit, now: float) -> list[Lot] | None:
        """배치를 구성한다. 아직 모을 수 있으면 None을 돌려주고 타임아웃을 건다.

        시간 비교에 허용오차가 필요하다. `enqueued + max_wait` 시각에 타임아웃을 걸어도
        `now - enqueued`가 부동소수점 오차로 `max_wait`에 미세하게 못 미칠 수 있고
        (실측 59.999999999999986 vs 60.0), 그러면 0에 가까운 지연으로 타임아웃을 다시
        걸어 같은 시각에 무한 반복한다. 재예약 지연에도 하한을 둬서 시계가 반드시
        전진하게 한다.
        """
        queue = unit.queue
        if len(queue) >= st.batch_size:
            return [queue.popleft() for _ in range(st.batch_size)]
        max_wait = self.config.batch_max_wait_minutes
        oldest_wait = now - queue[0]._step_enqueued
        if oldest_wait >= max_wait - _TIME_EPS:
            return [queue.popleft() for _ in range(len(queue))]
        if not unit.timeout_armed:
            unit.timeout_armed = True
            self.eq.schedule(
                max(max_wait - oldest_wait, _TIME_EPS), EV_BATCH_TIMEOUT, (st, unit)
            )
        return None

    def _on_batch_timeout(self, payload: tuple[_Station, _BayUnit]) -> None:
        st, unit = payload
        unit.timeout_armed = False
        self._try_start(st, unit)

    # ---- 처리와 고장 -----------------------------------------------------

    def _start_processing(
        self, st: _Station, tool_idx: int, members: list[Lot], remaining: float
    ) -> None:
        """남은 처리시간만큼 돌린다. 도중에 고장이 나면 거기서 끊는다."""
        eq = self.eq
        now = eq.now
        ttf = st.ttf[tool_idx]
        if ttf >= remaining:
            st.ttf[tool_idx] = ttf - remaining
            st.busy_minutes += overlap(now, now + remaining, self._w0, self._w1)
            eq.schedule(remaining, EV_PROC_END, (st, tool_idx, members))
        else:
            # 가동시간 ttf가 지난 시점에 고장 → 잔여 처리시간을 들고 수리에 들어간다
            st.busy_minutes += overlap(now, now + ttf, self._w0, self._w1)
            st.ttf[tool_idx] = 0.0
            repair = repair_minutes(self.streams.repair, st.mttr_h)
            down_start = now + ttf
            st.down[tool_idx] = True
            if now >= self._w0 or down_start >= self._w0:
                st.failures += 1
            st.down_minutes += overlap(down_start, down_start + repair, self._w0, self._w1)
            eq.schedule_at(
                down_start + repair, EV_TOOL_UP, (st, tool_idx, members, remaining - ttf)
            )

    def _on_tool_up(self, payload: tuple[_Station, int, list[Lot], float]) -> None:
        st, tool_idx, members, remaining = payload
        st.down[tool_idx] = False
        st.ttf[tool_idx] = time_to_failure(self.streams.breakdown, st.mtbf_h)
        self._start_processing(st, tool_idx, members, remaining)

    def _finish_processing(self, payload: tuple[_Station, int, list[Lot]]) -> None:
        st, tool_idx, members = payload
        unit = st.units[st.unit_of_tool[tool_idx]]
        unit.free.append(tool_idx)
        origin = st.slot_idx[tool_idx]

        for lot in members:
            lot.step += 1
            if lot.step >= len(lot.route):
                self._ship(lot, origin)
            else:
                self._depart(lot, origin)

        self._try_start(st, unit)

    def _complete(self, lot: Lot) -> None:
        now = self.eq.now
        lot.done_time = now
        self._in_system -= 1
        self.wip.set(now, self._in_system)
        del self._live[lot.lot_id]
        if now >= self._w0:
            self._completions_in_window += 1
            if lot.release_time >= self._w0:
                self._cycle_times.append(lot.cycle_time)
                self._raw_process.append(lot.raw_process_minutes())
                self._transport_times.append(lot.transport_minutes)

    # ---- WIP 시계열 -------------------------------------------------------

    def _schedule_wip_samples(self) -> None:
        t = 0.0
        step = self.config.wip_sample_minutes
        end = self.config.end_minutes
        while t <= end:
            self.eq.schedule_at(t, EV_WIP_SAMPLE, None)
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
                down_minutes=st.down_minutes,
                queue_minutes=st.queue_minutes,
                queue_entries=st.queue_entries,
                starts=st.starts,
                batches=st.batches,
                partial_batches=st.partial_batches,
                failures=st.failures,
                mean_queue_len=st.qlen.mean(),
                peak_queue_len=st.qlen.peak,
            )
        transport = None
        if self.pool is not None:
            transport = TransportMetrics(
                vehicles=self.pool.n,
                busy_minutes=self.pool.busy_minutes,
                wait_minutes=self.pool.wait_minutes,
                moves=self.pool.moves,
                queued_moves=self.pool.queued_moves,
                stocker_ops=self.pool.stocker_ops_total,
                distance_m=self.pool.distance_m,
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
            transport_minutes=self._transport_times,
            groups=groups,
            transport=transport,
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
    geo: LayoutGeometry | None = None,
) -> SimResult:
    return Simulation(fab, config, assignment, geo).run()


def replicate(
    fab: Fab,
    config: SimConfig,
    n: int = 3,
    assignment: Assignment | None = None,
    geo: LayoutGeometry | None = None,
) -> list[SimResult]:
    """독립 시드로 n회 반복. 신뢰구간 산출용 (`metrics.Replications`)."""
    from dataclasses import replace

    return [
        simulate(fab, replace(config, seed=config.seed + i * 7919), assignment, geo)
        for i in range(n)
    ]


ReleaseMode = Literal["fixed_rate"]
"""capacity_search는 M4."""
