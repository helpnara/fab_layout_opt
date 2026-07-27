"""탐색 이웃 연산 — **그룹 단위**로 움직인다.

설비 두 대의 자리를 바꾸는 tool 단위 이동으로는 탐색이 되지 않는다. 실측에서 흐름
기반 배치를 출발점으로 40,000회 무작위 교환을 시도했으나 **개선이 한 번도 나오지
않았다.** 이유는 명확하다 — 무작위 교환은 거의 항상 어떤 그룹을 두 bay로 쪼개고,
서버 풀 분할 벌점이 그 손해를 즉시 되돌려 놓기 때문이다.

    tool 단위 이동   →  그룹을 쪼갠다  →  풀 분할 벌점  →  항상 나빠진다
    그룹 단위 이동   →  풀은 그대로    →  bay 교차만 바뀐다  →  탐색이 된다

그래서 해를 **bay 배치 계획**(bay → 그룹 목록)으로 표현하고, 이 위에서 움직인다.
slot 배정은 계획에서 기계적으로 만들어진다.

이동 연산

    move_group      그룹 하나를 다른 bay로 (자리가 있으면)
    swap_groups     두 그룹의 bay를 맞바꾼다 (양쪽 자리가 맞으면)
    swap_bays       두 bay의 내용을 통째로 맞바꾼다 (중앙성만 바뀐다)
    split_group     큰 그룹을 두 bay로 나눈다 (풀 분할을 감수하고 교차를 줄일 때)
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..core.geometry import LayoutGeometry
from ..core.model import Assignment, Fab
from .baseline import _bay_centrality_order, _bay_slots


@dataclass(frozen=True)
class BayPlan:
    """bay별 그룹 배정 계획. 한 그룹이 여러 bay에 나뉠 수 있다.

    `slots[bay]`는 그 bay가 담은 (그룹, 대수) 목록이다.
    """

    slots: dict[int, list[tuple[str, int]]]
    capacity: dict[int, int]

    @classmethod
    def from_assignment(cls, asg: Assignment, geo: LayoutGeometry) -> BayPlan:
        by_bay: dict[int, dict[str, int]] = {b.bay: {} for b in geo.bays}
        for inst, slot in asg.placement.items():
            gid = Fab.group_of(inst)
            by_bay[slot.bay][gid] = by_bay[slot.bay].get(gid, 0) + 1
        return cls(
            slots={b: list(g.items()) for b, g in by_bay.items()},
            capacity={b.bay: len(_bay_slots(geo, b.bay)) for b in geo.bays},
        )

    def copy(self) -> BayPlan:
        return BayPlan({b: list(v) for b, v in self.slots.items()}, dict(self.capacity))

    def used(self, bay: int) -> int:
        return sum(n for _, n in self.slots[bay])

    def free(self, bay: int) -> int:
        return self.capacity[bay] - self.used(bay)

    def groups_in(self, bay: int) -> list[str]:
        return [g for g, _ in self.slots[bay]]

    def bays_of(self, gid: str) -> list[int]:
        return [b for b, v in self.slots.items() if any(g == gid for g, _ in v)]

    def count_in(self, bay: int, gid: str) -> int:
        return next((n for g, n in self.slots[bay] if g == gid), 0)

    def _set(self, bay: int, gid: str, n: int) -> None:
        rest = [(g, c) for g, c in self.slots[bay] if g != gid]
        if n > 0:
            rest.append((gid, n))
        self.slots[bay] = rest

    def to_assignment(self, geo: LayoutGeometry) -> Assignment:
        """계획을 실제 slot 배정으로 펼친다.

        bay 안에서는 spine에 가까운 자리부터 채운다 — 같은 bay 안의 위치 차이는
        bay 교차에 비하면 작지만, 굳이 멀리 둘 이유도 없다.
        """
        placement = {}
        index: dict[str, int] = {}
        for bay in sorted(self.slots):
            free = _bay_slots(geo, bay)
            for gid, n in self.slots[bay]:
                for _ in range(n):
                    index[gid] = index.get(gid, 0) + 1
                    placement[f"{gid}#{index[gid]}"] = free.pop(0)
        return Assignment(placement)


# ---- 이동 연산 ------------------------------------------------------------


def move_group(plan: BayPlan, rng: random.Random) -> BayPlan | None:
    """한 bay에 있는 그룹 하나를 통째로 다른 bay로 옮긴다."""
    bays = [b for b in plan.slots if plan.slots[b]]
    if not bays:
        return None
    src = rng.choice(bays)
    gid, n = rng.choice(plan.slots[src])
    dests = [b for b in plan.slots if b != src and plan.free(b) >= n]
    if not dests:
        return None
    dst = rng.choice(dests)
    out = plan.copy()
    out._set(src, gid, 0)
    out._set(dst, gid, out.count_in(dst, gid) + n)
    return out


def swap_groups(plan: BayPlan, rng: random.Random) -> BayPlan | None:
    """서로 다른 bay의 두 그룹을 맞바꾼다. 크기가 달라도 자리만 맞으면 된다."""
    entries = [(b, g, n) for b in plan.slots for g, n in plan.slots[b]]
    if len(entries) < 2:
        return None
    (b1, g1, n1), (b2, g2, n2) = rng.sample(entries, 2)
    if b1 == b2 or g1 == g2:
        return None
    if plan.free(b1) + n1 < n2 or plan.free(b2) + n2 < n1:
        return None
    out = plan.copy()
    out._set(b1, g1, 0)
    out._set(b2, g2, 0)
    out._set(b1, g2, out.count_in(b1, g2) + n2)
    out._set(b2, g1, out.count_in(b2, g1) + n1)
    return out


def swap_bays(plan: BayPlan, rng: random.Random) -> BayPlan | None:
    """두 bay의 내용을 통째로 맞바꾼다 — 어느 bay가 중앙에 오는지만 바뀐다."""
    bays = list(plan.slots)
    if len(bays) < 2:
        return None
    b1, b2 = rng.sample(bays, 2)
    if plan.used(b1) > plan.capacity[b2] or plan.used(b2) > plan.capacity[b1]:
        return None
    out = plan.copy()
    out.slots[b1], out.slots[b2] = out.slots[b2], out.slots[b1]
    return out


def split_group(plan: BayPlan, rng: random.Random) -> BayPlan | None:
    """그룹의 설비 일부를 다른 bay로 뗀다.

    풀 분할은 대체로 손해지만, 큰 그룹을 두 곳에 두어 양쪽 흐름을 모두 잡는 것이
    이득인 경우가 있다. 탐색이 그 가능성을 볼 수 있게 열어둔다.
    """
    entries = [(b, g, n) for b in plan.slots for g, n in plan.slots[b] if n >= 2]
    if not entries:
        return None
    src, gid, n = rng.choice(entries)
    take = rng.randint(1, n - 1)
    dests = [b for b in plan.slots if b != src and plan.free(b) >= take]
    if not dests:
        return None
    dst = rng.choice(dests)
    out = plan.copy()
    out._set(src, gid, n - take)
    out._set(dst, gid, out.count_in(dst, gid) + take)
    return out


def merge_group(plan: BayPlan, rng: random.Random) -> BayPlan | None:
    """여러 bay에 흩어진 그룹을 한 bay로 다시 모은다."""
    split = [g for g in {g for b in plan.slots for g, _ in plan.slots[b]}
             if len(plan.bays_of(g)) > 1]
    if not split:
        return None
    gid = rng.choice(split)
    bays = plan.bays_of(gid)
    total = sum(plan.count_in(b, gid) for b in bays)
    targets = [b for b in plan.slots if plan.free(b) + plan.count_in(b, gid) >= total]
    if not targets:
        return None
    dst = rng.choice(targets)
    out = plan.copy()
    for b in bays:
        out._set(b, gid, 0)
    out._set(dst, gid, total)
    return out


OPERATORS = (
    (move_group, 0.30),
    (swap_groups, 0.30),
    (swap_bays, 0.15),
    (merge_group, 0.15),
    (split_group, 0.10),
)


def neighbor(plan: BayPlan, rng: random.Random, tries: int = 8) -> BayPlan | None:
    """가중 확률로 연산자를 골라 이웃 하나를 만든다."""
    ops, weights = zip(*OPERATORS)
    for _ in range(tries):
        op = rng.choices(ops, weights=weights, k=1)[0]
        out = op(plan, rng)
        if out is not None:
            return out
    return None
