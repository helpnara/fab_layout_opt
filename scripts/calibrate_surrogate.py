#!/usr/bin/env python3
"""M5 게이트 — 대리지표가 DES의 순위를 재현하는가.

2단 최적화(대리지표로 거르고 DES로 검증)는 **대리지표의 순위가 DES와 맞을 때만**
성립한다. 맞지 않으면 담금질은 DES 기준으로 나쁜 해를 향해 열심히 내려간다. 그래서
최적화기를 신뢰하기 전에 이것부터 잰다.

    귀무가설   대리지표 점수와 DES 사이클타임의 순위상관 ρ < 0.7  → 2단 구조 폐기
    통과 기준  홀드아웃 표본에서 ρ ≥ 0.7

**왜 홀드아웃이 필요한가.** 세 항의 가중치를 표본에 맞춰 고르므로, 같은 표본에서
잰 상관은 반드시 부풀려진다. 가중치를 정하는 데 쓴 표본과 게이트를 판정하는 표본을
나눠야 판정이 정직하다.

**표본의 다양성이 관건이다.** 좋은 배치들만 모으면 순위상관은 잡음만 잰다. 나쁜
배치(흩뿌림·무작위)부터 좋은 배치(계열별·흐름별·담금질)까지 품질 범위를 넓게 덮도록
후보를 구성한다. 담금질 궤적 중간을 잘라 쓰면 그 사이가 채워진다.

    python scripts/calibrate_surrogate.py --out results/surrogate_calibration.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fablayout.core.analysis import flow_report  # noqa: E402
from fablayout.core.model import Assignment  # noqa: E402
from fablayout.data.midfab import build_midfab, midfab_geometry  # noqa: E402
from fablayout.opt import baseline  # noqa: E402
from fablayout.opt.anneal import anneal  # noqa: E402
from fablayout.opt.construct import flow_layout  # noqa: E402
from fablayout.opt.moves import BayPlan, neighbor  # noqa: E402
from fablayout.opt.objective import Candidate, Objective  # noqa: E402
from fablayout.opt.surrogate import Surrogate, spearman  # noqa: E402

GATE = 0.70
"""통과 기준. 이 밑이면 2단 구조를 버린다."""


# ---- 표본 구성 -------------------------------------------------------------


def sample_layouts(fab, geo, counts, n_random: int, seed: int) -> list[tuple[str, Assignment]]:
    """품질 범위를 넓게 덮는 배치 표본.

    구성 방식이 다른 배치를 섞고, 좋은 배치에서 출발한 무작위 걸음의 중간 지점을
    끼워 넣어 좋은 쪽과 나쁜 쪽 사이를 채운다.
    """
    out: list[tuple[str, Assignment]] = [
        ("기능별", baseline.functional_layout(fab, geo, counts)),
        ("계열별", baseline.family_layout(fab, geo, counts)),
        ("흐름별", flow_layout(fab, geo, counts)),
        ("흩뿌림", baseline.spread_layout(fab, geo, counts)),
    ]
    rng = random.Random(seed)
    for i in range(n_random):
        out.append((f"무작위 {i + 1}", baseline.random_layout(fab, geo, counts,
                                                             seed=rng.randrange(10**6))))

    # 계열별에서 출발한 무작위 걸음 — 걸음 수가 늘수록 나빠지므로 중간 품질을 채운다
    plan = BayPlan.from_assignment(baseline.family_layout(fab, geo, counts), geo)
    for k, walk in enumerate((2, 5, 10, 20, 40)):
        cur = plan
        for _ in range(walk):
            nxt = neighbor(cur, rng)
            if nxt is not None:
                cur = nxt
        out.append((f"교란 {walk}수", cur.to_assignment(geo)))

    # 담금질 해 — 대리지표가 좋다고 보는 쪽 끝을 표본에 넣어야 게이트가 의미를 갖는다
    sur = Surrogate.build(fab, geo)
    for s in (1, 2):
        res = anneal(sur, geo, baseline.family_layout(fab, geo, counts),
                     counts, steps=3000, seed=s)
        out.append((f"담금질 s{s}", res.assignment))
    return out


# ---- 가중치 적합 -----------------------------------------------------------


def _scales(rows: list[dict]) -> tuple[float, float, float]:
    """항별 표본 표준편차. 단위가 다른 세 항을 같은 눈금에 놓아야 격자 탐색이 된다."""
    def sd(key: str) -> float:
        vs = [r[key] for r in rows]
        m = sum(vs) / len(vs)
        v = sum((x - m) ** 2 for x in vs) / max(len(vs) - 1, 1)
        return max(v ** 0.5, 1e-9)
    return sd("stocker"), sd("travel"), sd("split")


def fit_weights(rows: list[dict], grid: int = 12) -> tuple[tuple[float, float, float], float]:
    """단체(simplex) 격자에서 순위상관을 최대화하는 가중치.

    반환값은 **원 단위**의 가중치라 `Surrogate`에 그대로 넣을 수 있다.
    """
    ss, st, sp = _scales(rows)
    ys = [r["cycle_hours"] for r in rows]
    best, best_rho = (1.0, 1.0, 1.0), -2.0
    for i in range(grid + 1):
        for j in range(grid + 1 - i):
            k = grid - i - j
            a, b, c = i / grid, j / grid, k / grid
            xs = [a * r["stocker"] / ss + b * r["travel"] / st + c * r["split"] / sp
                  for r in rows]
            rho = spearman(xs, ys)
            if rho > best_rho:
                best_rho, best = rho, (a / ss, b / st, c / sp)
    return best, best_rho


def score_with(rows: list[dict], w: tuple[float, float, float]) -> float:
    xs = [w[0] * r["stocker"] + w[1] * r["travel"] + w[2] * r["split"] for r in rows]
    return spearman(xs, [r["cycle_hours"] for r in rows])


def cross_validated_rho(rows: list[dict], splits: int, seed: int) -> tuple[list[float], list[tuple[float, float, float]]]:
    """무작위 분할을 반복해 홀드아웃 순위상관의 **분포**를 낸다.

    표본이 20여 개뿐이라 분할 한 번의 홀드아웃 상관은 그 자체가 잡음이다 — 한 번만
    재고 판정하면 분할 운에 따라 통과와 미달이 갈린다. 반복해서 중앙값으로 판정한다.
    """
    rhos: list[float] = []
    weights: list[tuple[float, float, float]] = []
    for k in range(splits):
        order = list(range(len(rows)))
        random.Random(seed + k * 7919).shuffle(order)
        cut = len(order) * 3 // 5
        train = [rows[i] for i in order[:cut]]
        test = [rows[i] for i in order[cut:]]
        w, _ = fit_weights(train, grid=8)
        rhos.append(score_with(test, w))
        weights.append(w)
    rhos.sort()
    return rhos, weights


def resolvable_accuracy(rows: list[dict], w: tuple[float, float, float]) -> tuple[float, int]:
    """**DES가 실제로 구분할 수 있는 쌍**에서만 순서 정확도를 잰다.

    순위상관은 사이클타임 차이가 신뢰구간 안에 있는 쌍까지 채점한다. 그 쌍의 "정답"은
    잡음이라, 대리지표가 아무리 좋아도 절반은 틀리게 나온다. 즉 ρ는 대리지표의 성능과
    DES의 분해능을 섞어 재고 있다.

    그래서 두 후보의 신뢰구간이 겹치지 않는 쌍만 골라 채점한다. 이것이 대리지표에
    실제로 요구하는 능력이다 — **구분 가능한 것을 구분하는가**.
    """
    def sc(r: dict) -> float:
        return w[0] * r["stocker"] + w[1] * r["travel"] + w[2] * r["split"]

    ok = total = 0
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            gap = abs(a["cycle_hours"] - b["cycle_hours"])
            if gap <= a["cycle_ci"] + b["cycle_ci"]:
                continue                      # DES도 못 가르는 쌍은 채점하지 않는다
            total += 1
            if (sc(a) - sc(b)) * (a["cycle_hours"] - b["cycle_hours"]) > 0:
                ok += 1
    return (ok / total if total else 0.0), total


# ---- 실행 -------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", type=int, default=10)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--run-days", type=float, default=180.0)
    ap.add_argument("--target", type=float, default=0.90)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--splits", type=int, default=200,
                    help="교차검증 분할 반복 수")
    ap.add_argument("--from", dest="reuse", type=Path, default=None,
                    help="이미 측정한 JSON의 표본으로 판정만 다시 한다")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    fab, geo = build_midfab(), midfab_geometry()
    obj = Objective.default(fab, geo, target_fraction=args.target,
                            run_days=args.run_days, replications=args.reps)
    sur = Surrogate.build(fab, geo)
    counts = dict(fab.tool_counts)

    if args.reuse:
        prev = json.loads(args.reuse.read_text(encoding="utf-8"))
        report(prev["rows"], args, prev["elapsed_seconds"], obj, fab, geo)
        return

    samples = sample_layouts(fab, geo, counts, args.random, args.seed)
    print(f"표본 {len(samples)}개 · DES {args.reps}회 × {args.run_days:.0f}일 "
          f"· 목표 {obj.target_lots_per_day:.2f} lot/일")
    print(f"{'배치':<12}{'대리지표':>10}{'스토커':>9}{'주행':>8}{'분할':>9}"
          f"{'DES 사이클':>12}{'bay교차':>9}")
    print("-" * 72)

    rows: list[dict] = []
    t0 = time.perf_counter()
    for name, asg in samples:
        ev = obj.evaluate(Candidate(asg, counts, fab.transport.vehicles))
        if not ev.feasible:
            print(f"{name:<12}  기각 [{ev.reject.value}] {ev.detail}")
            continue
        t = sur.terms(asg, counts)
        fr = flow_report(fab, geo, asg)
        rows.append(dict(
            name=name, stocker=t.stocker_minutes, travel=t.travel_minutes,
            split=t.split_penalty, interbay_moves=t.interbay_moves,
            split_groups=t.split_groups, cycle_hours=ev.cycle_hours,
            cycle_ci=ev.cycle_hours_ci, throughput=ev.throughput,
            interbay_fraction=fr.interbay_fraction,
        ))
        print(f"{name:<12}{t.score(1, 1, 1):>10.1f}{t.stocker_minutes:>9.1f}"
              f"{t.travel_minutes:>8.1f}{t.split_penalty:>9.1f}"
              f"{ev.cycle_hours:>10.1f}h{t.interbay_moves:>9.1f}")
    elapsed = time.perf_counter() - t0

    if len(rows) < 8:
        raise SystemExit(f"표본이 부족하다 ({len(rows)}개) — 게이트를 판정할 수 없다")

    report(rows, args, elapsed, obj, fab, geo)


def report(rows, args, elapsed, obj, fab, geo) -> None:
    """측정된 표본으로 게이트를 판정하고 결과를 남긴다."""
    ss, st, sp = _scales(rows)
    equal_w = (1.0 / ss, 1.0 / st, 1.0 / sp)     # 항별 표준편차로 정규화한 등가중치
    equal_rho = score_with(rows, equal_w)

    full_w, full_rho = fit_weights(rows)
    rhos, weights = cross_validated_rho(rows, args.splits, args.seed)
    cv_rho = rhos[len(rhos) // 2]
    cv_lo, cv_hi = rhos[len(rhos) // 10], rhos[-len(rhos) // 10 - 1]

    term_rho = {
        k: spearman([r[k] for r in rows], [r["cycle_hours"] for r in rows])
        for k in ("stocker", "travel", "split")
    }

    print("-" * 72)
    print(f"소요 {elapsed / 60:.1f}분 · 유효 표본 {len(rows)}개\n")
    print("  항별 단독 순위상관")
    for k, label in (("stocker", "스토커"), ("travel", "주행"), ("split", "풀 분할")):
        print(f"    {label:<8}{term_rho[k]:+.3f}")
    print(f"\n  정규화 등가중치 전체 ρ        {equal_rho:+.3f}")
    print(f"  전체 적합 ρ (참고, 낙관적)     {full_rho:+.3f}")
    print(f"  교차검증 홀드아웃 ρ 중앙값     {cv_rho:+.3f}   "
          f"[10~90분위 {cv_lo:+.3f} ~ {cv_hi:+.3f}]   ← 게이트")
    passed = cv_rho >= GATE
    print(f"\n  판정: {'통과' if passed else '미달'} (기준 ρ ≥ {GATE:.2f})")

    # DES가 실제로 구분할 수 있는 쌍에서의 정확도
    acc, n_pairs = resolvable_accuracy(rows, full_w)
    all_pairs = len(rows) * (len(rows) - 1) // 2
    print(f"\n  구분 가능한 쌍에서의 순서 정확도  {acc:.1%} "
          f"({n_pairs}/{all_pairs}쌍 — 나머지는 DES도 못 가른다)")

    # 좋은 배치들 안에서의 분해능
    good = sorted(rows, key=lambda r: r["cycle_hours"])[: max(len(rows) // 2, 4)]
    good_rho = score_with(good, full_w)
    span = good[-1]["cycle_hours"] - good[0]["cycle_hours"]
    good_ci = sum(r["cycle_ci"] for r in good) / len(good)
    good_acc, good_pairs = resolvable_accuracy(good, full_w)
    print(f"  상위 절반({len(good)}개) 안에서의 ρ    {good_rho:+.3f} "
          f"(폭 {span:.1f}h vs 평균 신뢰구간 ±{good_ci:.1f}h · "
          f"구분 가능한 쌍 {good_pairs}/{len(good) * (len(good) - 1) // 2})")

    print()
    if passed:
        print("  → 2단 구조 유지. 대리지표로 거르고 상위 소수만 DES로 검증한다.")
    else:
        print("  → 대리지표를 **순위 매기기**에는 쓸 수 없다. 다만 나쁜 배치를 "
              "걸러내는 필터로는")
        print("     쓸 수 있다(위 정확도 참조). M6에서 배치는 필터 수준으로 다루고, "
              "주 결정 변수를")
        print("     대수 구성으로 옮긴다.")

    payload = dict(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dataset=fab.name, tools=fab.total_tools, slots=geo.slot_count,
        target_lots_per_day=obj.target_lots_per_day,
        replications=args.reps, run_days=args.run_days, elapsed_seconds=elapsed,
        gate=GATE, passed=passed,
        equal_rho=equal_rho, full_rho=full_rho,
        cv_rho=cv_rho, cv_lo=cv_lo, cv_hi=cv_hi, splits=args.splits,
        term_rho=term_rho,
        resolvable_accuracy=acc, resolvable_pairs=n_pairs, all_pairs=all_pairs,
        good_rho=good_rho, good_span_hours=span, good_ci=good_ci,
        good_accuracy=good_acc, good_resolvable_pairs=good_pairs,
        good_all_pairs=len(good) * (len(good) - 1) // 2,
        weights=dict(stocker=full_w[0], travel=full_w[1], split=full_w[2]),
        rows=rows,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
