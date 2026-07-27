"""테스트 공용 픽스처와 소형 fab 빌더."""

from __future__ import annotations

from fablayout.core.model import Fab, Product, Step, ToolGroup, TransportSpec


def make_fab(
    groups: list[tuple[str, int, float]],
    route: list[str],
    cv: float = 0.15,
    name: str = "test",
) -> Fab:
    """검증용 소형 fab.

    `groups`는 (그룹ID, 대수, 처리시간분), `route`는 그룹 ID의 순서 목록이다.
    배치 설비·고장은 넣지 않는다 — M2 엔진 범위와 맞춘다.
    """
    gs = {
        gid: ToolGroup(gid, gid, n, minutes, "single", 1, 1e9, 0.0, cv, "test")
        for gid, n, minutes in groups
    }
    steps = tuple(Step(i, gid, 1) for i, gid in enumerate(route))
    return Fab(
        name=name,
        groups=gs,
        products={"P": Product("P", "P", steps, 1.0)},
        wafers_per_lot=25,
        transport=TransportSpec(),
        source="test fixture",
    )


def single_server_fab(process_minutes: float = 60.0, cv: float = 0.15) -> Fab:
    """M/G/1 대조용 — 설비 1대, 스텝 1개."""
    return make_fab([("S", 1, process_minutes)], ["S"], cv=cv, name="mg1")
