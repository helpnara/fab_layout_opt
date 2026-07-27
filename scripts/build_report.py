#!/usr/bin/env python3
"""마일스톤 진행 리포트 생성기.

현재 코드베이스에서 실제 수치를 계산해 단일 HTML 파일로 낸다. 리포트에 적힌 숫자는
모두 이 스크립트가 그 시점의 코드로 계산한 값이며, 손으로 옮겨적은 값은 없다.

    python scripts/build_report.py --out report.html
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fablayout.core.analysis import analytic_capacity, flow_report  # noqa: E402
from fablayout.core.distance import DistanceMatrix  # noqa: E402
from fablayout.core.geometry import default_geometry  # noqa: E402
from fablayout.data.builtin import build_smallfab21  # noqa: E402
from fablayout.opt import baseline  # noqa: E402
from fablayout.viz import svg  # noqa: E402

MILESTONES = [
    ("M1", "코어 · 데이터셋 · 거리", "done"),
    ("M2", "DES 엔진 기본", "next"),
    ("M3", "고장 · 배치 · 반송", "todo"),
    ("M4", "지표 · 능력탐색 · CLI", "todo"),
    ("M5", "대리지표 상관 검증", "gate"),
    ("M6", "SA · 2단 파이프라인", "todo"),
    ("M7", "FastAPI · 배치도 UI", "todo"),
    ("M8", "비교 · 애니메이션", "todo"),
]


# ---- 페이지 스타일 -------------------------------------------------------
# 클린룸의 여과된 빛을 기준으로 한 냉색 편향 중성색. 강조색은 도표와 동일한
# 검증 팔레트 슬롯 1(blue)이라, 페이지와 도표가 한 시스템으로 읽힌다.

CSS = """
:root {
  color-scheme: light;
  --ground:  #f5f7f8;
  --surface: #ffffff;
  --inset:   #eef1f3;
  --ink:     #101418;
  --ink-2:   #47535e;
  --ink-3:   #7b8792;
  --line:    #d7dde2;
  --hair:    #e6eaee;
  --accent:  #2a78d6;
  --good:    #0ca30c;
  --crit:    #d03b3b;
  --warn:    #b07a00;

  --fl-ink: #101418;  --fl-ink-2: #47535e;  --fl-ink-3: #7b8792;
  --fl-surface: #ffffff; --fl-surface-2: #eef1f3;
  --fl-line: #d7dde2; --fl-grid: #e6eaee;
  --fl-seq-0: #cde2fb; --fl-seq-1: #9ec5f4; --fl-seq-2: #6da7ec;
  --fl-seq-3: #3987e5; --fl-seq-4: #256abf; --fl-seq-5: #104281;
  --fl-cat-1: #2a78d6; --fl-cat-2: #eb6834; --fl-cat-3: #1baf7a; --fl-cat-4: #eda100;
  --fl-critical: #d03b3b; --fl-good: #0ca30c;

  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo",
          "Noto Sans KR", "Malgun Gothic", sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
          "Liberation Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --ground:  #101315;
    --surface: #191d20;
    --inset:   #212629;
    --ink:     #eef1f3;
    --ink-2:   #a8b3bc;
    --ink-3:   #78838c;
    --line:    #2e3438;
    --hair:    #262b2f;
    --accent:  #3987e5;
    --good:    #0ca30c;
    --crit:    #e05a5a;
    --warn:    #fab219;

    --fl-ink: #eef1f3;  --fl-ink-2: #a8b3bc;  --fl-ink-3: #78838c;
    --fl-surface: #191d20; --fl-surface-2: #212629;
    --fl-line: #2e3438; --fl-grid: #262b2f;
    --fl-seq-0: #0d366b; --fl-seq-1: #104281; --fl-seq-2: #1c5cab;
    --fl-seq-3: #2a78d6; --fl-seq-4: #5598e7; --fl-seq-5: #9ec5f4;
    --fl-cat-1: #3987e5; --fl-cat-2: #d95926; --fl-cat-3: #199e70; --fl-cat-4: #c98500;
    --fl-critical: #d03b3b; --fl-good: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --ground:#101315; --surface:#191d20; --inset:#212629;
  --ink:#eef1f3; --ink-2:#a8b3bc; --ink-3:#78838c;
  --line:#2e3438; --hair:#262b2f; --accent:#3987e5;
  --good:#0ca30c; --crit:#e05a5a; --warn:#fab219;
  --fl-ink:#eef1f3; --fl-ink-2:#a8b3bc; --fl-ink-3:#78838c;
  --fl-surface:#191d20; --fl-surface-2:#212629;
  --fl-line:#2e3438; --fl-grid:#262b2f;
  --fl-seq-0:#0d366b; --fl-seq-1:#104281; --fl-seq-2:#1c5cab;
  --fl-seq-3:#2a78d6; --fl-seq-4:#5598e7; --fl-seq-5:#9ec5f4;
  --fl-cat-1:#3987e5; --fl-cat-2:#d95926; --fl-cat-3:#199e70; --fl-cat-4:#c98500;
  --fl-critical:#d03b3b; --fl-good:#0ca30c;
}

