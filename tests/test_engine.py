"""이벤트 큐 검증 — 순서와 재현성.

동시각 이벤트가 임의 순서로 나오면 같은 seed에서도 결과가 달라진다. 최적화가 배치
A와 B를 비교할 때 이 흔들림이 실제 차이로 오인되므로, 순서 결정론성은 엔진의
가장 기본적인 요구사항이다.
"""

from __future__ import annotations

import pytest

from fablayout.sim.engine import EV_PROC_END, EV_RELEASE, EventQueue


def test_pops_in_time_order() -> None:
    eq = EventQueue()
    for d in (5.0, 1.0, 3.0, 2.0):
        eq.schedule(d, EV_PROC_END, d)
    seen = []
    while len(eq):
        _, payload = eq.pop()
        seen.append(payload)
    assert seen == [1.0, 2.0, 3.0, 5.0]


def test_clock_advances_to_event_time() -> None:
    eq = EventQueue()
    eq.schedule(7.5, EV_RELEASE, None)
    assert eq.now == 0.0
    eq.pop()
    assert eq.now == 7.5


def test_simultaneous_events_keep_insertion_order() -> None:
    """같은 시각 이벤트는 넣은 순서대로 나와야 한다 — 재현성의 근거."""
    eq = EventQueue()
    for i in range(50):
        eq.schedule(10.0, EV_PROC_END, i)
    order = []
    while len(eq):
        _, payload = eq.pop()
        order.append(payload)
    assert order == list(range(50))


def test_relative_scheduling_is_from_now() -> None:
    eq = EventQueue()
    eq.schedule(10.0, EV_RELEASE, "a")
    eq.pop()                       # now = 10
    eq.schedule(5.0, EV_RELEASE, "b")
    eq.pop()
    assert eq.now == 15.0


def test_schedule_at_absolute() -> None:
    eq = EventQueue()
    eq.schedule_at(42.0, EV_RELEASE, None)
    eq.pop()
    assert eq.now == 42.0


def test_rejects_negative_delay() -> None:
    eq = EventQueue()
    with pytest.raises(ValueError, match="음수 지연"):
        eq.schedule(-1.0, EV_RELEASE, None)


def test_rejects_past_absolute_time() -> None:
    eq = EventQueue()
    eq.schedule(10.0, EV_RELEASE, None)
    eq.pop()
    with pytest.raises(ValueError, match="과거 시각"):
        eq.schedule_at(5.0, EV_RELEASE, None)


def test_counts_popped_events() -> None:
    eq = EventQueue()
    for i in range(17):
        eq.schedule(float(i), EV_PROC_END, i)
    while len(eq):
        eq.pop()
    assert eq.popped == 17


def test_zero_delay_allowed() -> None:
    """반송시간 0(M2)이나 즉시 재시도에 필요하다."""
    eq = EventQueue()
    eq.schedule(0.0, EV_RELEASE, None)
    kind, _ = eq.pop()
    assert kind == EV_RELEASE
    assert eq.now == 0.0
