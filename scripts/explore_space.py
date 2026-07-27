#!/usr/bin/env python3
"""탐색공간 사전 점검 — "최적화할 가치가 있는가"를 먼저 확인한다.

최적화기를 만들기 전에 답해야 할 질문이 있다. **기준선이 이미 거의 최선이면 최적화는
헛수고다.** M5의 대리지표 게이트보다 앞서 확인해야 할 것이 이것이다.

세 갈래로 나눠 본다.

    A. 배치만 바꿀 때의 분포     대수 구성을 고정하고 배치를 무작위로 여러 개
                                 → 기준선이 그 분포의 어디쯤인가?
    B. 대수 구성만 바꿀 때        예산 중립 재구성 (여유 있는 그룹을 줄여 병목에 투자)
                                 → 배치보다 큰 레버인가?
    C. 둘을 합쳤을 때             가장 좋은 대수 구성 위에서 배치를 다시 탐색

    python scripts/explore_space.py [--random 20] [--climb 20]
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fablayout.core.analysis import analytic_capacity  # noqa: E402
from fablayout.core.geometry import default_geometry  # noqa: E402
from fablayout.core.model import Assignment, Fab  # noqa: E402
from fablayout.data.builtin import build_smallfab21  # noqa: E402
from fablayout.opt import baseline  # noqa: E402
from fablayout.opt.objective import Candidate, Evaluation, Objective  # noqa: E402


# ---- 예산 중립 대수 재구성 ------------------------------------------------


def slack_ranking(fab: Fab, counts: dict[str, int], target: float) -> list[tuple[str, float]]:
    """그룹별 여유 = 해석적 능력 ÷ 목표 처리량. 클수록 줄일 여지가 있다."""
    rep = analytic_capacity(fab, counts)
    return sorted(
        ((g.gid, g.capacity_lots_per_day / target) for g in rep.groups),
        key=lambda kv: -kv[1],
    )


def rebalance(
    obj: Objective,
    counts: dict[str, int],
    vehicles: int | None = None,
    sink: str | None = None,
    min_slack: float = 1.30,
) -> dict[str, int] | None:
    """병목 그룹에 1대를 더하고, 필요한 예산은 여유 그룹을 팔아 마련한다.

    한 대 팔아 한 대 사는 1:1 교환만 허용하면 비싼 병목(스캐너 10)을 사기 위해 싼 그룹을
    여러 개 파는 조합을 못 만들고, 첫 단계에서 탐색이 멈춘다. 그래서 **살 돈이 모일
    때까지 여유가 큰 순서로 판다.**

    `min_slack`은 팔고 난 뒤에도 남겨야 할 여유(해석적 능력 ÷ 목표 처리량)다. 목표
    바로 위까지 깎으면 그 그룹이 새 병목이 되어 사이클타임이 오히려 나빠진다.
    해석적 여유는 대기를 무시하므로 1.12(부하 89%)로는 턱없이 부족하다 — 실측에서
    그 값으로 재구성했더니 사이클타임이 24% 나빠졌다. 1.30(부하 77%)을 기본으로 둔다.
    """
    v = obj.fab.transport.vehicles if vehicles is None else vehicles
    target = obj.target_lots_per_day
    ranked = slack_ranking(obj.fab, counts, target)
    tight = sink or ranked[-1][0]
    cost = obj.budget.model

    trial = dict(counts)
    need = cost.marginal(tight) - obj.budget.headroom(trial, v)
    for gid, _ in ranked:
        if need <= 1e-9:
            break
        if gid == tight:
            continue
        while need > 1e-9 and trial[gid] > 1:
            probe = dict(trial)
            probe[gid] -= 1
            after = analytic_capacity(obj.fab, probe).by_gid(gid).capacity_lots_per_day / target
            if after < min_slack:
                break
            trial = probe
            need -= cost.marginal(gid)
    if need > 1e-9:
        return None

    trial[tight] += 1
    if not obj.budget.fits(trial, v) or sum(trial.values()) > obj.geo.slot_count:
        return None
    if analytic_capacity(obj.fab, trial).capacity_lots_per_day < target:
        return None
    return trial


def rebalance_chain(obj: Objective, rounds: int = 6) -> list[dict[str, int]]:
    """재구성을 반복 적용해 후보 사슬을 만든다."""
    out = []
    counts = dict(obj.fab.tool_counts)
    seen = {tuple(sorted(counts.items()))}
    for _ in range(rounds):
        nxt = rebalance(obj, counts)
        key = tuple(sorted(nxt.items())) if nxt else None
        if nxt is None or key in seen:
            break
        seen.add(key)
        counts = nxt
        out.append(dict(counts))
    return out


# ---- 배치 탐색 -------------------------------------------------------------


def random_layouts(obj: Objective, counts: dict[str, int], n: int, seed: int = 0):
    rng = random.Random(seed)
    for _ in range(n):
        yield baseline.random_layout(obj.fab, obj.geo, counts, seed=rng.randrange(10**6))


def swap_neighbor(asg: Assignment, geo, rng: random.Random) -> Assignment:
    """설비 두 대의 자리를 바꾸거나, 한 대를 빈 자리로 옮긴다."""
    place = dict(asg.placement)
    tools = list(place)
    if rng.random() < 0.6 or len(place) == geo.slot_count:
        a, b = rng.sample(tools, 2)
        place[a], place[b] = place[b], place[a]
    else:
        empty = [s for s in geo.slots if s not in set(place.values())]
        place[rng.choice(tools)] = rng.choice(empty)
    return Assignment(place)


def hill_climb(obj: Objective, start: Candidate, steps: int, seed: int = 0,
               require_significant: bool = True):
    """DES를 직접 쓰는 단순 등반. 개선 여지가 있는지 확인하는 용도다.

    `require_significant`가 참이면 **쌍대 비교로 유의한 개선만** 수용한다. 잡음 섞인
    평가를 그냥 비교하면 운 좋은 평가를 골라 내려가는 편향이 생겨, 실제로 개선하지
    않고도 개선한 것처럼 보인다.
    """
    rng = random.Random(seed)
    best, best_eval = start, obj.evaluate(start)
    history = [best_eval.cycle_hours]
    accepted = 0
    for _ in range(steps):
        cand = Candidate(swap_neighbor(best.assignment, obj.geo, rng),
                         best.counts, best.vehicles)
        ev = obj.evaluate(cand)
        take = (ev.significantly_better_than(best_eval) if require_significant
                else (ev.feasible and ev.cycle_hours < best_eval.cycle_hours))
        if take:
            best, best_eval = cand, ev
            accepted += 1
        history.append(best_eval.cycle_hours)
    return best, best_eval, history, accepted


# ---- 보고 -----------------------------------------------------------------


def line(label: str, ev: Evaluation, base: Evaluation | None = None) -> str:
    """쌍대 차이로 보고한다 — 같은 시드끼리 짝지어야 잡음이 상쇄된다."""
    if not ev.feasible:
        return f"  {label:<32} 기각 [{ev.reject.value}] {ev.detail}"
    cmp_ = " " * 20
    if base is not None and base.feasible:
        d, ci = ev.paired_delta(base)
        mark = "*" if abs(d) > ci else " "
        cmp_ = f"  {d:+6.1f}±{ci:4.1f}h{mark}"
    return (f"  {label:<32} {ev.cycle_hours:6.1f}h{cmp_}"
            f"  처리량 {ev.throughput:.2f}  capex {ev.capex:5.1f}  WIP {ev.wip:4.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", type=int, default=16, help="무작위 배치 개수")
    ap.add_argument("--climb", type=int, default=24, help="등반 스텝 수")
    ap.add_argument("--target", type=float, default=0.90, help="목표 처리량 (상한 대비)")
    ap.add_argument("--run-days", type=float, default=200.0)
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()

    fab, geo = build_smallfab21(), default_geometry()
    obj = Objective.default(fab, geo, target_fraction=args.target,
                            run_days=args.run_days, replications=args.reps)
    bound = analytic_capacity(fab).capacity_lots_per_day

    print("=" * 92)
    print("탐색공간 사전 점검 — 최적화가 기준선을 이길 여지가 있는가")
    print("=" * 92)
    print(f"목적: 사이클타임 최소화 | 제약: capex ≤ {obj.budget.limit:.1f}, "
          f"처리량 ≥ {obj.target_lots_per_day:.2f} lot/일 (해석적 상한 {bound:.2f}의 "
          f"{args.target:.0%})")
    print(f"평가: {args.reps}회 반복 × (워밍업 {obj.sim_config.warmup_days:.0f}일 + "
          f"관측 {args.run_days:.0f}일)\n")

    base_cand = obj.baseline_candidate(baseline.functional_layout(fab, geo))
    base_eval = obj.evaluate(base_cand)
    print("[기준선] 기능별 배치 · 기준 대수 구성")
    print(line("functional", base_eval))
    ref = base_eval.cycle_hours

    # ---- A. 배치만
    print(f"\n[A] 대수 고정, 배치만 바꾼다 — 무작위 {args.random}개")
    vals = []
    for i, asg in enumerate(random_layouts(obj, base_cand.counts, args.random, seed=7)):
        ev = obj.evaluate(Candidate(asg, base_cand.counts, base_cand.vehicles))
        if ev.feasible:
            vals.append(ev.cycle_hours)
    for name, asg in (("spread", baseline.spread_layout(fab, geo)),):
        ev = obj.evaluate(Candidate(asg, base_cand.counts, base_cand.vehicles))
        print(line(name, ev, base_eval))
    if vals:
        vals.sort()
        print(f"  {'무작위 배치 분포':<34} 최선 {vals[0]:.1f}h · 중앙 "
              f"{statistics.median(vals):.1f}h · 최악 {vals[-1]:.1f}h  (n={len(vals)})")
        better = sum(1 for v in vals if v < ref)
        print(f"  → 기준선({ref:.1f}h)보다 나은 무작위 배치: {better}/{len(vals)}개, "
              f"기준선은 상위 {(better + 0.5) / len(vals):.0%} 지점")

    # ---- B. 대수 구성만
    print("\n[B] 배치 고정(기능별), 예산 중립 대수 재구성")
    best_counts, best_counts_eval = base_cand.counts, base_eval
    for i, counts in enumerate(rebalance_chain(obj), start=1):
        asg = baseline.functional_layout(fab, geo, counts)
        ev = obj.evaluate(Candidate(asg, counts, base_cand.vehicles))
        diff = {g: counts[g] - base_cand.counts[g] for g in counts
                if counts[g] != base_cand.counts[g]}
        desc = " ".join(f"{g}{n:+d}" for g, n in sorted(diff.items()))
        print(line(f"{i}단계 {desc}", ev, base_eval))
        if ev.feasible and ev.cycle_hours < best_counts_eval.cycle_hours:
            best_counts, best_counts_eval = counts, ev

    # 반송차 증설 — 예산은 여유 설비를 팔아 마련한다
    print("\n[B'] 반송차 대수 (증설분은 여유 설비를 팔아 예산 안에서 확보)")
    for v in (1, 3, 4):
        counts = dict(base_cand.counts)
        need = obj.budget.model.marginal("VEHICLE") * (v - base_cand.vehicles)
        ok = True
        if need > obj.budget.headroom(counts, v - (v - base_cand.vehicles)):
            for gid, _ in slack_ranking(fab, counts, obj.target_lots_per_day):
                while (not obj.budget.fits(counts, v)) and counts[gid] > 1:
                    probe = dict(counts)
                    probe[gid] -= 1
                    slack = (analytic_capacity(fab, probe).by_gid(gid).capacity_lots_per_day
                             / obj.target_lots_per_day)
                    if slack < 1.30:
                        break
                    counts = probe
                if obj.budget.fits(counts, v):
                    break
            ok = obj.budget.fits(counts, v)
        if not ok:
            print(f"  {'반송차 ' + str(v) + '대':<34} 예산 안에서 확보 불가")
            continue
        diff = {g: counts[g] - base_cand.counts[g] for g in counts
                if counts[g] != base_cand.counts[g]}
        desc = " ".join(f"{g}{n:+d}" for g, n in sorted(diff.items())) or "대수 변경 없음"
        asg = baseline.functional_layout(fab, geo, counts)
        ev = obj.evaluate(Candidate(asg, counts, v))
        print(line(f"반송차 {v}대 {desc}", ev, base_eval))

    # ---- C. 결합
    print(f"\n[C] 최선 대수 구성 위에서 배치 등반 — {args.climb}스텝")
    start = Candidate(baseline.functional_layout(fab, geo, best_counts),
                      best_counts, base_cand.vehicles)
    start_eval = obj.evaluate(start)
    _, climbed, history, accepted = hill_climb(obj, start, args.climb, seed=11)
    print(line("등반 결과", climbed, base_eval))
    d, ci = climbed.paired_delta(start_eval)
    print(f"  → 시작 {history[0]:.1f}h → {history[-1]:.1f}h · 수용 {accepted}/{args.climb}회 "
          f"· 쌍대 차이 {d:+.1f}±{ci:.1f}h "
          f"({'유의한 개선' if d < -ci else '유의하지 않음'})")

    # 대조군: 유의성 없이 탐욕적으로 받아들이면 얼마나 부풀려지는가
    _, greedy, ghist, gacc = hill_climb(obj, start, args.climb, seed=11,
                                        require_significant=False)
    gd, gci = greedy.paired_delta(start_eval)
    print(f"  [대조] 탐욕적 수용: {ghist[-1]:.1f}h · 수용 {gacc}/{args.climb}회 "
          f"· 쌍대 차이 {gd:+.1f}±{gci:.1f}h "
          f"({'유의' if gd < -gci else '유의하지 않음 — 잡음을 탄 것'})")

    # ---- 결론
    print("\n" + "=" * 92)
    print("판정")
    print("=" * 92)
    layout_lever = (max(vals) - min(vals)) / ref if vals else 0.0
    count_lever = (ref - best_counts_eval.cycle_hours) / ref
    total = (ref - min(climbed.cycle_hours, best_counts_eval.cycle_hours)) / ref
    print(f"  배치만의 레버 (무작위 최선~최악 폭)   {layout_lever:6.1%}")
    print(f"  대수 재구성의 레버                     {count_lever:+6.1%}")
    print(f"  결합 최선 vs 기준선                    {total:+6.1%}")
    print(f"  기준선 사이클타임                      {ref:.1f}h")
    print(f"  달성 최선                              "
          f"{min(climbed.cycle_hours, best_counts_eval.cycle_hours):.1f}h")


if __name__ == "__main__":
    main()