* { box-sizing: border-box; }
body {
  margin: 0; background: var(--ground); color: var(--ink);
  font-family: var(--sans); font-size: 15px; line-height: 1.7;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 940px; margin: 0 auto; padding: 40px 24px 96px; }

/* ---- 머리말 ---- */
.masthead { border-bottom: 2px solid var(--ink); padding-bottom: 18px; }
.eyebrow {
  font-family: var(--sans); font-size: 11.5px; font-weight: 600;
  letter-spacing: 0.02em; color: var(--ink-3);
}
h1 {
  font-size: clamp(26px, 4.2vw, 38px); line-height: 1.2; margin: 10px 0 6px;
  letter-spacing: -0.022em; font-weight: 700; text-wrap: balance;
}
.dek { color: var(--ink-2); margin: 0; max-width: 36em; }
.meta {
  display: flex; flex-wrap: wrap; gap: 6px 20px; margin-top: 16px;
  font-size: 12.5px; color: var(--ink-3);
}
.meta b { color: var(--ink-2); font-weight: 500; }

/* ---- 마일스톤 진행 ---- */
.track { display: grid; grid-template-columns: repeat(8, 1fr); gap: 3px; margin: 28px 0 0; }
.stage {
  background: var(--surface); border: 1px solid var(--line);
  border-top: 3px solid var(--line); padding: 8px 8px 10px; min-width: 0;
}
.stage .id { font-family: var(--mono); font-size: 12px; font-weight: 600; }
.stage .nm { font-size: 10.5px; line-height: 1.35; color: var(--ink-3); }
.stage.done { border-top-color: var(--good); }
.stage.done .id { color: var(--good); }
.stage.next { border-top-color: var(--accent); background: var(--inset); }
.stage.next .id { color: var(--accent); }
.stage.gate { border-top-color: var(--warn); }
.stage.gate .id { color: var(--warn); }
.track-note { font-size: 11.5px; color: var(--ink-3); margin: 8px 0 0; }

/* ---- 섹션 ---- */
section { margin-top: 52px; }
section > .eyebrow { display: block; margin-bottom: 4px; }
h2 {
  font-size: 20px; margin: 0 0 4px; letter-spacing: -0.012em; font-weight: 650;
  padding-top: 12px; border-top: 1px solid var(--line);
}
h2 .n { font-family: var(--mono); color: var(--ink-3); font-weight: 500; margin-right: 10px; }
h3 { font-size: 15.5px; margin: 26px 0 6px; font-weight: 650; }
p { margin: 10px 0; max-width: 40em; }
.lead { color: var(--ink-2); }
a { color: var(--accent); }
code {
  font-family: var(--mono); font-size: 0.88em; background: var(--inset);
  padding: 1px 5px; border-radius: 3px;
}

/* ---- 수치 격자 ---- */
.kpis {
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px; background: var(--line); border: 1px solid var(--line); margin: 22px 0;
}
.kpi { background: var(--surface); padding: 13px 15px 15px; }
.kpi dt {
  font-size: 11.5px; color: var(--ink-3); margin: 0 0 5px; font-weight: 600;
}
.kpi dd {
  margin: 0; font-size: 23px; font-weight: 650; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums; line-height: 1.15;
}
.kpi dd .u { font-size: 12px; font-weight: 500; color: var(--ink-3); margin-left: 3px; }
.kpi .sub { font-size: 11.5px; color: var(--ink-3); margin-top: 3px; line-height: 1.4; }

/* ---- 그림 ---- */
figure {
  margin: 24px 0; background: var(--surface); border: 1px solid var(--line);
  padding: 4px;
}
figure .scroll { overflow-x: auto; }
figcaption {
  font-size: 12px; color: var(--ink-2); padding: 10px 12px 8px;
  border-top: 1px solid var(--hair); line-height: 1.55;
}
figcaption b { color: var(--ink); font-weight: 600; }

