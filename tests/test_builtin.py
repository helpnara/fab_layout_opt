"""내장 데이터셋 검증.

라우트는 레이어 템플릿을 손으로 적은 것이므로, 그룹별 방문 횟수가 의도한 표와
정확히 일치하는지 기계적으로 확인해야 한다. 이 테스트가 데이터셋의 유일한 보증이다.
"""

from __future__ import annotations

import pytest

from fablayout.core.model import Fab
from fablayout.data.builtin import (
    TARGET_STEP_COUNTS,
    TARGET_VISITS,
    build_smallfab21,
    load,
)


@pytest.fixture
def fab() -> Fab:
    return build_smallfab21()


def test_visit_counts_match_target(fab: Fab) -> None:
    for pid, target in TARGET_VISITS.items():
        assert fab.products[pid].visit_counts() == target, f"{pid} 방문 횟수 불일치"


def test_step_counts_match_target(fab: Fab) -> None:
    for pid, n in TARGET_STEP_COUNTS.items():
        assert fab.products[pid].step_count == n


def test_step_indices_are_contiguous(fab: Fab) -> None:
    for p in fab.products.values():
        assert [s.index for s in p.steps] == list(range(p.step_count))


def test_layers_are_monotonic(fab: Fab) -> None:
    """레이어 번호는 1부터 연속 증가해야 한다 (감소하면 라우트 생성 오류)."""
    for p in fab.products.values():
        layers = [s.layer for s in p.steps]
        assert layers == sorted(layers)
        assert set(layers) == set(range(1, p.layer_count + 1))


def test_reentrant_flow(fab: Fab) -> None:
    """재진입: 같은 노광 설비를 레이어마다 다시 방문한다."""
    logic = fab.products["LOGIC_A"]
    assert logic.layer_count == 10
    assert logic.visit_counts()["PHOTO"] == 10
    photo_layers = [s.layer for s in logic.steps if s.group == "PHOTO"]
    assert photo_layers == list(range(1, 11)), "레이어마다 정확히 1회 노광"

    mem = fab.products["MEM_B"]
    assert mem.layer_count == 7
    assert [s.layer for s in mem.steps if s.group == "PHOTO"] == list(range(1, 8))


def test_layer_starts_with_clean_ends_with_metro(fab: Fab) -> None:
    """공정 순서의 타당성: 각 레이어는 세정으로 시작하고 계측으로 끝난다."""
    for p in fab.products.values():
        for layer in range(1, p.layer_count + 1):
            steps = [s for s in p.steps if s.layer == layer]
            assert steps[0].group == "CLEAN", f"{p.pid} L{layer} 첫 스텝"
            assert steps[-1].group == "METRO", f"{p.pid} L{layer} 마지막 스텝"


def test_photo_precedes_etch_in_each_layer(fab: Fab) -> None:
    """노광 후 식각. 순서가 뒤집히면 공정으로 성립하지 않는다."""
    for p in fab.products.values():
        for layer in range(1, p.layer_count + 1):
            steps = [s for s in p.steps if s.layer == layer]
            photo_at = [i for i, s in enumerate(steps) if s.group == "PHOTO"]
            etch_at = [i for i, s in enumerate(steps) if s.group.startswith("ETCH")]
            if photo_at and etch_at:
                assert min(photo_at) < min(etch_at), f"{p.pid} L{layer}"


def test_tool_counts(fab: Fab) -> None:
    assert fab.total_tools == 21
    assert fab.tool_counts["PHOTO"] == 3
    assert fab.tool_counts["PVD"] == 1


def test_batch_and_single_tools_present(fab: Fab) -> None:
    batch = {g.gid for g in fab.groups.values() if g.mode == "batch"}
    assert batch == {"ETCH_WET", "DIFF_FURN", "CLEAN"}
    assert fab.groups["DIFF_FURN"].batch_size == 6
    assert fab.groups["PHOTO"].mode == "single"
    assert fab.groups["PHOTO"].batch_size == 1


def test_availability_range(fab: Fab) -> None:
    """모든 그룹의 가동률이 실제 fab 범위(70~99%)에 있어야 한다."""
    for g in fab.groups.values():
        assert 0.70 <= g.availability <= 0.99, f"{g.gid}: A={g.availability:.3f}"
    assert fab.groups["IMPL_HC"].availability == pytest.approx(0.88, abs=0.005)
    assert fab.groups["PHOTO"].availability == pytest.approx(0.90, abs=0.005)


def test_minutes_per_lot_divides_batch(fab: Fab) -> None:
    """능력 계산용 환산: 배치 설비는 배치 크기로 나눈다."""
    diff = fab.groups["DIFF_FURN"]
    assert diff.minutes_per_lot == pytest.approx(240.0 / 6)
    assert fab.groups["PHOTO"].minutes_per_lot == pytest.approx(60.0)


def test_raw_process_hours_does_not_divide_batch(fab: Fab) -> None:
    """반대로 사이클타임(X-factor 분모)은 배치 전체 시간을 겪는다."""
    logic_raw = fab.raw_process_hours("LOGIC_A")
    # DIFF_FURN 8회 × 240분 = 1920분이 나눠지지 않고 그대로 들어가야 한다
    assert logic_raw * 60 > 8 * 240
    assert logic_raw == pytest.approx(5020 / 60, rel=1e-9)


def test_mix_sums_to_one(fab: Fab) -> None:
    assert sum(p.mix for p in fab.products.values()) == pytest.approx(1.0)


def test_custom_mix() -> None:
    fab = build_smallfab21(mix_logic=0.5)
    assert fab.products["LOGIC_A"].mix == pytest.approx(0.5)
    assert fab.products["MEM_B"].mix == pytest.approx(0.5)


def test_counts_override() -> None:
    counts = dict(build_smallfab21().tool_counts)
    counts["PVD"] = 2
    fab = build_smallfab21(counts=counts)
    assert fab.tool_counts["PVD"] == 2
    assert fab.total_tools == 22


def test_source_flags_synthetic(fab: Fab) -> None:
    """데이터 출처가 합성임이 반드시 표시되어야 한다."""
    assert "원본 수치 아님" in fab.source


def test_weighted_visits(fab: Fab) -> None:
    w = fab.weighted_visits()
    assert w["PHOTO"] == pytest.approx(0.7 * 10 + 0.3 * 7)
    assert w["DIFF_FURN"] == pytest.approx(0.7 * 8 + 0.3 * 12)
    assert sum(w.values()) == pytest.approx(0.7 * 84 + 0.3 * 68)


def test_group_of() -> None:
    assert Fab.group_of("PHOTO#2") == "PHOTO"
    assert Fab.group_of("IMPL_HC#1") == "IMPL_HC"


def test_unknown_dataset() -> None:
    with pytest.raises(KeyError):
        load("smt2020:HVLM")
