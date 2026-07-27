"""흐름 기반 배치 구성 휴리스틱.

기준선(`opt/baseline.py`)은 **무엇이 같은 종류인가**로 bay를 나눈다 — 그룹별이든
공정 계열별이든. 여기서는 **무엇이 서로 이어지는가**로 나눈다.

    lot이 A → B로 자주 오가면 A와 B를 같은 bay에 두어야 한다.
    같은 종류인지는 상관없다.

이 차이가 실제로 나타나는 대표 사례가 **트랙과 스캐너**다. lot은 `트랙 → 노광 → 트랙`
순으로 오가므로 둘 사이 왕복이 전체 이동에서 가장 잦다. 그룹별 배치는 트랙을 한 bay에,
스캐너를 다른 bay에 몰아넣어 이 왕복을 전부 bay 교차로 만든다. 공정 계열별 배치는
둘 다 "노광 계열"이라 같은 구역에 넣지만, 계열이 커서 여러 bay로 흘러넘치면 다시
갈라진다.

**두 힘이 맞선다.** 흐름만 보고 bay를 채우면 그룹이 여러 bay로 쪼개지고, 그러면
서버 풀이 분할되어 대기가 늘어난다(M3에서 확인한 pooling 효과). 실측에서 흐름만 보고
채운 배치는 bay 교차를 85% → 80%로 줄였는데도 사이클타임이 8.7% **나빠졌다** —
쪼개진 손해가 교차를 줄인 이득을 넘어섰다.

    bay 교차를 줄여라        → 이어지는 그룹끼리 같은 bay에
    서버 풀을 쪼개지 마라    → 한 그룹은 한 bay에 통째로

**구성 방식** — 그룹을 통째로 유지하는 흐름 친화 배정

    1. 라우트에서 연속한 스텝 쌍의 (그룹A, 그룹B) 빈도를 센다 (제품 믹스 가중)
    2. 총 흐름이 큰 그룹부터, **통째로 들어갈 자리가 있는 bay 중 이미 담긴 그룹들과의
       흐름 합이 가장 큰 bay**에 배정한다
    3. 어느 bay에도 통째로 안 들어가면(그룹이 bay보다 큰 경우) 흐름 친화도가 높은
       bay부터 나눠 담는다
    4. 빈 bay는 spine 중앙에 가까운 순으로 연다

이것은 최적해가 아니라 **좋은 출발점**이다. M6의 국소 탐색이 여기서 시작한다.
"""

from __future__ import annotations

from ..core.geometry import LayoutGeometry, SlotId
from ..core.model import Assignment, Fab
from .baseline import _bay_centrality_order, _bay_slots


def pair_flows(fab: Fab, counts: dict[str, int] | None = None) -> dict[tuple[str, str], float]:
    """연속 스텝 그룹 쌍의 이동 빈도 (lot 1개당, 제품 믹스 가중).

    방향은 구분하지 않는다 — 같은 bay에 있으면 양방향 모두 bay 교차를 면한다.
    """
    flows: dict[tuple[str, str], float] = {}
    for product in fab.products.values():
        for cur, nxt in zip(product.steps, product.steps[1:]):
            if cur.group == nxt.group:
                continue
            key = (cur.group, nxt.group) if cur.group < nxt.group else (nxt.group, cur.group)
            flows[key] = flows.get(key, 0.0) + product.mix
    return flows


def group_flow_totals(fab: Fab) -> dict[str, float]:
    """그룹별 총 흐름 = 그 그룹이 관여하는 모든 쌍 빈도의 합."""
    totals = {gid: 0.0 for gid in fab.groups}
    for (a, b), w in pair_flows(fab).items():
        totals[a] += w
        totals[b] += w
    return totals


def top_pairs(fab: Fab, n: int = 10) -> list[tuple[str, str, float]]:
    """가장 잦은 그룹 쌍. 어떤 쌍을 붙여야 하는지 진단할 때 쓴다."""
    return [
        (a, b, w)
        for (a, b), w in sorted(pair_flows(fab).items(), key=lambda kv: -kv[1])[:n]
    ]


def flow_layout(
    fab: Fab,
    geo: LayoutGeometry,
    counts: dict[str, int] | None = None,
) -> Assignment:
    """흐름이 큰 그룹끼리 같은 bay에 모으는 탐욕 구성."""
    counts = counts or fab.tool_counts
    flows = pair_flows(fab)
    totals = group_flow_totals(fab)

    bays = _bay_centrality_order(geo)
    free: dict[int, list[SlotId]] = {b: _bay_slots(geo, b) for b in bays}
    if sum(counts.values()) > geo.slot_count:
        raise ValueError(
            f"slot 부족: 설비 {sum(counts.values())}대 > slot {geo.slot_count}개"
        )

    def flow_between(a: str, b: str) -> float:
        key = (a, b) if a < b else (b, a)
        return flows.get(key, 0.0)

    order = sorted((g for g in fab.groups if counts[g] > 0),
                   key=lambda g: (-totals[g], g))
    in_bay: dict[int, list[str]] = {b: [] for b in bays}
    placement: dict[str, SlotId] = {}

    def affinity(gid: str, bay: int) -> float:
        return sum(flow_between(gid, p) for p in in_bay[bay])

    for gid in order:
        need = counts[gid]
        # 통째로 들어가는 bay 중 흐름 친화도가 가장 높은 곳. 동률이면 이미 쓰던 bay를
        # 먼저 채워 빈 bay를 아낀다 (bay를 적게 쓸수록 교차가 준다).
        fits = [b for b in bays if len(free[b]) >= need]
        if fits:
            best = max(fits, key=lambda b: (affinity(gid, b), len(in_bay[b]), -bays.index(b)))
            in_bay[best].append(gid)
            for i in range(need):
                placement[f"{gid}#{i + 1}"] = free[best].pop(0)
            continue

        # 그룹이 bay보다 크다 — 친화도 높은 bay부터 나눠 담는다
        placed = 0
        for b in sorted(bays, key=lambda b: (-affinity(gid, b), -len(free[b]))):
            while free[b] and placed < need:
                placed += 1
                placement[f"{gid}#{placed}"] = free[b].pop(0)
            if gid not in in_bay[b] and placed:
                in_bay[b].append(gid)
            if placed >= need:
                break
        if placed < need:  # pragma: no cover - slot 검사를 이미 통과했다
            raise ValueError("slot 부족")
    return Assignment(placement=placement)
