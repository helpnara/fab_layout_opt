"""테스트 공용 픽스처와 소형 fab 빌더."""

from __future__ import annotations

from fablayout.core.model import Fab, Product, Step, ToolGroup, TransportSpec
from fablayout.sim import SimConfig


def m2_config(**kwargs) -> SimConfig:
    """M3 기능(고장·배치·반송)을 모두 끈 기본 엔진 설정.

    M2 검증 테스트는 "대기가 없으면 X-factor가 1", "실측 ≤ 해석적 상한" 같은 **기본
    엔진 불변식**을 확인한다. 고장이 끼면 저부하에서도 수리 대기가 붙고, 배치가 끼면
    해석적 상한의 전제가 달라져 검증의 의미가 바뀐다. 그래서 명시적으로 끈다.
    M3 기능이 켜진 상태의 검증은 `test_sim_m3.py`가 따로 한다.
    """
    kwargs.setdefault("breakdowns", False)
    kwargs.setdefault("batching", False)
    kwargs.setdefault("transport", False)
    return SimConfig(**kwargs)


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