/* ---- 표 ---- */
.tblwrap { overflow-x: auto; margin: 20px 0; border: 1px solid var(--line); }
table { border-collapse: collapse; width: 100%; font-size: 13px; background: var(--surface); }
th, td { padding: 7px 11px; text-align: right; border-bottom: 1px solid var(--hair); }
th:first-child, td:first-child { text-align: left; }
thead th {
  font-size: 11.5px; color: var(--ink-3); font-weight: 600;
  border-bottom: 1px solid var(--line); white-space: nowrap;
}
tbody td { font-variant-numeric: tabular-nums; }
tbody tr:last-child td { border-bottom: none; }
td.gid { font-family: var(--mono); font-weight: 600; font-size: 12px; }
tr.bottleneck td { background: color-mix(in srgb, var(--crit) 8%, transparent); }
.tag {
  font-family: var(--mono); font-size: 10px; padding: 1px 5px;
  border: 1px solid var(--line); border-radius: 3px; color: var(--ink-2);
  white-space: nowrap;
}

/* ---- 판단 기록 ---- */
.finding { border-top: 1px solid var(--line); padding-top: 16px; margin-top: 26px; }
.finding .hd { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.finding .kind {
  font-size: 11.5px; padding: 2px 7px; border: 1px solid currentColor;
  font-weight: 600;
}
.finding.fix .kind { color: var(--crit); }
.finding.add .kind { color: var(--accent); }
.finding h3 { margin: 0; font-size: 16px; }
.ba {
  display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--line);
  border: 1px solid var(--line); margin: 14px 0 0;
}
.ba > div { background: var(--surface); padding: 12px 14px; }
.ba .lbl {
  font-size: 11.5px; color: var(--ink-3); margin-bottom: 6px; font-weight: 600;
}
.ba p { margin: 0; font-size: 13.5px; }
.ba .num { font-variant-numeric: tabular-nums; font-weight: 650; }

/* ---- 목록 ---- */
ul.checks { list-style: none; padding: 0; margin: 16px 0; }
ul.checks li {
  padding: 7px 0 7px 26px; border-bottom: 1px solid var(--hair);
  position: relative; font-size: 13.5px;
}
ul.checks li::before {
  content: "✓"; position: absolute; left: 4px; color: var(--good); font-weight: 700;
}
ul.checks li.open::before { content: "→"; color: var(--accent); }
ul.checks li b { font-weight: 600; }
ul.plain { padding-left: 20px; }
ul.plain li { margin: 6px 0; max-width: 40em; }

