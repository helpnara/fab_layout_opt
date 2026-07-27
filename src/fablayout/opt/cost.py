"""설비 투자비(capex) 모델과 예산 제약.

**왜 예산 제약이 반드시 필요한가.** 설비 대수를 결정 변수로 두고 성능을 최적화하면
답은 항상 "설비를 최대로 산다"가 된다. 배치 부분은 아무 영향도 못 주고 최적화가
무의미해진다. 예산이 있어야 "같은 돈으로 더 잘 배치한다"는 원래 질문이 성립한다.

⚠️ **단가는 자리표시자다.** 실제 장비 가격은 비공개이고 세대·공급사별로 크게 다르다.
여기 값은 "스캐너가 가장 비싸고 계측이 가장 싸다"는 상대적 순서만 반영한다. 실제 값을
아는 경우 `CostModel`에 넣어 교체해야 하며, 최적화 결과의 절대적 타당성은 이 표에
달려 있다 (`docs/SPEC.md` §11-A).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.model import Fab

VEHICLE = "VEHICLE"
"""반송차의 비용 항목 키. 설비 그룹 ID와 충돌하지 않는 이름을 쓴다."""

DEFAULT_UNIT_COST: dict[str, float] = {
    "PHOTO": 10.0,      # 스캐너 — 압도적으로 비싸다
    "IMPL_HC": 6.0,
    "IMPL_MC": 5.0,
    "DIFF_FURN": 4.0,
    "ETCH_DRY": 3.5,
    "CVD": 3.0,
    "PVD": 3.0,
    "CMP": 2.5,
    "ETCH_WET": 1.5,
    "METRO": 1.2,
    "CLEAN": 1.0,
    VEHICLE: 0.5,
}


@dataclass(frozen=True)
class CostModel:
    """상대 단가표. 단위는 임의이며 비율만 의미가 있다."""

    unit_cost: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_UNIT_COST))
    source: str = "자리표시자 — 상대적 순서만 반영 (docs/SPEC.md §11-A)"

    def tool_capex(self, counts: dict[str, int]) -> float:
        return sum(self.unit_cost.get(gid, 0.0) * n for gid, n in counts.items())

    def vehicle_capex(self, vehicles: int) -> float:
        return self.unit_cost.get(VEHICLE, 0.0) * vehicles

    def capex(self, counts: dict[str, int], vehicles: int) -> float:
        return self.tool_capex(counts) + self.vehicle_capex(vehicles)

    def baseline_capex(self, fab: Fab) -> float:
        """기준 구성의 총액. 기본 예산으로 쓰면 "추가 투자 없이" 실험이 된다."""
        return self.capex(fab.tool_counts, fab.transport.vehicles)

    def breakdown(self, counts: dict[str, int], vehicles: int) -> list[tuple[str, int, float]]:
        """(항목, 수량, 금액) 내림차순. 어디에 돈이 묶여 있는지 보여준다."""
        rows = [(gid, n, self.unit_cost.get(gid, 0.0) * n) for gid, n in counts.items() if n]
        rows.append((VEHICLE, vehicles, self.vehicle_capex(vehicles)))
        return sorted(rows, key=lambda r: -r[2])

    def marginal(self, gid: str) -> float:
        """그 그룹 설비를 1대 더 살 때의 비용."""
        return self.unit_cost.get(gid, 0.0)


@dataclass(frozen=True)
class Budget:
    """capex 상한과 판정."""

    limit: float
    model: CostModel = field(default_factory=CostModel)

    @classmethod
    def same_as_baseline(cls, fab: Fab, model: CostModel | None = None) -> Budget:
        """기준 구성과 같은 예산 — "돈을 더 쓰지 않고 얼마나 개선되는가"."""
        m = model or CostModel()
        return cls(limit=m.baseline_capex(fab), model=m)

    def spend(self, counts: dict[str, int], vehicles: int) -> float:
        return self.model.capex(counts, vehicles)

    def fits(self, counts: dict[str, int], vehicles: int) -> bool:
        return self.spend(counts, vehicles) <= self.limit + 1e-9

    def headroom(self, counts: dict[str, int], vehicles: int) -> float:
        """남은 예산. 음수면 초과."""
        return self.limit - self.spend(counts, vehicles)
