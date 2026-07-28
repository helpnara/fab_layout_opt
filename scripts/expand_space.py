#!/usr/bin/env python3
"""탐색공간 확대 실험 — 규모를 키우면 배치 최적화의 여지가 커지는가.

M4의 `smallfab-21`(설비 21대·bay 5개)에서는 배치 레버가 작았다. 규모가 작아서
그런 것인지, 문제 구조가 원래 그런 것인지 구분해야 최적화기 설계가 정해진다.
그래서 3.7배 규모의 `midfab`(설비 77대·bay 12개·slot 96)을 만들고 같은 질문을 다시
던진다.

    1. 규모        두 데이터셋의 실제 크기 대비
    2. 분해        사이클타임 = 순수처리 + 설비 큐 + 반송  → 배치가 손댈 수 있는 최대치
    3. 대수 구성   같은 예산 안에서 대수를 재배분했을 때의 개선폭
    4. 기하 대조   같은 설비를 다른 bay 구성(6bay×16)에 넣었을 때도 결론이 같은가

배치 쪽 증거는 `scripts/calibrate_surrogate.py`가 만든 표본을 그대로 쓴다. 같은
목적함수·같은 시드로 이미 DES를 돌렸으므로 다시 돌릴 이유가 없다.

    python scripts/calibrate_surrogate.py --out results/surrogate_calibration.json
    python scripts/expand_space.py --out results/midfab_space.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from explore_space import rebalance, slack_ranking  # noqa: E402
from fablayout.core.analysis import analytic_capacity, flow_report  # noqa: E402
from fablayout.core.geometry import default_geometry  # noqa: E402
from fablayout.data.builtin import build_smallfab21  # noqa: E402
from fablayout.data.midfab import build_midfab, midfab_geometry  # noqa: E402
from fablayout.opt import baseline  # noqa: E402
from fablayout.opt.anneal import anneal  # noqa: E402
from fablayout.opt.cost import CostModel  # noqa: E402
from fablayout.opt.objective import Candidate, Objective  # noqa: E402
from fablayout.opt.surrogate import Surrogate  # noqa: E402


# ---- 1. 규모 ---------------------------------------------------------------


def scale_rows() -> list[dict]:
    small, sgeo = build_smallfab21(), default_geometry()
    mid, mgeo = build_midfab(), midfab_geometry()
    cost = CostModel()

    def row(label: str, fab, geo) -> dict:
        steps = max(p.step_count for p in fab.products.values())
        return dict(
            label=label, dataset=fab.name, groups=len(fab.groups), tools=fab.total_tools,
            products=len(fab.products), max_steps=steps,
            bays=len(geo.bays), slots=geo.slot_count,
            spine_m=geo.spine_length_m, vehicles=fab.transport.vehicles,
            capex=cost.baseline_capex(fab),
            capacity=analytic_capacity(fab).capacity_lots_per_day,
        )
    return [row("smallfab-21", small, sgeo), row("midfab", mid, mgeo)]


# ---- 2. 사이클타임 분해 ----------------------------------------------------


def decompose(obj: Objective, cand: Candidate) -> dict:
    """사이클타임을 순수처리·설비 큐·반송으로 나눈다.

    **이 분해가 배치 최적화의 천장을 정한다.** 배치가 바꿀 수 있는 것은 반송 항뿐이고
    (설비 큐는 대수와 부하가 정한다), 반송 항을 0으로 만들어도 그 이상은 못 줄인다.
    """
    ev = obj.evaluate(cand)
    if not ev.feasible:
        raise SystemExit(f"기준선이 기각됐다 [{ev.reject.value}] {ev.detail}")
    raw = sum(r.mean_raw_process_hours for r in ev.runs) / len(ev.runs)
    trans = sum(r.mean_transport_hours for r in ev.runs) / len(ev.runs)
    total = ev.cycle_hours
    return dict(
        cycle_hours=total, raw_hours=raw, transport_hours=trans,
        queue_hours=total - raw - trans, x_factor=ev.x_factor,
        vehicle_utilization=ev.vehicle_utilization, capex=ev.capex,
        throughput=ev.throughput,
    )


# ---- 3. 예산 중립 대수 재구성 ----------------------------------------------


SLACK_FLOORS = (1.35, 1.25, 1.15)
"""팔고 난 뒤 남겨야 할 최소 여유. 여러 값을 훑는다.

