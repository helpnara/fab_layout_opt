"""SVG 렌더러 — 의존성 없이 배치도와 지표 도표를 만든다.

색상은 CSS 커스텀 프로퍼티로 참조하고 라이트 모드 값을 폴백으로 둔다. 페이지가 변수를
정의하면 다크 모드가 따라오고, SVG 파일을 단독으로 열면 폴백이 쓰인다.

설계 규칙 (dataviz 스킬 기준)
- 설비 그룹이 11개다. 그룹마다 색을 배정하면 구별 가능한 범주 색 수(8)를 넘겨 색맹
  안전성이 깨진다. 따라서 **정체성은 텍스트 라벨**이 담당하고, **색은 크기(순차 램프)**만
  표현한다.
- 범주 색을 쓰는 곳은 검증을 통과한 조합만 사용한다.
    반송 부하 4분해 (스택)  : 슬롯 1~4, 인접 페어리스트 통과 (light/dark)
    기준선 3종 비교        : 슬롯 1~3, 전체 페어리스트 통과 (light/dark)
- 라이트 모드에서 3:1 미달인 색이 있으므로 모든 도표에 **직접 수치 라벨**을 붙인다.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from ..core.analysis import CapacityReport, FlowReport
from ..core.distance import DistanceMatrix
from ..core.geometry import LayoutGeometry, SlotId
from ..core.model import Assignment, Fab

# ---- 색 역할 -------------------------------------------------------------
# var(--이름, 폴백) 형태로 참조한다. 폴백은 라이트 모드 값이다.

INK = "var(--fl-ink, #0b0b0b)"
INK2 = "var(--fl-ink-2, #52514e)"
INK3 = "var(--fl-ink-3, #7a7975)"
SURFACE = "var(--fl-surface, #fcfcfb)"
SURFACE2 = "var(--fl-surface-2, #f4f3f0)"
LINE = "var(--fl-line, #dcdad4)"
GRID = "var(--fl-grid, #e8e6e1)"

# 순차 램프 (blue). 서수 바닥 규칙: 라이트는 step 250 이하로 밝게 가지 않는다.
SEQ = tuple(f"var(--fl-seq-{i}, {hexv})" for i, hexv in enumerate(
    ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#104281")
))
# 범주 슬롯 1~4 (검증 통과 조합)
CAT = tuple(f"var(--fl-cat-{i + 1}, {hexv})" for i, hexv in enumerate(
    ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
))
CRITICAL = "var(--fl-critical, #d03b3b)"
GOOD = "var(--fl-good, #0ca30c)"

FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Apple SD Gothic Neo', "
    "'Noto Sans KR', 'Malgun Gothic', sans-serif"
)


def _e(s: object) -> str:
    return escape(str(s), quote=True)


def _n(v: float) -> str:
    """SVG 좌표를 짧게. 불필요한 소수점을 지운다."""
    return f"{v:.2f}".rstrip("0").rstrip(".")


@dataclass
class _Svg:
    width: float
    height: float
    parts: list[str]
    aria: str = ""

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(
        self,
        x: float,
        y: float,
        s: object,
        size: float = 12,
        fill: str = INK,
        anchor: str = "start",
        weight: str = "400",
        opacity: float = 1.0,
    ) -> None:
        op = "" if opacity >= 1.0 else f' opacity="{opacity}"'
        self.add(
            f'<text x="{_n(x)}" y="{_n(y)}" font-size="{_n(size)}" fill="{fill}"'
            f' text-anchor="{anchor}" font-weight="{weight}"{op}>{_e(s)}</text>'
        )

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: str = "none",
        stroke: str = "none",
        rx: float = 0,
        sw: float = 1,
        title: str | None = None,
    ) -> None:
        t = f"<title>{_e(title)}</title>" if title else ""
        close = f">{t}</rect>" if t else "/>"
        self.add(
            f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}"'
            f' fill="{fill}" stroke="{stroke}" stroke-width="{_n(sw)}" rx="{_n(rx)}"{close}'
        )

    def line(self, x1: float, y1: float, x2: float, y2: float,
             stroke: str = LINE, sw: float = 1, dash: str = "") -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(
            f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}"'
            f' stroke="{stroke}" stroke-width="{_n(sw)}"{d}/>'
        )

    def render(self) -> str:
        label = f' role="img" aria-label="{_e(self.aria)}"' if self.aria else ""
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_n(self.width)}'
            f' {_n(self.height)}" width="100%" style="max-width:{_n(self.width)}px;'
            f'height:auto;font-family:{FONT}"{label}>'
            + "".join(self.parts)
            + "</svg>"
        )


def _bin_index(value: float, lo: float, hi: float, bins: int) -> int:
    if hi <= lo:
        return 0
    t = (value - lo) / (hi - lo)
    return max(0, min(bins - 1, int(t * bins - 1e-9)))


# =========================================================================
# 배치도
# =========================================================================


def short_codes(fab: Fab) -> dict[str, str]:
    """설비 그룹의 2~3자 약호. 도면 박스가 좁아 전체 ID가 들어가지 않는다.

    `ETCH_DRY` → `ED`, `PHOTO` → `PH`. 충돌하면 한 자를 더 붙인다.
    실제 fab 도면도 장비를 짧은 코드로 표기한다.
    """
    out: dict[str, str] = {}
    used: set[str] = set()
    for gid in fab.groups:
        parts = gid.split("_")
        base = "".join(p[0] for p in parts) if len(parts) > 1 else gid[:2]
        code = base.upper()
        extra = 1
        while code in used:
            src = gid.replace("_", "")
            code = (base + src[len(base) + extra - 1: len(base) + extra]).upper()
            extra += 1
            if extra > len(gid):
                code = gid[:3].upper()
                break
        used.add(code)
        out[gid] = code
    return out


def layout_svg(
    fab: Fab,
    geo: LayoutGeometry,
    assignment: Assignment,
    title: str = "",
    scale: float = 14.0,
) -> str:
    """Bay 구조 배치도.

    bay별 반송 부하는 **배경 음영이 아니라 bay 라벨 옆의 막대 + 숫자**로 표시한다.
    배경을 음영 처리하면 흰 설비 박스가 대부분을 덮어 빈 자리만 진하게 보이고, 그
    빈 자리가 특수한 설비처럼 오독된다.
    """
    x0, _, x1, y1 = geo.extent()
    codes = short_codes(fab)
    visits = fab.weighted_visits()
    bay_load = {
        b.bay: sum(
            visits[Fab.group_of(inst)]
            for inst, slot in assignment.placement.items()
            if slot.bay == b.bay
        )
        for b in geo.bays
    }
    hi_load = max(bay_load.values()) or 1.0

    pad_x, pad_top, label_h = 46.0, 22.0 + (26 if title else 0), 52.0
    legend_h = 84.0
    plot_w = (x1 - x0) * scale
    W = max(plot_w + pad_x * 2, 660.0)
    H = pad_top + label_h + (y1 * 2) * scale + label_h + legend_h
    origin_x = (W - plot_w) / 2
    mid_y = pad_top + label_h + y1 * scale  # spine 중심선(y=0)의 화면 좌표

    def px(x: float) -> float:
        return origin_x + (x - x0) * scale

    def py(y: float) -> float:
        return mid_y - y * scale

    s = _Svg(W, H, [], aria=f"{title or 'fab 배치도'} — bay {len(geo.bays)}개, "
                           f"설비 {len(assignment.placement)}대, slot {geo.slot_count}개")
    s.rect(0, 0, W, H, fill=SURFACE)
    if title:
        s.text(pad_x - 30, 22, title, size=14, weight="600")

    # spine
    sw = geo.spine_width_m
    s.rect(px(x0) - 34, py(sw / 2), plot_w + 34, sw * scale, fill=SURFACE2, stroke=LINE, sw=1)
    s.line(px(x0) - 34, mid_y, px(x1), mid_y, stroke=INK3, sw=1.5, dash="7 5")
    s.text(px(x1) - 4, mid_y - 7, "SPINE 간선 궤도", size=9, fill=INK3, anchor="end")

    # 투입/출하 스토커
    s.rect(px(x0) - 32, mid_y - 15, 26, 30, fill=SURFACE, stroke=INK2, rx=3,
           title="투입/출하 스토커 — 라우트의 시작점과 종점 (x=0)")
    s.text(px(x0) - 19, mid_y + 4, "ST", size=9.5, fill=INK, anchor="middle", weight="700")

    tools_by_slot: dict[SlotId, str] = {v: k for k, v in assignment.placement.items()}
    blen = geo.bay_length_m
    depth, aisle, pitch = geo.tool_depth_m, geo.aisle_width_m, geo.slot_pitch_m

    for b in sorted(geo.bays, key=lambda z: z.bay):
        cx = geo.bay_center_x(b.bay)
        north = b.bay_side == "N"
        y_near = sw / 2 if north else -sw / 2
        y_far = y_near + (blen if north else -blen)
        bx = cx - geo.bay_width_m / 2
        occupied = sum(1 for sl in tools_by_slot if sl.bay == b.bay)

        s.rect(px(bx), py(max(y_near, y_far)), geo.bay_width_m * scale, blen * scale,
               fill=SURFACE2, stroke=LINE, sw=1, rx=3,
               title=f"BAY {b.bay} ({b.name}) — 설비 {occupied}/{2 * geo.positions_per_side}대,"
                     f" 반송 부하 {bay_load[b.bay]:.1f}회/lot")
        # bay 중앙 통로 = 궤도가 지나는 곳
        s.rect(px(cx - aisle / 2), py(max(y_near, y_far)), aisle * scale, blen * scale,
               fill=SURFACE)
        s.line(px(cx), py(y_near), px(cx), py(y_far), stroke=INK3, sw=1, dash="3 4")
        # bay 입구 (스토커 접속점)
        s.rect(px(cx) - 7, py(y_near) - (3 if north else 0), 14, 3, fill=INK2,
               title=f"BAY {b.bay} 입구 스토커")

        # bay 라벨 블록: bay 밖(먼 끝)에 두고, 쌓는 방향을 bay 방향에 맞춘다.
        # 북측 bay는 위로, 남측 bay는 아래로 쌓아야 블록이 bay 안으로 들어가지 않는다.
        edge = py(y_far)
        if north:
            y_label, y_bar, y_count = edge - 30, edge - 21, edge - 6
        else:
            y_label, y_bar, y_count = edge + 20, edge + 26, edge + 44
        s.text(px(cx), y_label, f"BAY {b.bay}", size=11, fill=INK,
               anchor="middle", weight="700")
        bw = 56.0
        s.rect(px(cx) - bw / 2, y_bar, bw, 5, fill=SURFACE2, rx=2.5)
        s.rect(px(cx) - bw / 2, y_bar, bw * bay_load[b.bay] / hi_load, 5,
               fill=SEQ[_bin_index(bay_load[b.bay], 0.0, hi_load, len(SEQ))], rx=2.5,
               title=f"BAY {b.bay} 반송 부하 {bay_load[b.bay]:.1f}회/lot")
        s.text(px(cx), y_count, f"{bay_load[b.bay]:.0f}회/lot · {occupied}대",
               size=8.5, fill=INK3, anchor="middle")

        for pos in range(geo.positions_per_side):
            for side in ("L", "R"):
                slot = SlotId(b.bay, side, pos)  # type: ignore[arg-type]
                tx, ty = geo.tool_center(slot)
                rx0, ry0 = px(tx - depth / 2) + 1.5, py(ty + pitch / 2) + 1.5
                w, h = depth * scale - 3, pitch * scale - 3
                inst = tools_by_slot.get(slot)
                if inst is None:
                    s.rect(rx0, ry0, w, h, fill="none", stroke=LINE, sw=1, rx=2,
                           title=f"{slot} — 빈 자리 (증설 여유)")
                    s.text(rx0 + w / 2, ry0 + h / 2 + 4, "빈자리", size=8,
                           fill=INK3, anchor="middle")
                    continue
                gid = Fab.group_of(inst)
                g = fab.groups[gid]
                batch = f" ×{g.batch_size}" if g.mode == "batch" else ""
                s.rect(rx0, ry0, w, h, fill=SURFACE, stroke=INK2, sw=1.3, rx=2,
                       title=f"{inst} · {g.name} · {g.family} · "
                             f"{'배치' if g.mode == 'batch' else '단일'}{batch} · "
                             f"{g.process_minutes:.0f}분/회 · 가동률 {g.availability:.0%} · "
                             f"방문 {visits[gid]:.1f}회/lot · {slot}")
                if g.mode == "batch":  # 배치 설비: 상단 굵은 선
                    s.line(rx0 + 3, ry0 + 3, rx0 + w - 3, ry0 + 3, stroke=INK, sw=2.5)
                s.text(rx0 + w / 2, ry0 + h / 2 - 1, codes[gid], size=15,
                       fill=INK, anchor="middle", weight="700")
                s.text(rx0 + w / 2, ry0 + h / 2 + 13, f"#{inst.split('#')[1]}", size=8.5,
                       fill=INK3, anchor="middle")

    # ---- 범례: 약호표 2행 + 기호 1행
    lx0 = pad_x - 30
    ly0 = H - legend_h + 16
    s.text(lx0, ly0, "약호", size=9, fill=INK2, weight="600")
    per_row = 6
    col_w = (W - lx0 - pad_x + 30 - 34) / per_row
    for i, (gid, code) in enumerate(codes.items()):
        gx = lx0 + 34 + (i % per_row) * col_w
        gy = ly0 + (i // per_row) * 15
        s.text(gx, gy, code, size=9, fill=INK, weight="700")
        # 괄호 부연은 떼고 핵심 명칭만 — 좁은 칸에서 단어 중간이 잘리는 것을 막는다
        s.text(gx + 20, gy, fab.groups[gid].name.split(" (")[0], size=9, fill=INK3)
    sy = ly0 + 38
    s.line(lx0, sy - 12, W - pad_x + 30, sy - 12, stroke=GRID, sw=1)
    s.text(lx0, sy + 2,
           "상단 굵은선 = 배치 설비  ·  점선 = bay 중앙 궤도  ·  "
           "막대 = bay별 반송 부하(방문 횟수/lot)  ·  ST = 투입/출하 스토커",
           size=9, fill=INK2)
    return s.render()


# =========================================================================
# 그룹별 생산능력
# =========================================================================


def capacity_bars_svg(
    fab: Fab,
    report: CapacityReport,
    title: str = "설비 그룹별 생산능력 (해석적 상한)",
    width: float = 720,
    row: float = 26,
) -> str:
    groups = list(report.groups)
    left, right, top = 128.0, 82.0, 58.0
    H = top + len(groups) * row + 52
    plot_w = width - left - right
    hi = max(g.capacity_lots_per_day for g in groups)
    limit = report.capacity_lots_per_day

    s = _Svg(width, H, [], aria=f"{title}. 병목은 {report.bottleneck.gid}.")
    s.rect(0, 0, width, H, fill=SURFACE)
    s.text(16, 22, title, size=14, weight="600")
    s.text(16, 39, f"막대 = 그룹 단독 능력 · 파선 = fab 병목 {limit:.2f} lot/일"
                   f" ({limit * fab.wafers_per_lot:.0f} 웨이퍼/일)", size=10, fill=INK2)

    for gx in range(0, 5):
        v = hi * gx / 4
        x = left + plot_w * gx / 4
        s.line(x, top - 6, x, top + len(groups) * row - 4, stroke=GRID, sw=1)
        s.text(x, top + len(groups) * row + 12, f"{v:.0f}", size=9,
               fill=INK3, anchor="middle")
    s.text(left + plot_w / 2, top + len(groups) * row + 28, "lot/일", size=9,
           fill=INK2, anchor="middle")

    for i, g in enumerate(groups):
        y = top + i * row
        bh = row - 9
        w = plot_w * g.capacity_lots_per_day / hi
        is_bottleneck = g.gid == report.bottleneck.gid
        near = g.capacity_lots_per_day <= limit * 1.2
        fill = CRITICAL if is_bottleneck else (SEQ[4] if near else SEQ[1])
        s.text(left - 8, y + bh / 2 + 4, f"{g.gid} ×{g.tools}", size=10,
               fill=INK, anchor="end", weight="600" if is_bottleneck else "400")
        s.rect(left, y, w, bh, fill=fill, rx=3,
               title=f"{g.gid} ({g.name}) — 능력 {g.capacity_lots_per_day:.2f} lot/일,"
                     f" 요구 {g.workload_min_per_lot:.0f}분/lot,"
                     f" 방문 {g.visits_per_lot:.1f}회/lot,"
                     f" 가동률 A={g.availability:.0%},"
                     f" 병목 대비 {limit / g.capacity_lots_per_day:.0%}")
        s.text(left + w + 6, y + bh / 2 + 4, f"{g.capacity_lots_per_day:.2f}", size=10, fill=INK)
        s.text(left + w + 46, y + bh / 2 + 4,
               f"{limit / g.capacity_lots_per_day:.0%}", size=9, fill=INK3)

    xb = left + plot_w * limit / hi
    s.line(xb, top - 2, xb, top + len(groups) * row + 2, stroke=INK, sw=1.5, dash="5 3")
    # 부제 줄(y=39)과 겹치지 않도록 첫 막대 바로 위에 둔다
    s.text(xb + 5, top - 5, "병목", size=9, fill=INK, weight="600")
    return s.render()


# =========================================================================
# 반송 부하 분해
# =========================================================================

_LOAD_PARTS = (
    ("고정 상하차", "handling_seconds_per_lot"),
    ("주행", "travel_seconds_per_lot"),
    ("스토커 입출고", "stocker_seconds_per_lot"),
    ("빈차 회송", "empty_seconds_per_lot"),
)


def transport_stack_svg(
    reports: dict[str, FlowReport],
    lots_per_day: float,
    vehicles: int,
    title: str = "배치별 반송차 부하 분해",
    width: float = 720,
    row: float = 56,
) -> str:
    """배치안별 lot당 반송차 점유시간을 4항으로 스택. 가동률을 함께 표기한다."""
    left, right, top = 92.0, 118.0, 74.0
    H = top + len(reports) * row + 34
    plot_w = width - left - right
    hi = max(r.vehicle_seconds_per_lot for r in reports.values()) / 3600

    s = _Svg(width, H, [], aria=title)
    s.rect(0, 0, width, H, fill=SURFACE)
    s.text(16, 22, title, size=14, weight="600")
    s.text(16, 39, f"lot 1개가 요구하는 반송차 점유시간 · 투입 {lots_per_day:.2f} lot/일"
                   f" · 반송차 {vehicles}대", size=10, fill=INK2)
    # 범례 (직접 라벨과 함께 — 라이트 모드 대비 미달 색이 있으므로 필수)
    lx = 16.0
    for i, (name, _) in enumerate(_LOAD_PARTS):
        s.rect(lx, 48, 10, 10, fill=CAT[i], rx=2)
        s.text(lx + 14, 57, name, size=9.5, fill=INK2)
        lx += 16 + len(name) * 9.5

    for i, (label, r) in enumerate(reports.items()):
        y = top + i * row
        bh = 26.0
        util = r.vehicle_utilization(lots_per_day, vehicles)
        s.text(left - 8, y + bh / 2 + 4, label, size=10.5, fill=INK,
               anchor="end", weight="600")
        x = left
        for j, (name, attr) in enumerate(_LOAD_PARTS):
            secs = getattr(r, attr)
            w = plot_w * (secs / 3600) / hi
            s.rect(x, y, max(w - 2, 0), bh, fill=CAT[j], rx=2,
                   title=f"{label} — {name} {secs / 3600:.2f}h/lot"
                         f" ({secs / r.vehicle_seconds_per_lot:.0%})")
            if w > 34:
                s.text(x + w / 2 - 1, y + bh / 2 + 4, f"{secs / 3600:.1f}",
                       size=9, fill=SURFACE, anchor="middle", weight="600")
            x += w
        total = r.vehicle_seconds_per_lot / 3600
        s.text(x + 6, y + bh / 2 + 4, f"{total:.1f}h", size=10, fill=INK, weight="600")
        band = GOOD if 0.55 <= util <= 0.85 else CRITICAL
        s.rect(x + 42, y + 4, 7, 7, fill=band, rx=1.5)
        s.text(x + 53, y + bh / 2 + 4, f"가동률 {util:.0%}", size=9.5, fill=INK2)
        s.text(left, y + bh + 15,
               f"bay 교차 {r.interbay_moves_per_lot:.0f}/{r.moves_per_lot:.0f}회"
               f" ({r.interbay_fraction:.0%}) · 스토커 조작 {r.stocker_ops_per_lot:.0f}회"
               f" · 총 주행거리 {r.distance_m_per_lot / 1000:.2f}km", size=9, fill=INK3)

    s.text(16, H - 12, "가동률 옆 표식: 초록 = 민감 구간(55~85%) — 이 안에 있어야 "
                       "배치 변경이 처리량에 나타난다", size=9, fill=INK2)
    return s.render()


# =========================================================================
# 부하-사이클타임 곡선
# =========================================================================


def load_curve_svg(
    points: list[tuple[float, float, float, float]],
    capacity_lots_per_day: float,
    title: str = "투입률에 따른 거동",
    width: float = 720,
    panel_h: float = 168,
) -> str:
    """(투입률 비율, X-factor, 달성 처리량, 병목 가동률) 곡선.

    X-factor와 처리량은 단위와 범위가 달라 한 축에 겹칠 수 없다. 축을 두 개 그리는
    대신 **패널을 위아래로 나누고 x축을 공유**한다 (이중축 금지).
    """
    left, right, top, gap = 58.0, 86.0, 60.0, 40.0
    H = top + panel_h * 2 + gap + 52
    plot_w = width - left - right
    xs = [p[0] for p in points]
    x_lo, x_hi = min(xs), max(xs)

    def px(v: float) -> float:
        return left + plot_w * (v - x_lo) / (x_hi - x_lo) if x_hi > x_lo else left

    s = _Svg(width, H, [], aria=f"{title} — 투입률 대 X-factor 및 처리량")
    s.rect(0, 0, width, H, fill=SURFACE)
    s.text(16, 22, title, size=14, weight="600")
    s.text(16, 39, f"가로축 = 해석적 병목 능력({capacity_lots_per_day:.2f} lot/일) 대비 투입률"
                   " · 두 패널은 x축을 공유한다", size=10, fill=INK2)

    def panel(y0: float, values: list[float], label: str, fmt: str,
              baseline: float | None, baseline_label: str) -> None:
        v_hi = max(values) * 1.12
        v_lo = min(min(values), baseline if baseline else min(values)) * 0.92

        def py(v: float) -> float:
            return y0 + panel_h - panel_h * (v - v_lo) / (v_hi - v_lo)

        s.rect(left, y0, plot_w, panel_h, fill="none", stroke=GRID, sw=1)
        s.text(left, y0 - 7, label, size=10.5, fill=INK, weight="600")
        for k in range(4):
            v = v_lo + (v_hi - v_lo) * k / 3
            s.line(left, py(v), left + plot_w, py(v), stroke=GRID, sw=1)
            s.text(left - 6, py(v) + 3.5, f"{v:{fmt}}", size=9, fill=INK3, anchor="end")
        if baseline is not None:
            s.line(left, py(baseline), left + plot_w, py(baseline),
                   stroke=INK3, sw=1.5, dash="5 3")
            s.text(left + plot_w + 5, py(baseline) + 3.5, baseline_label,
                   size=9, fill=INK3)
        pts = " ".join(f"{_n(px(x))},{_n(py(v))}" for x, v in zip(xs, values))
        s.add(f'<polyline points="{pts}" fill="none" stroke="{SEQ[4]}" '
              f'stroke-width="2" stroke-linejoin="round"/>')
        for x, v in zip(xs, values):
            s.add(f'<circle cx="{_n(px(x))}" cy="{_n(py(v))}" r="4" fill="{SEQ[4]}" '
                  f'stroke="{SURFACE}" stroke-width="2"><title>'
                  f'투입 {x:.0%} → {v:{fmt}}</title></circle>')
        # 마지막 점만 직접 라벨 (모든 점에 숫자를 붙이지 않는다)
        s.text(px(xs[-1]) + 9, py(values[-1]) + 3.5, f"{values[-1]:{fmt}}",
               size=10, fill=INK, weight="600")

    panel(top, [p[1] for p in points], "X-factor (사이클타임 ÷ 순수 처리시간)",
          ".2f", 1.0, "대기 없음")
    y2 = top + panel_h + gap
    panel(y2, [p[2] for p in points], "달성 처리량 (lot/일)",
          ".2f", capacity_lots_per_day, "해석적 상한")

    for x in xs:
        s.line(px(x), y2 + panel_h, px(x), y2 + panel_h + 4, stroke=INK3, sw=1)
        s.text(px(x), y2 + panel_h + 16, f"{x:.0%}", size=9, fill=INK3, anchor="middle")
    s.text(left + plot_w / 2, y2 + panel_h + 34, "투입률 (병목 능력 대비)",
           size=9.5, fill=INK2, anchor="middle")
    return s.render()


# =========================================================================
# 이론값 대조 (편차 도표)
# =========================================================================


def deviation_svg(
    rows: list[tuple[str, float, float, float]],
    title: str = "대기행렬 이론과의 대조",
    subtitle: str = "",
    width: float = 720,
    row_h: float = 34,
) -> str:
    """(사례, 실측, 이론, 허용오차) → 편차 도표.

    "맞았는가"를 보는 데는 두 값을 나란히 놓은 막대보다 **0을 기준으로 한 편차**가
    낫다. 허용오차 띠 안에 점이 들어왔는지가 한눈에 보인다.
    """
    left, right, top = 168.0, 96.0, 74.0
    H = top + len(rows) * row_h + 44
    plot_w = width - left - right
    span = max(max(abs(m / t - 1.0) for _, m, t, _ in rows),
               max(tol for *_, tol in rows)) * 1.35

    def px(dev: float) -> float:
        return left + plot_w / 2 + (plot_w / 2) * dev / span

    s = _Svg(width, H, [], aria=f"{title} — 사례 {len(rows)}건의 이론값 대비 편차")
    s.rect(0, 0, width, H, fill=SURFACE)
    s.text(16, 22, title, size=14, weight="600")
    if subtitle:
        s.text(16, 39, subtitle, size=10, fill=INK2)
    s.text(16, 56, "점 = 이론값 대비 편차 · 회색 띠 = 허용오차 · 세로선 = 완전 일치",
           size=9.5, fill=INK3)

    for i, (label, measured, theory, tol) in enumerate(rows):
        y = top + i * row_h + row_h / 2
        s.rect(px(-tol), y - 11, px(tol) - px(-tol), 22, fill=SURFACE2, rx=3,
               title=f"허용오차 ±{tol:.0%}")
        dev = measured / theory - 1.0
        ok = abs(dev) <= tol
        s.text(left - 10, y + 4, label, size=10, fill=INK, anchor="end")
        s.line(px(0), y - 11, px(0), y + 11, stroke=INK3, sw=1)
        s.line(px(0), y, px(dev), y, stroke=INK3, sw=1.5)
        s.add(f'<circle cx="{_n(px(dev))}" cy="{_n(y)}" r="5.5" '
              f'fill="{GOOD if ok else CRITICAL}" stroke="{SURFACE}" stroke-width="1.5">'
              f'<title>{_e(label)} — 실측 {measured:.2f} vs 이론 {theory:.2f} '
              f'({dev:+.1%}, 허용 ±{tol:.0%})</title></circle>')
        s.text(left + plot_w + 8, y + 4, f"{dev:+.1%}", size=10,
               fill=INK, weight="600")

    ay = top + len(rows) * row_h + 16
    s.text(px(0), ay, "0", size=9, fill=INK3, anchor="middle")
    s.text(px(-span), ay, f"{-span:+.0%}", size=9, fill=INK3, anchor="middle")
    s.text(px(span), ay, f"{span:+.0%}", size=9, fill=INK3, anchor="middle")
    return s.render()


# =========================================================================
# 라우트 행렬 (재진입 흐름)
# =========================================================================


def route_matrix_svg(
    fab: Fab,
    pid: str,
    title: str = "",
    cell: float = 7.0,
    row: float = 15.0,
) -> str:
    """라우트를 (설비 그룹 × 스텝) 행렬로. 재진입 구조가 한눈에 보인다.

    색은 쓰지 않는다 — 한 계열의 채움/비움과 레이어 경계선만으로 충분하고, 그룹 11개에
    범주 색을 배정하는 것보다 정확하다.
    """
    product = fab.products[pid]
    gids = sorted(fab.groups, key=lambda g: -fab.weighted_visits()[g])
    n_steps = product.step_count
    left, top = 96.0, 74.0
    W = left + n_steps * cell + 52
    H = top + len(gids) * row + 46

    s = _Svg(W, H, [], aria=f"{pid} 라우트 {n_steps}스텝의 설비 그룹별 방문 위치")
    s.rect(0, 0, W, H, fill=SURFACE)
    s.text(16, 22, title or f"{pid} 라우트 구조 — 재진입 흐름", size=14, weight="600")
    s.text(16, 39, f"{product.name} · {n_steps}스텝 · {product.layer_count}레이어"
                   f" · 믹스 {product.mix:.0%}", size=10, fill=INK2)
    s.text(16, 56, "가로 = 공정 순서, 세로 = 설비 그룹. 한 행에 점이 여러 개 = 그 설비를 다시 방문",
           size=9.5, fill=INK3)

    counts = product.visit_counts()
    # 레이어 경계
    bounds: list[int] = []
    for i, st in enumerate(product.steps):
        if i and st.layer != product.steps[i - 1].layer:
            bounds.append(i)
    for i in bounds:
        x = left + i * cell - 0.5
        s.line(x, top - 4, x, top + len(gids) * row - 4, stroke=LINE, sw=1)
    for li in range(product.layer_count):
        start = 0 if li == 0 else bounds[li - 1]
        end = bounds[li] if li < len(bounds) else n_steps
        s.text(left + (start + end) / 2 * cell, top - 8, f"L{li + 1}", size=8,
               fill=INK3, anchor="middle")

    for r, gid in enumerate(gids):
        y = top + r * row
        n = counts.get(gid, 0)
        s.text(left - 8, y + row / 2 + 2, gid, size=9, fill=INK if n else INK3,
               anchor="end", weight="600" if n > 6 else "400")
        s.line(left, y + row / 2 - 0.5, left + n_steps * cell, y + row / 2 - 0.5,
               stroke=GRID, sw=1)
        for i, st in enumerate(product.steps):
            if st.group != gid:
                continue
            s.rect(left + i * cell + 0.5, y + 2.5, cell - 1.5, row - 7,
                   fill=SEQ[4], rx=1.5,
                   title=f"스텝 {i + 1}/{n_steps} · L{st.layer} · {gid}")
        s.text(left + n_steps * cell + 8, y + row / 2 + 2, f"{n}회", size=9,
               fill=INK2 if n else INK3)

    s.text(left, H - 14, f"레이어 경계 {len(bounds)}개 · 세로선 = 레이어 전환", size=9, fill=INK3)
    return s.render()


# =========================================================================
# 거리 행렬 히트맵
# =========================================================================


def distance_heatmap_svg(
    dm: DistanceMatrix,
    geo: LayoutGeometry,
    title: str = "slot 간 반송 거리 행렬",
    cell: float = 15.0,
) -> str:
    n = len(dm)
    left, top = 62.0, 76.0
    W = left + n * cell + 74
    H = top + n * cell + 62
    hi = dm.max_m
    bins = len(SEQ)

    s = _Svg(W, H, [], aria=f"{title} — {n}×{n}, 최대 {hi:.0f}m")
    s.rect(0, 0, W, H, fill=SURFACE)
    s.text(16, 22, title, size=14, weight="600")
    s.text(16, 39, f"{n}×{n} · 최대 {hi:.0f}m · 평균 {dm.mean_offdiag_m:.1f}m"
                   f" · 진할수록 멀다", size=10, fill=INK2)
    s.text(16, 56, "같은 bay 블록(대각선의 6×6 덩어리)이 밝다 = bay 안이 항상 가깝다",
           size=9.5, fill=INK3)

    for i, a in enumerate(dm.slots):
        for j, b in enumerate(dm.slots):
            d = dm.by_index(i, j)
            idx = _bin_index(d, 0.0, hi, bins)
            s.rect(left + j * cell, top + i * cell, cell - 1, cell - 1,
                   fill=SEQ[idx] if d > 0 else SURFACE2, rx=1,
                   title=f"{a} → {b} = {d:.1f}m")

    # bay 경계선
    per_bay = 2 * geo.positions_per_side
    for k in range(0, n + 1, per_bay):
        s.line(left + k * cell - 0.5, top, left + k * cell - 0.5, top + n * cell,
               stroke=INK3, sw=1)
        s.line(left, top + k * cell - 0.5, left + n * cell, top + k * cell - 0.5,
               stroke=INK3, sw=1)
    for bi, b in enumerate(sorted(geo.bays, key=lambda z: z.bay)):
        cx = left + (bi + 0.5) * per_bay * cell
        s.text(cx, top - 6, f"B{b.bay}", size=9, fill=INK2, anchor="middle", weight="600")
        s.text(left - 8, top + (bi + 0.5) * per_bay * cell + 4, f"BAY {b.bay}",
               size=9, fill=INK2, anchor="end", weight="600")

    ly = top + n * cell + 26
    s.text(left, ly, "0m", size=9, fill=INK3)
    for i, c in enumerate(SEQ):
        s.rect(left + 24 + i * 16, ly - 9, 14, 10, fill=c, stroke=LINE, sw=0.5)
    s.text(left + 24 + bins * 16 + 4, ly, f"{hi:.0f}m", size=9, fill=INK3)
    return s.render()
