#!/usr/bin/env python3
"""DES 엔진 성능 측정.

**왜 최우선 검증 항목인가.** 최적화 2단 파이프라인(M6)은 후보 K개 × 생산능력 탐색
7회 × 반복 3회 = 수백 회의 DES 실행을 요구한다. 엔진 속도가 곧 "후보를 몇 개나
검증할 수 있는가"를 정한다. 설계서는 50만 이벤트/초를 목표로 잡고 그 위에서 K와
반복 횟수를 계산했으므로, 실측이 크게 다르면 그 수치를 다시 잡아야 한다.

배치 설비는 아직 미구현(M3)이라 확산로가 lot을 모으지 못해 fab 능력이 1/5로
떨어진다. 그대로 재면 이벤트 수가 실제의 1/5밖에 안 나오므로, 배치 그룹을 등가
단일 설비(처리시간 ÷ 배치크기)로 바꾼 변형으로 잰다 — M3 이후의 실제 부하와
같은 규모다.

    python scripts/bench_engine.py [--days 400] [--repeat 3]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fablayout.core.analysis import analytic_capacity  # noqa: E402
from fablayout.core.model import Fab, ToolGroup  # noqa: E402
from fablayout.data.builtin import build_smallfab21  # noqa: E402
from fablayout.sim import SimConfig, simulate  # noqa: E402

TARGET_EVENTS_PER_SEC = 500_000


def batch_equivalent_fab(fab: Fab) -> Fab:
    """배치 설비를 등가 단일 설비로 바꾼 변형.

    처리시간을 배치 크기로 나누면 그룹의 시간당 처리 능력이 같아진다. 대기 거동은
    다르지만(모으는 대기가 없다) 이벤트 수와 흐름 규모는 M3 이후와 같아진다.
    """
    groups = {}
    for gid, g in fab.groups.items():
        if g.mode == "batch":
            groups[gid] = ToolGroup(
                g.gid, g.name, g.count, g.process_minutes / g.batch_size,
                "single", 1, g.mtbf_h, g.mttr_h, g.process_cv, g.family,
            )
        else:
            groups[gid] = g
    return Fab(
        name=fab.name + "-batchequiv",
        groups=groups,
        products=fab.products,
        wafers_per_lot=fab.wafers_per_lot,
        transport=fab.transport,
        source=fab.source + " / 벤치마크용 배치 등가 변형",
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=400.0, help="관측 구간 길이")
    ap.add_argument("--warmup", type=float, default=30.0)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--load", type=float, default=0.92, help="병목 대비 투입률")
    args = ap.parse_args()

    fab = batch_equivalent_fab(build_smallfab21())
    cap = analytic_capacity(fab).capacity_lots_per_day
    cfg = SimConfig(
        release_lots_per_day=cap * args.load,
        warmup_days=args.warmup,
        run_days=args.days,
    )

    print(f"시나리오: {fab.name} · 설비 {fab.total_tools}대 · "
          f"병목 능력 {cap:.2f} lot/일 · 투입 {args.load:.0%} · "
          f"{args.warmup:.0f}+{args.days:.0f}일")
    print()

    rates: list[float] = []
    for i in range(args.repeat):
        r = simulate(fab, replace(cfg, seed=cfg.seed + i * 7919))
        rate = r.events / r.wall_seconds
        rates.append(rate)
        print(f"  {i + 1}회: 이벤트 {r.events:>9,}개 / {r.wall_seconds:6.3f}s "
              f"= {rate:>9,.0f} 이벤트/초   "
              f"(처리량 {r.throughput_lots_per_day:.2f} lot/일, "
              f"X-factor {r.x_factor:.2f}, lot {r.completed}개)")

    median = statistics.median(rates)
    print()
    print(f"중앙값 {median:,.0f} 이벤트/초 (목표 {TARGET_EVENTS_PER_SEC:,})")

    # 최적화 파이프라인이 감당 가능한 규모로 환산
    r = simulate(fab, cfg)
    per_run = r.events / (args.days / 90.0)   # 표준 90일 실행 1회의 이벤트 수
    run_sec = per_run / median
    print(f"  표준 실행(워밍업 30일 + 관측 90일) 1회 ≈ {per_run:>8,.0f} 이벤트 "
          f"/ {run_sec:.2f}s")
    for k in (10, 20, 30):
        total = k * 7 * 3 * run_sec          # 후보 K × 능력탐색 7회 × 반복 3회
        print(f"  후보 {k:>2}개 검증(×7 능력탐색 ×3 반복) = {k * 21:>4}회 실행 "
              f"→ 단일코어 {total / 60:5.1f}분, 8코어 {total / 60 / 8:5.1f}분")

    print()
    if median >= TARGET_EVENTS_PER_SEC:
        print("판정: 목표 달성 — 설계서의 후보 수·반복 횟수를 그대로 쓸 수 있다.")
    else:
        ratio = TARGET_EVENTS_PER_SEC / median
        print(f"판정: 목표의 1/{ratio:.1f} — 절대 실행시간으로 재판단할 것. "
              "이벤트/초가 낮아도 실행 1회가 충분히 짧으면 문제되지 않는다.")


if __name__ == "__main__":
    main()
