"""SVG 렌더러 검증.

렌더링 결과가 "예쁜지"는 테스트할 수 없다. 테스트할 수 있는 것은 구조적 정확성이다:
모든 설비가 도면에 나타나는지, 좌표가 뷰박스 안에 있는지, XML이 유효한지.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from fablayout.core.analysis import analytic_capacity, flow_report
from fablayout.core.distance import DistanceMatrix
from fablayout.core.geometry import default_geometry
from fablayout.core.model import Fab
from fablayout.data.builtin import build_smallfab21
from fablayout.opt import baseline
from fablayout.viz import svg


@pytest.fixture
def ctx():
    fab = build_smallfab21()
    geo = default_geometry()
    return fab, geo, DistanceMatrix.build(geo), baseline.functional_layout(fab, geo)


def _parse(src: str) -> ET.Element:
    """SVG가 유효한 XML인지 확인하며 파싱한다. 이스케이프 누락도 여기서 잡힌다."""
    return ET.fromstring(src)


def test_layout_svg_is_valid_xml(ctx) -> None:
    fab, geo, _, asg = ctx
    root = _parse(svg.layout_svg(fab, geo, asg, title="테스트"))
    assert root.tag.endswith("svg")
    assert root.get("viewBox")


def test_layout_svg_contains_every_tool(ctx) -> None:
    """설비 21대 전부가 도면에 라벨로 나타나야 한다."""
    fab, geo, _, asg = ctx
    out = svg.layout_svg(fab, geo, asg)
    for gid, n in fab.tool_counts.items():
        if n:
            assert gid in out, f"{gid}가 배치도에 없다"
    # 각 설비 인스턴스마다 툴팁(<title>)이 있어야 한다
    root = _parse(out)
    titles = [t.text or "" for t in root.iter("{http://www.w3.org/2000/svg}title")]
    for inst in fab.tool_instances():
        assert any(t.startswith(inst + " ") for t in titles), f"{inst} 툴팁 없음"


def test_layout_svg_marks_empty_slots(ctx) -> None:
    fab, geo, _, asg = ctx
    root = _parse(svg.layout_svg(fab, geo, asg))
    titles = [t.text or "" for t in root.iter("{http://www.w3.org/2000/svg}title")]
    empties = [t for t in titles if "빈 자리" in t]
    assert len(empties) == geo.slot_count - fab.total_tools == 9


def test_layout_svg_shows_all_bays(ctx) -> None:
    fab, geo, _, asg = ctx
    out = svg.layout_svg(fab, geo, asg)
    for b in geo.bays:
        assert f"BAY {b.bay}" in out


def test_layout_coordinates_within_viewbox(ctx) -> None:
    """좌표가 뷰박스를 벗어나면 잘려 보이지 않는다."""
    fab, geo, _, asg = ctx
    root = _parse(svg.layout_svg(fab, geo, asg, title="T"))
    _, _, vw, vh = (float(v) for v in root.get("viewBox").split())
    ns = "{http://www.w3.org/2000/svg}"
    for r in root.iter(f"{ns}rect"):
        x, y = float(r.get("x")), float(r.get("y"))
        w, h = float(r.get("width")), float(r.get("height"))
        assert -30 <= x and x + w <= vw + 1, f"rect x 범위 이탈: {x}..{x + w}"
        assert -1 <= y and y + h <= vh + 1, f"rect y 범위 이탈: {y}..{y + h}"


def test_capacity_bars(ctx) -> None:
    fab, *_ = ctx
    rep = analytic_capacity(fab)
    out = svg.capacity_bars_svg(fab, rep)
    _parse(out)
    for g in fab.groups:
        assert g in out
    assert rep.bottleneck.gid in out
    assert "병목" in out


def test_capacity_bars_bottleneck_uses_status_color(ctx) -> None:
    """병목은 status critical 색으로만 강조한다 (범주 색 재사용 금지)."""
    fab, *_ = ctx
    out = svg.capacity_bars_svg(fab, analytic_capacity(fab))
    assert out.count("--fl-critical") >= 1


def test_transport_stack(ctx) -> None:
    fab, geo, dm, asg = ctx
    reports = {
        "기능별": flow_report(fab, geo, asg, dm),
        "흩뿌림": flow_report(fab, geo, baseline.spread_layout(fab, geo), dm),
        "무작위": flow_report(fab, geo, baseline.random_layout(fab, geo, seed=3), dm),
    }
    rate = analytic_capacity(fab).capacity_lots_per_day
    out = svg.transport_stack_svg(reports, rate, fab.transport.vehicles)
    _parse(out)
    for label in reports:
        assert label in out
    for name, _ in svg._LOAD_PARTS:
        assert name in out  # 범례가 4항 모두 표기


def test_distance_heatmap(ctx) -> None:
    _, geo, dm, _ = ctx
    root = _parse(svg.distance_heatmap_svg(dm, geo))
    ns = "{http://www.w3.org/2000/svg}"
    titles = [t.text or "" for t in root.iter(f"{ns}title")]
    # 30×30 셀마다 툴팁
    assert len([t for t in titles if "→" in t]) == len(dm) ** 2


def test_svg_escapes_titles(ctx) -> None:
    """제목에 XML 특수문자가 들어와도 문서가 깨지지 않아야 한다."""
    fab, geo, _, asg = ctx
    out = svg.layout_svg(fab, geo, asg, title='A & B <c> "d"')
    _parse(out)
    assert "&amp;" in out


def test_theme_variables_have_light_fallbacks() -> None:
    """모든 색 참조는 var(--이름, 폴백) 형태여야 한다 (단독 열람 시 대비)."""
    for name in dir(svg):
        val = getattr(svg, name)
        if isinstance(val, str) and val.startswith("var("):
            assert "," in val, f"{name}에 폴백 색이 없다"


def test_no_hardcoded_group_colors(ctx) -> None:
    """설비 그룹별 고정 색상은 쓰지 않는다 (범주 색 8개 한도 초과 방지)."""
    fab, *_ = ctx
    assert not hasattr(fab.groups["PHOTO"], "color")
    assert fab.groups["PHOTO"].family == "노광"
