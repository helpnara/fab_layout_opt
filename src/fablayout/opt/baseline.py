"""기준선(Before) 배치 생성.

**기능별 배치(functional layout)** — 동종 설비를 같은 bay에 모으는 실제 fab의 표준
관행이다. 최적화 결과는 이것과 비교해야 의미가 있다. 무작위 배치를 이기는 것은
성과가 아니다.

배치 순서의 판단 기준은 **방문 횟수**다 (요구작업량이 아니다). 반송 횟수는 방문 횟수에
비례하므로, 이동거리를 줄이려면 자주 방문하는 그룹을 spine 중앙 쪽에 두어야 한다.
처리시간이 긴 그룹은 설비 시간을 많이 쓸 뿐 반송을 더 유발하지는 않는다.

`M5`에서 대리지표와 함께 검증할 예정이나, 배치도 시각화와 M2 이후의 DES 테스트에
기준 배치가 필요하므로 M1에서 미리 구현했다.
"""

from __future__ import annotations

import random
from typing import Literal

from ..core.geometry import LayoutGeometry, SlotId
from ..core.model import Assignment, Fab

BaselineKind = Literal["functional", "spread", "random"]


def _bay_centrality_order(geo: LayoutGeometry) -> list[int]:
    """bay를 채울 순서 — spine 중앙에서 시작해 **이미 고른 bay에 가까운 순**으로 넓혀간다.

    단순히 "중앙에서 가까운 순"으로만 정렬하면 안 된다. 같은 column의 북/남 bay는
    spine 이동거리가 0이라 서로 가장 가까운 쌍인데, 중앙 거리만 보면 반대편 끝 column의
    bay와 동률이 되어 임의로 갈린다. 실제로 기본 기하에서 bay 1(col 0)·bay 4(col 0)·
    bay 3(col 2)이 모두 중앙에서 14m로 동률이며, bay 번호순으로 깨면 bay 1 다음에
    28m 떨어진 bay 3을 골라 bay 4를 비워 두는 결과가 나온다.

    그래서 중앙 최근접 bay를 씨앗으로 잡고, 이후에는 선택된 bay들에 가장 가까운 bay를
    차례로 고른다. 기준선이 불필요하게 나빠지는 것을 막는다.
    """
    mid = geo.spine_length_m / 2
    remaining = sorted(b.bay for b in geo.bays)
    seed = min(remaining, key=lambda bay: (abs(geo.bay_center_x(bay) - mid), bay))
    order = [seed]
    remaining.remove(seed)
    while remaining:
        nxt = min(
            remaining,
            key=lambda bay: (
                min(abs(geo.bay_center_x(bay) - geo.bay_center_x(p)) for p in order),
                abs(geo.bay_center_x(bay) - mid),
                bay,
            ),
        )
        order.append(nxt)
        remaining.remove(nxt)
    return order


def _bay_slots(geo: LayoutGeometry, bay: int) -> list[SlotId]:
    """한 bay의 slot을 spine에 가까운 쪽부터. geo.slots가 이미 pos → side 순이다."""
    return [s for s in geo.slots if s.bay == bay]


def _groups_by_visits(fab: Fab, counts: dict[str, int]) -> list[str]:
    visits = fab.weighted_visits()
    return sorted(
        (gid for gid in fab.groups if counts[gid] > 0),
        key=lambda gid: (-visits[gid], gid),
    )


def functional_layout(
    fab: Fab,
    geo: LayoutGeometry,
    counts: dict[str, int] | None = None,
) -> Assignment:
    """방문 빈도가 높은 그룹을 중앙 bay부터, 그룹 단위로 뭉쳐서 배치한다.

    한 그룹의 설비는 가능한 한 같은 bay에 모은다. 남은 자리가 부족하면 다음 bay로
    넘어가고, 그 bay의 빈자리는 뒤에 오는 더 작은 그룹이 채운다.
    """
    counts = counts or fab.tool_counts
    bays = _bay_centrality_order(geo)
    free: dict[int, list[SlotId]] = {b: _bay_slots(geo, b) for b in bays}

    placement: dict[str, SlotId] = {}
    for gid in _groups_by_visits(fab, counts):
        n = counts[gid]
        target = next((b for b in bays if len(free[b]) >= n), None)
        if target is None:
            # 어느 bay에도 통째로 들어가지 않는다 → 여러 bay에 나눠 담는다.
            remaining = n
            for b in bays:
                while free[b] and remaining:
                    placement[f"{gid}#{n - remaining + 1}"] = free[b].pop(0)
                    remaining -= 1
                if not remaining:
                    break
            if remaining:
                raise ValueError(
                    f"slot 부족: 설비 {sum(counts.values())}대 > slot {geo.slot_count}개"
                )
            continue
        for i in range(n):
            placement[f"{gid}#{i + 1}"] = free[target].pop(0)
    return Assignment(placement=placement)


def spread_layout(
    fab: Fab,
    geo: LayoutGeometry,
    counts: dict[str, int] | None = None,
) -> Assignment:
    """그룹을 여러 bay에 라운드로빈으로 흩뿌린다.

    유틸리티·진동 요건 때문에 동종 설비를 한 곳에 몰지 못하는 fab을 모사한 대안
    기준선이다. 기본 기준선은 `functional_layout`이다.
    """
    counts = counts or fab.tool_counts
    bays = _bay_centrality_order(geo)
    free: dict[int, list[SlotId]] = {b: _bay_slots(geo, b) for b in bays}

    placement: dict[str, SlotId] = {}
    cursor = 0
    for gid in _groups_by_visits(fab, counts):
        for i in range(counts[gid]):
            for _ in range(len(bays)):
                bay = bays[cursor % len(bays)]
                cursor += 1
                if free[bay]:
                    placement[f"{gid}#{i + 1}"] = free[bay].pop(0)
                    break
            else:
                raise ValueError("slot 부족")
    return Assignment(placement=placement)


def random_layout(
    fab: Fab,
    geo: LayoutGeometry,
    counts: dict[str, int] | None = None,
    seed: int = 0,
) -> Assignment:
    """무작위 배치. 최적화기가 동작하는지 확인하는 새니티 체크 전용이다."""
    counts = counts or fab.tool_counts
    instances = [
        f"{gid}#{i + 1}" for gid in fab.groups for i in range(counts[gid])
    ]
    slots = list(geo.slots)
    rng = random.Random(seed)
    rng.shuffle(slots)
    if len(instances) > len(slots):
        raise ValueError("slot 부족")
    return Assignment(placement=dict(zip(instances, slots)))


def build(
    kind: BaselineKind,
    fab: Fab,
    geo: LayoutGeometry,
    counts: dict[str, int] | None = None,
    seed: int = 0,
) -> Assignment:
    if kind == "functional":
        return functional_layout(fab, geo, counts)
    if kind == "spread":
        return spread_layout(fab, geo, counts)
    if kind == "random":
        return random_layout(fab, geo, counts, seed)
    raise ValueError(f"알 수 없는 기준선 종류: {kind}")