너무 높게 잡으면 비싼 병목(스캐너 14)을 살 돈이 모이지 않아 후보가 하나도 안 나오고,
너무 낮게 잡으면 판 그룹이 새 병목이 되어 사이클타임이 오히려 나빠진다. 해석적 여유는
대기를 무시하므로 어느 값이 맞는지 사전에 알 수 없다 — 훑어서 DES에 물어본다.
"""


def count_candidates(obj: Objective, sinks: int = 5, depth: int = 2) -> list[dict[str, int]]:
    """같은 예산 안에서 병목에 설비를 더하는 구성들.

    여유가 가장 적은 그룹부터 차례로 "투자 대상"으로 삼고, 필요한 돈은 여유가 큰
    그룹을 팔아 마련한다. 한 번 성공한 구성 위에서 다시 시도해 2대 증설까지 본다.
    """
    base = dict(obj.fab.tool_counts)
    ranked = slack_ranking(obj.fab, base, obj.target_lots_per_day)
    tight = [gid for gid, _ in ranked[-sinks:]]

    out: list[dict[str, int]] = []
    seen = {tuple(sorted(base.items()))}
    frontier = [base]
    for _ in range(depth):
        nxt_frontier = []
        for counts in frontier:
            for gid in tight:
                for floor in SLACK_FLOORS:
                    trial = rebalance(obj, counts, sink=gid, min_slack=floor)
                    if trial is None:
                        continue
                    key = tuple(sorted(trial.items()))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(trial)
                    nxt_frontier.append(trial)
        frontier = nxt_frontier
    return out


def describe(counts: dict[str, int], base: dict[str, int]) -> str:
    up = sorted(g for g in counts if counts[g] > base[g])
    down = sorted(g for g in counts if counts[g] < base[g])
    fmt = lambda gs: " ".join(f"{g}{counts[g] - base[g]:+d}" for g in gs)  # noqa: E731
    return f"{fmt(up)} / {fmt(down)}" if down else fmt(up)


# ---- 실행 -------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--run-days", type=float, default=180.0)
    ap.add_argument("--target", type=float, default=0.90)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    fab, geo = build_midfab(), midfab_geometry()
    obj = Objective.default(fab, geo, target_fraction=args.target,
                            run_days=args.run_days, replications=args.reps)
    base_counts = dict(fab.tool_counts)
    base_cand = obj.baseline_candidate(baseline.family_layout(fab, geo))

    print("=" * 84)
    print("탐색공간 확대 — 규모를 키우면 배치의 여지가 커지는가")
    print("=" * 84)
    for r in scale_rows():
        print(f"  {r['label']:<12} 그룹 {r['groups']:>2} · 설비 {r['tools']:>3}대 · "
              f"bay {r['bays']:>2} · slot {r['slots']:>3} · spine {r['spine_m']:>5.1f}m · "
              f"capex {r['capex']:>6.1f} · 상한 {r['capacity']:.2f} lot/일")

    print(f"\n목적: 사이클타임 최소화 | 제약: capex ≤ {obj.budget.limit:.1f}, "
          f"처리량 ≥ {obj.target_lots_per_day:.2f} lot/일")
    print(f"평가: {args.reps}회 반복 × (워밍업 {obj.sim_config.warmup_days:.0f}일 + "
          f"관측 {args.run_days:.0f}일)")

    # ---- 2. 분해
    t0 = time.perf_counter()
    dec = decompose(obj, base_cand)
    print(f"\n[분해] 기준선(계열별 배치) 사이클타임 {dec['cycle_hours']:.1f}h")
    for label, v in (("순수 처리", dec["raw_hours"]), ("설비 큐 대기", dec["queue_hours"]),
                     ("반송(대기 포함)", dec["transport_hours"])):
        print(f"  {label:<16}{v:>7.1f}h{v / dec['cycle_hours']:>8.1%}")
    print(f"  → 배치가 손댈 수 있는 것은 반송 항뿐이므로 이론적 천장은 "
          f"{dec['transport_hours'] / dec['cycle_hours']:.1%}다.")

    base_ev = obj.evaluate(base_cand)

    # ---- 3. 대수 재구성
    print("\n[대수] 같은 예산 안에서 대수를 재배분한다")
    print(f"  {'구성':<44}{'사이클':>9}{'기준선 대비(쌍대)':>20}{'capex':>8}")
    count_rows = []
    for counts in count_candidates(obj):
        asg = baseline.family_layout(fab, geo, counts)
        ev = obj.evaluate(Candidate(asg, counts, base_cand.vehicles))
        desc = describe(counts, base_counts)
        if not ev.feasible:
            print(f"  {desc:<44}  기각 [{ev.reject.value}]")
            continue
        d, ci = ev.paired_delta(base_ev)
        sig = abs(d) > ci
        print(f"  {desc:<44}{ev.cycle_hours:>8.1f}h{d:>+11.1f} ± {ci:<4.1f}"
              f"{'*' if sig else ' '}{ev.capex:>8.1f}")
        count_rows.append(dict(desc=desc, counts=counts, cycle_hours=ev.cycle_hours,
                               delta=d, ci=ci, significant=sig, capex=ev.capex,
                               throughput=ev.throughput,
                               gain=-d / base_ev.cycle_hours))
    count_rows.sort(key=lambda r: r["cycle_hours"])

    # ---- 4. 기하 대조
    print("\n[기하] 같은 설비를 다른 bay 구성에 넣는다 — 결론이 기하에 의존하는가")
    geo_rows = []
    for cols, pos in ((6, 4), (3, 8)):
        g2 = midfab_geometry(columns=cols, positions_per_side=pos)
        if g2.slot_count < fab.total_tools:
            continue
        o2 = Objective.default(fab, g2, target_fraction=args.target,
                               run_days=args.run_days, replications=args.reps)
        fam = baseline.family_layout(fab, g2)
        fam_ev = o2.evaluate(Candidate(fam, base_counts, fab.transport.vehicles))
        sur = Surrogate.build(fab, g2)
        res = anneal(sur, g2, fam, base_counts, steps=4000, seed=5, restarts=2)
        ann_ev = o2.evaluate(Candidate(res.assignment, base_counts, fab.transport.vehicles))
        d, ci = ann_ev.paired_delta(fam_ev)
        fr_f = flow_report(fab, g2, fam)
        fr_a = flow_report(fab, g2, res.assignment)
        label = f"bay {len(g2.bays)} × slot {2 * pos}"
        print(f"  {label:<18} 계열별 {fam_ev.cycle_hours:6.1f}h → "
              f"담금질 {ann_ev.cycle_hours:6.1f}h  ({d:+.1f} ± {ci:.1f}h"
              f"{'*' if abs(d) > ci else ''})  "
              f"bay교차 {fr_f.interbay_fraction:.0%} → {fr_a.interbay_fraction:.0%} · "
              f"대리지표 {res.gain:+.0%}")
        geo_rows.append(dict(
            label=label, bays=len(g2.bays), slots=g2.slot_count,
            spine_m=g2.spine_length_m,
            family_hours=fam_ev.cycle_hours, anneal_hours=ann_ev.cycle_hours,
            delta=d, ci=ci, significant=abs(d) > ci,
            family_interbay=fr_f.interbay_fraction, anneal_interbay=fr_a.interbay_fraction,
            surrogate_gain=res.gain,
        ))
    elapsed = time.perf_counter() - t0

    # ---- 판정
    best_count = count_rows[0] if count_rows else None
    print("\n" + "=" * 84)
    print("판정")
    print("=" * 84)
    print(f"  배치의 이론적 천장 (반송 비중)      "
          f"{dec['transport_hours'] / dec['cycle_hours']:6.1%}")
    if best_count:
        print(f"  대수 재구성의 실측 개선            {best_count['gain']:+6.1%}  "
              f"({best_count['desc']})")
    print("  → 규모를 키워도 배치의 천장은 반송 비중을 넘지 못한다. 같은 예산 안의 "
          "대수 재구성이\n     한 자릿수 배 더 큰 레버다. 최적화기는 대수 구성을 "
          "주 결정 변수로 두어야 한다.")
    print(f"\n  소요 {elapsed / 60:.1f}분")

    payload = dict(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        target_lots_per_day=obj.target_lots_per_day, budget=obj.budget.limit,
        replications=args.reps, run_days=args.run_days, elapsed_seconds=elapsed,
        scale=scale_rows(), decomposition=dec,
        baseline=dict(cycle_hours=base_ev.cycle_hours, ci=base_ev.cycle_hours_ci,
                      capex=base_ev.capex, throughput=base_ev.throughput,
                      counts=base_counts),
        counts=count_rows, geometries=geo_rows,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
