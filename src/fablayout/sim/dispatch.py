"""디스패칭 규칙 — 설비가 비었을 때 큐의 어느 lot을 먼저 처리할지.

인터페이스만 열어두고 기본은 FIFO다 (인터뷰 확정 사항). FIFO는 `deque.popleft`로
처리하는 전용 경로가 있어 규칙 객체 호출 비용이 없다.

납기 기반 규칙(EDD·CR)은 lot마다 납기가 있어야 의미가 있는데 현재 데이터셋에는
납기가 없다. 인터페이스 자리만 만들어 두고 구현은 납기 모델 추가 시로 미룬다
(`docs/SPEC.md` §11-C).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from collections import deque

    from .lot import Lot


class DispatchRule(Protocol):
    """큐에서 다음에 처리할 lot의 **인덱스**를 고른다."""

    name: str

    def select(self, queue: "deque[Lot]", now: float) -> int: ...


class Fifo:
    """선입선출. 큐에 들어온 순서대로."""

    name = "fifo"

    def select(self, queue: "deque[Lot]", now: float) -> int:
        return 0


class Srpt:
    """최소 잔여작업(Shortest Remaining Processing Time).

    남은 처리시간이 짧은 lot을 먼저 내보내 평균 사이클타임을 줄인다. 긴 lot이
    계속 밀리는 기아(starvation)가 생길 수 있다.
    """

    name = "srpt"

    def select(self, queue: "deque[Lot]", now: float) -> int:
        best, best_v = 0, float("inf")
        for i, lot in enumerate(queue):
            v = lot.remaining_process_minutes()
            if v < best_v:
                best, best_v = i, v
        return best


class Lifo:
    """후입선출. 사이클타임 분산이 커지는 것을 보여주는 대조군으로만 쓴다."""

    name = "lifo"

    def select(self, queue: "deque[Lot]", now: float) -> int:
        return len(queue) - 1


_RULES: dict[str, type] = {r.name: r for r in (Fifo, Srpt, Lifo)}


def get(name: str) -> DispatchRule:
    try:
        return _RULES[name]()  # type: ignore[return-value]
    except KeyError:
        raise KeyError(
            f"알 수 없는 디스패칭 규칙 '{name}'. 사용 가능: {sorted(_RULES)}"
        ) from None


def available() -> tuple[str, ...]:
    return tuple(sorted(_RULES))