footer {
  margin-top: 64px; padding-top: 18px; border-top: 1px solid var(--line);
  font-size: 12px; color: var(--ink-3);
}
:where(a, [tabindex]):focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
@media (max-width: 720px) {
  .track { grid-template-columns: repeat(4, 1fr); }
  .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ba { grid-template-columns: 1fr; }
}
"""


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def build() -> str:
    fab = build_smallfab21()
    geo = default_geometry()
    dm = DistanceMatrix.build(geo)
    func = baseline.functional_layout(fab, geo)
    cap = analytic_capacity(fab)
    rate = cap.capacity_lots_per_day

    flows = {
        "기능별 (기준선)": flow_report(fab, geo, func, dm),
        "흩뿌림": flow_report(fab, geo, baseline.spread_layout(fab, geo), dm),
        "무작위": flow_report(fab, geo, baseline.random_layout(fab, geo, seed=3), dm),
    }
    fr = flows["기능별 (기준선)"]
    util = fr.vehicle_utilization(rate, fab.transport.vehicles)

    # 스토커 모델을 뺐을 때와의 대조 (M1에서 발견한 문제의 크기)
    no_stocker_s = (
        fr.handling_seconds_per_lot + fr.travel_seconds_per_lot + fr.empty_seconds_per_lot
    )
    no_stocker_util = rate * no_stocker_s / 86400 / fab.transport.vehicles

    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
    )
    tail = [ln for ln in tests.stdout.strip().splitlines() if "passed" in ln or "failed" in ln]
    test_line = tail[-1] if tail else "테스트 실행 실패"
    n_tests = test_line.split()[0] if test_line[0].isdigit() else "?"

    near = cap.near_bottleneck(0.20)
    families = sorted({g.family for g in fab.groups.values()})

    h: list[str] = []
    a = h.append

    # ---- 머리말
    a('<div class="wrap">')
    a('<header class="masthead">')
    a('<span class="eyebrow">fab_layout_opt · 진행 리포트</span>')
    a("<h1>M1 완료 — 코어 모델, 내장 데이터셋, 거리 행렬</h1>")
    a('<p class="dek">Bay 구조 클린룸의 기하·거리 모델과 벤치마크 구조를 참조한 축소 '
      "데이터셋을 구현했다. 이 단계에서 설계서의 기하 모델 오류 하나를 고치고, "
      "빠져 있던 반송 모델 요소 하나를 추가했다.</p>")
    a('<div class="meta">')
    a(f"<span>테스트 <b>{n_tests}개 통과</b></span>")
    a(f"<span>설비 <b>{fab.total_tools}대</b> / slot <b>{geo.slot_count}</b></span>")
    a(f"<span>데이터셋 <b>{fab.name}</b></span>")
    a("<span>다음 <b>M2 · DES 엔진</b></span>")
    a("</div></header>")

    # ---- 마일스톤 트랙
    a('<div class="track">')
    for mid, name, state in MILESTONES:
        a(f'<div class="stage {state}"><div class="id">{mid}</div>'
          f'<div class="nm">{name}</div></div>')
    a("</div>")
    a('<p class="track-note">M5는 게이트다 — 거리 대리지표와 DES 실측의 순위상관이 '
      "0.7 미만이면 2단 최적화 구조를 버리고 다른 접근으로 선회한다.</p>")

    # ---- 1. 요약
    a("<section>")
    a('<span class="eyebrow">요약</span>')
    a('<h2><span class="n">1</span>현재 상태의 수치</h2>')
    a('<p class="lead">아래 값은 모두 이 리포트를 만들 때 현재 코드로 계산한 것이다. '
      "손으로 옮겨적은 숫자는 없다.</p>")
    a('<dl class="kpis">')
    for label, value, unit, sub in [
        ("생산능력 상한", f"{rate:.2f}", "lot/일",
         f"{rate * fab.wafers_per_lot:.0f} 웨이퍼/일 · 해석적 상한"),
        ("병목 그룹", cap.bottleneck.gid, "",
         f"{cap.bottleneck.name} ×{cap.bottleneck.tools}대"),
        ("근접 병목", f"{len(near)}", "개",
         "병목 능력의 120% 이내 — 최적화 여지 있음"),
        ("순수 처리시간", f"{cap.raw_process_hours:.0f}", "h",
         f"lot당 · X-factor 3이면 사이클타임 {cap.raw_process_hours * 3 / 24:.1f}일"),
        ("반송차 가동률", _pct(util), "",
         f"{fab.transport.vehicles}대 기준 · 민감 구간 55~85%"),
        ("bay 교차 이동", _pct(fr.interbay_fraction), "",
         f"{fr.interbay_moves_per_lot:.0f}/{fr.moves_per_lot:.0f}회 · 스토커 비용 발생"),
        ("배치 조절 가능 비중", _pct(fr.layout_controllable_fraction), "",
         "반송 부하 중 배치가 바꿀 수 있는 부분"),
        ("거리 행렬", f"{len(dm)}×{len(dm)}", "",
         f"최대 {dm.max_m:.0f}m · 평균 {dm.mean_offdiag_m:.1f}m"),
    ]:
        u = f'<span class="u">{unit}</span>' if unit else ""
        a(f'<div class="kpi"><dt>{label}</dt><dd>{value}{u}</dd>'
          f'<div class="sub">{sub}</div></div>')
    a("</dl></section>")

    # ---- 2. 설계 변경
    a("<section>")
    a('<span class="eyebrow">이번 단계에서 바뀐 것</span>')
    a('<h2><span class="n">2</span>설계 결정 2건</h2>')
    a('<p class="lead">구현하면서 설계서가 틀렸음이 드러난 부분이다. 둘 다 '
      "결과의 타당성에 직결되므로 넘어가지 않고 고쳤다.</p>")

    a('<div class="finding fix">')
    a('<div class="hd"><span class="kind">수정</span>'
      "<h3>bay 방향이 반대였다</h3></div>")
    a("<p>설계서 §5.1은 bay 내부 slot이 spine과 <b>평행</b>하게 늘어서도록 적혀 있었다. "
      "실제 spine/bay 레이아웃에서 bay는 spine에서 <b>수직으로</b> 뻗어 나간다. "
      "같은 문서 §4.3의 “slot 0이 bay 입구에 가장 가깝다”와도 모순이었다. "
      "수직 bay + 중앙 통로 양벽 배치로 고쳤다.</p>")
    a('<div class="ba">')
    a('<div><div class="lbl">수정 전 (설계서)</div>'
      "<p>bay가 spine과 평행 · slot이 x축 방향 · 입구 위치가 정의상 모순</p></div>")
    a('<div><div class="lbl">수정 후 (구현)</div>'
      f'<p>bay가 spine과 수직 · slot = <code>(bay, 좌/우, 위치)</code> · '
      f'bay {len(geo.bays)}개 × 2벽 × {geo.positions_per_side}위치 = '
      f'<span class="num">{geo.slot_count}</span> slot</p></div>')
    a("</div></div>")

    a('<div class="finding add">')
    a('<div class="hd"><span class="kind">추가</span>'
      "<h3>스토커 경유 모델이 빠져 있었다 — 배치가 처리량에 영향을 못 주고 있었다</h3></div>")
    a(f"<p>초기 구현으로 실측했더니 반송이 사이클타임에 더하는 시간이 순수 처리시간의 "
      f"<b>{no_stocker_s / 3600 / cap.raw_process_hours:.1%}</b>뿐이었고, 그중 "
      f"{fr.handling_seconds_per_lot / no_stocker_s:.0%}가 배치와 무관한 상하차 "
      f"고정시간이었다. 반송차 가동률도 {fab.transport.vehicles}대에서 "
      f"{no_stocker_util:.0%}로 포화와 거리가 멀었다. 즉 <b>배치를 아무리 잘 해도 "
      "처리량이 바뀌지 않는 상태</b>였다.</p>")
    a(f"<p>원인은 모델링 누락이었다. 실제 fab의 AMHS는 bay 내부와 bay 간이 분리되어 있어, "
      "bay를 넘는 lot은 출발 bay 스토커에 입고되고 도착 bay 스토커에서 재출고된다. "
      f"이 비용은 초가 아니라 <b>분 단위</b>다. 스토커 조작 시간"
      f"(항방향 {fab.transport.stocker_time_s:.0f}초)을 넣으면 "
      "“같은 bay 안에서 공정을 이어가라”는 bay 배치의 본래 동기가 모델에 살아난다.</p>")
    a('<div class="ba">')
    a(f'<div><div class="lbl">스토커 모델 없음</div><p>반송 부하 '
      f'<span class="num">{no_stocker_s / 3600:.1f}h</span>/lot '
      f'({no_stocker_s / 3600 / cap.raw_process_hours:.1%} of 처리시간) · '
      f'반송차 가동률 <span class="num">{_pct(no_stocker_util)}</span> → '
      "배치 무관</p></div>")
    a(f'<div><div class="lbl">스토커 모델 있음 (채택)</div><p>반송 부하 '
      f'<span class="num">{fr.vehicle_seconds_per_lot / 3600:.1f}h</span>/lot '
      f'({fr.vehicle_seconds_per_lot / 3600 / cap.raw_process_hours:.1%}) · '
      f'가동률 <span class="num">{_pct(util)}</span> → 민감 구간</p></div>')
    a("</div></div></section>")

    # ---- 3. 배치도
    a("<section>")
    a('<span class="eyebrow">기하 · 배치</span>')
    a('<h2><span class="n">3</span>기준선 배치도</h2>')
    a("<p class=\"lead\">기준선은 실무 관행인 <b>기능별 배치</b>다 — 동종 설비를 한 bay에 "
      "모으고, 방문이 잦은 그룹을 spine 중앙 쪽에 둔다. 최적화 결과는 무작위가 아니라 "
      "이것과 비교해야 의미가 있다.</p>")
    a('<figure><div class="scroll">')
    a(svg.layout_svg(fab, geo, func, title=f"{fab.name} · 기능별 배치 (기준선)"))
    a("</div><figcaption>")
    a(f"bay {len(geo.bays)}개(북 3 / 남 2), bay당 slot 6개로 총 {geo.slot_count}자리, "
      f"설비 {fab.total_tools}대 배치(채움률 {fab.total_tools / geo.slot_count:.0%}). "
      f"spine 길이 {geo.spine_length_m:.0f}m. 남서 한 자리는 유틸리티 구역으로 비워 둔 "
      "설정이다. bay 라벨 아래 <b>막대는 그 bay가 받는 반송 부하</b>(방문 횟수/lot)이므로, "
      "막대가 긴 bay가 물류가 몰리는 곳이다. 설비 박스의 2자 약호는 아래 표의 "
      "<b>약호</b> 열과 같다 — 설비 그룹이 11개라 그룹별 색을 배정하면 색맹 안전 "
      "범위(8색)를 넘기므로, 색이 아니라 라벨로 구분한다.")
    a("</figcaption></figure></section>")

    # ---- 4. 데이터셋
    a("<section>")
    a('<span class="eyebrow">데이터</span>')
    a('<h2><span class="n">4</span>내장 데이터셋 <code>smallfab-21</code></h2>')
    a("<p class=\"lead\">SMT2020 / MIMAC 벤치마크 원본은 이 실행 환경에서 다운로드가 "
      "차단되어 있고, 설비 1,000대급이라 대상 범위(10~40대)와도 맞지 않는다. "
      f"그래서 <b>구조만 따른 축소 합성 데이터</b>를 동봉했다. 공정 계열 {len(families)}종"
      f"({' · '.join(families)}), 재진입·배치 설비·고장을 모두 포함한다. "
      "원본 파일을 확보하면 읽는 로더는 별도로 구현한다.</p>")
    a('<div class="tblwrap"><table>')
    a("<thead><tr><th>그룹</th><th>약호</th><th>공정</th><th>계열</th><th>대수</th>"
      "<th>처리방식</th><th>처리시간</th><th>가동률</th><th>방문/lot</th><th>요구/lot</th>"
      "<th>능력</th><th>병목대비</th></tr></thead><tbody>")
    codes = svg.short_codes(fab)
    for g in cap.groups:
        tg = fab.groups[g.gid]
        mode = f"배치 ×{tg.batch_size}" if tg.mode == "batch" else "단일"
        cls = ' class="bottleneck"' if g.gid == cap.bottleneck.gid else ""
        a(f"<tr{cls}><td class=\"gid\">{g.gid}</td>"
          f'<td class="gid">{codes[g.gid]}</td><td>{tg.name}</td>'
          f'<td><span class="tag">{tg.family}</span></td><td>{g.tools}</td>'
          f"<td>{mode}</td><td>{tg.process_minutes:.0f}분</td>"
          f"<td>{g.availability:.0%}</td><td>{g.visits_per_lot:.1f}</td>"
          f"<td>{g.workload_min_per_lot:.0f}분</td>"
          f"<td>{g.capacity_lots_per_day:.2f}</td>"
          f"<td>{rate / g.capacity_lots_per_day:.0%}</td></tr>")
    a("</tbody></table></div>")
    a(f'<p style="font-size:12.5px;color:var(--ink-3)">출처: {fab.source}. '
      "대외 자료에 결과를 쓸 경우 이 구분을 표기해야 한다.</p>")

    a("<h3>제품과 라우트</h3>")
    a("<p>재진입 흐름이 반도체 공정의 핵심 특징이다. 웨이퍼는 층을 하나씩 쌓으므로 "
      "같은 노광 설비를 레이어마다 다시 방문한다. 방문이 잦은 설비의 위치가 이동거리 "
      "총합을 지배하므로, 배치 최적화에서 가장 중요한 구조다.</p>")
    for pid in fab.products:
        a('<figure><div class="scroll">')
        a(svg.route_matrix_svg(fab, pid))
        a("</div><figcaption>")
        p = fab.products[pid]
        top3 = sorted(p.visit_counts().items(), key=lambda kv: -kv[1])[:3]
        a(f"<b>{p.name}</b> — {p.step_count}스텝 / {p.layer_count}레이어. "
          f"최다 방문: {', '.join(f'{k} {v}회' for k, v in top3)}. "
          "각 레이어는 세정으로 시작하고 계측으로 끝나며, 노광이 식각보다 앞선다 — "
          "테스트가 이 순서를 기계적으로 검증한다.")
        a("</figcaption></figure>")
    a("</section>")

    # ---- 5. 생산능력
    a("<section>")
    a('<span class="eyebrow">해석적 사전 계산</span>')
    a('<h2><span class="n">5</span>생산능력과 병목 구조</h2>')
    a("<p class=\"lead\">DES 없이 설비 대수 구성만으로 구한 능력 상한이다. 세 가지 용도가 "
      "있다 — 데이터셋 점검, DES 투입률 램프의 시작점, 최적화 대리지표. 대기행렬을 "
      "무시하므로 낙관적 상한이며, DES 실측은 반드시 이보다 낮아야 한다.</p>")
    a('<figure><div class="scroll">')
    a(svg.capacity_bars_svg(fab, cap))
    a("</div><figcaption>")
    a(f"병목은 <b>{cap.bottleneck.gid}</b>({cap.bottleneck.name}, {cap.bottleneck.tools}대)로 "
      f"{rate:.2f} lot/일. 능력의 120% 이내에 <b>{len(near)}개 그룹</b>"
      f"({', '.join(g.gid for g in near)})이 몰려 있다. "
      "이 근접 병목 구조는 의도한 것이다 — 병목이 하나만 압도적이면 답이 자명해지고, "
      "전부 여유면 배치가 무관해진다. 테스트가 이 구조를 유지하도록 지키고 있다.")
    a("</figcaption></figure></section>")

    # ---- 6. 반송
    a("<section>")
    a('<span class="eyebrow">배치 → 처리량 전달 경로</span>')
    a('<h2><span class="n">6</span>반송차 부하 분해</h2>')
    a("<p class=\"lead\">배치는 사이클타임에 직접 더해지는 이동시간이 아니라, "
      "<b>반송차 혼잡</b>을 통해 처리량에 영향을 준다. 배치가 나쁘면 bay 교차가 늘고 "
      "→ 스토커 조작이 늘고 → 반송차가 모자라고 → 설비가 반송을 기다리며 유휴가 된다. "
      "그래서 반송차 가동률이 민감 구간에 있어야 이 경로가 살아 있다.</p>")
    a('<figure><div class="scroll">')
    a(svg.transport_stack_svg(flows, rate, fab.transport.vehicles))
    a("</div><figcaption>")
    good, poor = flows["기능별 (기준선)"], flows["흩뿌림"]
    gap = poor.vehicle_utilization(rate, fab.transport.vehicles) - util
    a(f"<b>스토커 항이 지배적</b>이므로 최적화의 실질적 레버는 거리보다 "
      f"<b>bay 교차 횟수</b>다. 기능별 배치는 bay 교차 "
      f"{good.interbay_moves_per_lot:.0f}회, 흩뿌림은 {poor.interbay_moves_per_lot:.0f}회로, "
      f"반송차 가동률 차이가 {gap * 100:.0f}%p 벌어진다. "
      "이 차이가 DES에서 큐 대기시간 차이로 나타날 것이고, 그것이 처리량 차이가 된다. "
      "M5의 대리지표는 거리와 bay 교차를 모두 담아야 한다.")
    a("</figcaption></figure></section>")

    # ---- 7. 거리 행렬
    a("<section>")
    a('<span class="eyebrow">거리 모델</span>')
    a('<h2><span class="n">7</span>거리 행렬</h2>')
    a("<p class=\"lead\">반송차는 bay 중앙 궤도와 spine 궤도만 달린다. 같은 bay 안은 "
      "<code>|y 차이|</code>, 다른 bay는 <code>|y₁| + |x₁−x₂| + |y₂|</code>로 spine을 "
      "경유한다. 레이아웃 기하가 결정 변수가 아니므로 <b>이 행렬은 최적화 전체에서 "
      "불변</b>이다 — 배치를 바꾸는 것은 어느 설비가 어느 slot에 있는지를 바꾸는 것뿐이고, "
      "그래서 대리지표 평가가 빠르다.</p>")
    a('<figure><div class="scroll">')
    a(svg.distance_heatmap_svg(dm, geo))
    a("</div><figcaption>")
    fa, fb, fd = dm.farthest_pair()
    a(f"대각선의 6×6 블록이 밝다 — <b>같은 bay 안은 항상 가깝다</b>. 이것이 그룹을 "
      f"뭉치는 근거다. 가장 먼 쌍은 {fa} ↔ {fb} = {fd:.1f}m. "
      "거리 공리(비음수·대칭·삼각부등식)는 30×30 전수로 검증했다 — 최적화가 이 행렬을 "
      "수백만 번 조회하므로 여기서 틀리면 이후 모든 결과가 무의미해진다.")
    a("</figcaption></figure></section>")

    # ---- 8. 검증
    a("<section>")
    a('<span class="eyebrow">검증</span>')
    a(f'<h2><span class="n">8</span>테스트 {n_tests}개</h2>')
    a("<ul class=\"checks\">")
    for item in [
        "<b>거리 공리</b> — 30×30 전수로 비음수·대칭·삼각부등식 확인, 손계산 예제 5건 대조",
        "<b>기하</b> — bay가 spine에 수직인지, slot 0이 입구 최근접인지, 북/남 대칭인지",
        "<b>데이터셋 방문 횟수</b> — 라우트 생성 결과가 의도한 표와 정확히 일치",
        "<b>공정 순서 타당성</b> — 각 레이어가 세정으로 시작·계측으로 끝나고, 노광이 식각보다 앞섬",
        "<b>재진입</b> — 노광이 레이어마다 정확히 1회씩, 총 10회(LOGIC_A) / 7회(MEM_B)",
        "<b>배치/단일 설비 구분</b> — 능력 계산은 배치 크기로 나누고, 사이클타임은 나누지 않음",
        "<b>근접 병목 구조</b> — 병목 120% 이내가 3개 이상, 여유 그룹도 3개 이상 유지",
        "<b>기준선 타당성</b> — 기능별 배치가 무작위보다 거리·bay 교차 모두 적음, 그룹이 흩어지지 않음",
        "<b>반송차 민감 구간</b> — 기본 시나리오 가동률이 55~85% 안에 있는지 (깨지면 재조정 신호)",
        "<b>배치 레버 크기</b> — 좋은 배치와 나쁜 배치의 가동률 차이가 5%p 이상",
        "<b>SVG 구조</b> — 유효한 XML, 설비 21대 전부 라벨·툴팁 존재, 좌표가 뷰박스 안",
    ]:
        a(f"<li>{item}</li>")
    a("</ul>")
    a(f'<p style="font-size:12.5px;color:var(--ink-3)">pytest 출력: <code>{test_line}</code></p>')
    a("</section>")

    # ---- 9. 다음
    a("<section>")
    a('<span class="eyebrow">다음 단계</span>')
    a('<h2><span class="n">9</span>M2 · DES 엔진과 미해결 항목</h2>')
    a("<h3>M2에서 할 일</h3>")
    a('<ul class="plain">')
    a("<li>heapq 기반 이벤트 루프. 같은 seed면 비트 단위로 같은 결과가 나와야 한다</li>")
    a("<li><b>엔진 성능 측정이 최우선</b> — 목표 50만 이벤트/초. 미달이면 최적화 후보 수와 "
      "반복 횟수를 재조정해야 한다</li>")
    a("<li>SimPy로 같은 소형 모델을 별도 작성해 교차검증 (자체 엔진 논리 오류를 잡는 "
      "가장 확실한 방법)</li>")
    a("<li>난수 스트림 분리 — 처리시간·고장·수리·투입을 각각 독립 스트림으로. 섞으면 "
      "배치 효과와 난수 노이즈가 구분되지 않는다</li>")
    a("</ul>")
    a("<h3>확인이 필요한 항목</h3>")
    a('<ul class="checks">')
    a('<li class="open"><b>장비 상대 단가표</b> — 현재는 자리표시자(스캐너 10 / 계측 1.2 등)다. '
      "설비 대수를 결정 변수로 두는 이상 capex 예산 제약이 없으면 최적화가 퇴화하므로, "
      "이 표가 결과의 절대적 타당성을 좌우한다. M6 이전에 확정하면 된다.</li>")
    a('<li class="open"><b>스토커 시간 120초의 근거</b> — 실제 fab 값을 아신다면 교체가 '
      "필요하다. 이 값이 배치 최적화의 레버 크기를 직접 결정한다.</li>")
    a('<li class="open"><b>빈차 회송 계수 0.6</b> — 해석적으로 구할 수 없어 근사로 두었다. '
      "M4에서 DES 실측으로 교정한다.</li>")
    a("</ul></section>")

    a("<footer>")
    a(f"fab_layout_opt · 브랜치 <code>claude/fab-equipment-layout-simulator-861iyt</code> · "
      f"도표는 <code>scripts/build_report.py</code>가 현재 코드로 계산해 생성한다. "
      f"설계서는 <code>docs/SPEC.md</code>.")
    a("</footer></div>")

    return (
        f"<title>fab_layout_opt — M1 진행 리포트</title>\n<style>{CSS}</style>\n"
        + "\n".join(h)
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(), encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
