"""명령줄 인터페이스.

    fablayout evaluate            기준선을 목표 함수로 평가한다
    fablayout compare             기준선 배치 3종을 쌍대 비교한다
    fablayout capacity            지속 가능한 최대 처리량을 찾는다
    fablayout simulate            한 지점에서 시뮬레이션하고 지표를 낸다
    fablayout report              배치도·지표 도표가 담긴 HTML 리포트를 만든다
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from .core.analysis import analytic_capacity, flow_report
from .core.geometry import default_geometry
from .data.builtin import build_smallfab21
from .opt import baseline
from .opt.objective import Candidate, Objective
from .sim import SimConfig, simulate
from .sim.capacity import find_capacity


def _fab_and_geo(args):
    fab = build_smallfab21(vehicles=args.vehicles)
    return fab, default_geometry()


def _objective(args, fab, geo) -> Objective:
    return Objective.default(
        fab, geo,
        target_fraction=args.target,
        warmup_days=args.warmup_days,
        run_days=args.run_days,
        replications=args.reps,
    )


# ---- 명령 ----------------------------------------------------------------


def cmd_simulate(args) -> int:
    fab, geo = _fab_and_geo(args)
    asg = baseline.build(args.layout, fab, geo, seed=args.seed)
    bound = analytic_capacity(fab).capacity_lots_per_day
    rate = args.rate if args.rate else bound * args.target
    r = simulate(
        fab,
        SimConfig(release_lots_per_day=rate, warmup_days=args.warmup_days,
                  run_days=args.run_days, seed=args.seed),
        asg, geo,
    )
    print(f"시나리오: {fab.name} · {args.layout} 배치 · 반송차 {args.vehicles}대")
    print(f"투입 {rate:.2f} lot/일 (해석적 상한 {bound:.2f}의 {rate / bound:.0%})")
    print()
    print(r.summary())
    print()
    print(f"{'그룹':<11} {'가동률':>7} {'가용대비':>8} {'큐대기':>9} {'평균배치':>8}")
    for gid, g in sorted(r.groups.items(),
                         key=lambda kv: -kv[1].load_factor(r.window_minutes)):
        print(f"{gid:<11} {g.utilization(r.window_minutes):>6.0%} "
              f"{g.load_factor(r.window_minutes):>7.0%} "
              f"{g.mean_queue_wait:>8.1f}분 {g.mean_batch_size:>7.2f}")
    if r.transport:
        t = r.transport
        print(f"\n반송: 가동률 {t.utilization(r.window_minutes):.0%} · "
              f"평균 대기 {t.mean_wait_minutes:.1f}분 · "
              f"이동 {t.moves:,}회 · 스토커 {t.stocker_ops:,}회 · "
              f"주행 {t.distance_m / 1000:.0f}km")
    return 0


def cmd_evaluate(args) -> int:
    fab, geo = _fab_and_geo(args)
    obj = _objective(args, fab, geo)
    asg = baseline.build(args.layout, fab, geo, seed=args.seed)
    ev = obj.evaluate(obj.baseline_candidate(asg))
    print(f"목적: 사이클타임 최소화")
    print(f"제약: capex ≤ {obj.budget.limit:.1f} · "
          f"처리량 ≥ {obj.target_lots_per_day:.2f} lot/일")
    print(f"평가: {obj.replications}회 반복 × "
          f"(워밍업 {args.warmup_days:.0f}일 + 관측 {args.run_days:.0f}일)")
    print()
    print(f"{args.layout}: {ev.summary()}")
    if ev.feasible:
        fr = flow_report(fab, geo, asg)
        print(f"  bay 교차 {fr.interbay_moves_per_lot:.0f}/{fr.moves_per_lot:.0f}회 "
              f"({fr.interbay_fraction:.0%}) · 총 주행 {fr.distance_m_per_lot / 1000:.2f}km/lot")
        for item, n, amt in obj.budget.model.breakdown(
            obj.fab.tool_counts, obj.fab.transport.vehicles
        )[:3]:
            print(f"  capex 상위: {item} ×{n} = {amt:.1f}")
    return 0 if ev.feasible else 1


def cmd_compare(args) -> int:
    fab, geo = _fab_and_geo(args)
    obj = _objective(args, fab, geo)
    base = obj.baseline_candidate(baseline.functional_layout(fab, geo))
    base_ev = obj.evaluate(base)
    print(f"목표 처리량 {obj.target_lots_per_day:.2f} lot/일 · "
          f"예산 {obj.budget.limit:.1f} · {obj.replications}회 반복")
    print(f"\n{'배치안':<12} {'사이클타임':>10} {'기준선 대비(쌍대)':>20} "
          f"{'처리량':>8} {'WIP':>6}")
    for kind in ("functional", "spread", "random"):
        asg = baseline.build(kind, fab, geo, seed=args.seed)
        ev = obj.evaluate(Candidate(asg, base.counts, base.vehicles))
        if not ev.feasible:
            print(f"{kind:<12} 기각 [{ev.reject.value}] {ev.detail}")
            continue
        d, ci = ev.paired_delta(base_ev)
        mark = "*" if abs(d) > ci else " "
        note = "" if kind == "functional" else f"{d:+7.1f}±{ci:4.1f}h{mark}"
        print(f"{kind:<12} {ev.cycle_hours:>9.1f}h {note:>20} "
              f"{ev.throughput:>8.2f} {ev.wip:>6.0f}")
    print("\n* = 쌍대 차이가 95% 신뢰구간을 넘음 (같은 시드끼리 짝지어 비교)")
    return 0


def cmd_capacity(args) -> int:
    fab, geo = _fab_and_geo(args)
    asg = baseline.build(args.layout, fab, geo, seed=args.seed)
    cfg = SimConfig(release_lots_per_day=1.0, warmup_days=args.warmup_days,
                    run_days=args.run_days, seed=args.seed)
    res = find_capacity(fab, cfg, asg, geo)
    print(f"해석적 상한        {res.analytic_bound:.2f} lot/일")
    print(f"지속 가능 최대     {res.capacity_lots_per_day:.2f} lot/일 "
          f"({res.capacity_wafers_per_day:.0f} 웨이퍼/일, 상한의 "
          f"{res.utilization_of_bound:.0%})")
    print(f"최초 실패 지점     {res.first_failure_lots_per_day:.2f} lot/일")
    print(f"그때의 사이클타임  {res.cycle_hours_at_capacity:.0f}시간")
    print(f"\n{'투입률':>8} {'완료율':>8} {'WIP드리프트':>12} {'사이클타임':>10}  판정")
    for rate, v in res.probes:
        mark = "지속" if v.sustainable else f"실패 — {v.reason}"
        print(f"{rate:>8.2f} {v.throughput_ratio:>8.3f} {v.wip_drift:>+11.0%} "
              f"{v.cycle_hours:>9.0f}h  {mark}")
    return 0


def cmd_report(args) -> int:
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts"))
    import build_report  # type: ignore

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report.build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


# ---- 진입점 --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fablayout", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vehicles", type=int, default=2)
    ap.add_argument("--warmup-days", type=float, default=60.0)
    ap.add_argument("--run-days", type=float, default=180.0)
    ap.add_argument("--target", type=float, default=0.90,
                    help="목표 처리량 (해석적 상한 대비)")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260727)
    ap.add_argument("--layout", default="functional",
                    choices=["functional", "spread", "random"])

    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate", help="한 지점 시뮬레이션")
    s.add_argument("--rate", type=float, default=0.0, help="투입률 (미지정 시 --target)")
    s.set_defaults(func=cmd_simulate)
    sub.add_parser("evaluate", help="목표 함수로 평가").set_defaults(func=cmd_evaluate)
    sub.add_parser("compare", help="기준선 3종 쌍대 비교").set_defaults(func=cmd_compare)
    sub.add_parser("capacity", help="지속 가능 최대 처리량 탐색").set_defaults(func=cmd_capacity)
    r = sub.add_parser("report", help="HTML 리포트 생성")
    r.add_argument("--out", default="report.html")
    r.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
