# -*- coding: utf-8 -*-
"""测算模型的测试：公式、边界、以及报告里引用的每个数字。

金标准（手算）：
  中档：节省 1.5 人月/月，满额 4.25 万/月，月运行 0.60 万，
        首年收益 36.975 万，首年成本 33.2 万，净 3.775 万，第 11 个月回本。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from roi_model import (Assumptions, adoption_curve, default_tiers,
                       monthly_benefit_full, monthly_run_cost,
                       person_months_saved, sensitivity, simulate)


@pytest.fixture(scope="module")
def tiers():
    return default_tiers()


@pytest.fixture(scope="module")
def mid(tiers):
    return tiers["中（一期）"]


# ---------------------------------------------------------------- 基础公式
def test_run_cost_is_sum_of_components(mid):
    total, detail = monthly_run_cost(mid)
    assert round(sum(detail.values()), 6) == total
    assert total == pytest.approx(0.60, abs=1e-9)
    assert detail["运维人力"] == pytest.approx(0.30)
    assert detail["基础设施"] == pytest.approx(0.25)


def test_person_months_saved(mid, tiers):
    # 4 人 × 3 小时 / 8 = 1.5 人月
    assert person_months_saved(mid) == pytest.approx(1.5)
    # 低档只省 2 小时：4 × 2 / 8 = 1.0
    assert person_months_saved(tiers["低（MVP）"]) == pytest.approx(1.0)


def test_full_benefit_combines_labor_and_recovery(mid):
    # 1.5 人月 × 2.5 万 + 0.5 万追偿 = 4.25 万
    assert monthly_benefit_full(mid) == pytest.approx(4.25)


def test_zero_saving_when_hours_unchanged(mid):
    same = Assumptions(**{**mid.__dict__, "hours_after": mid.hours_before})
    assert person_months_saved(same) == pytest.approx(0.0)
    # 没有工时节省，只剩追偿
    assert monthly_benefit_full(same) == pytest.approx(mid.recover_per_month)


# ---------------------------------------------------------------- 采用率曲线
def test_adoption_curve_length_and_values(mid):
    curve = adoption_curve(mid, 12)
    assert len(curve) == 12
    assert curve == [0.3, 0.5, 0.7] + [0.8] * 9


def test_adoption_curve_extends_with_last_value(mid):
    short = Assumptions(**{**mid.__dict__, "adoption": (0.2, 0.4)})
    curve = adoption_curve(short, 4)
    assert curve == [0.2, 0.4, 0.4, 0.4]


# ---------------------------------------------------------------- 现金流
def test_cash_flow_shape(mid):
    r = simulate(mid)
    assert len(r.cash_flow) == 13            # 第 0 月 + 12 个月
    assert r.cash_flow[0]["month"] == 0
    assert r.cash_flow[0]["cost"] == pytest.approx(26.0)
    assert r.cash_flow[0]["cumulative"] == pytest.approx(-26.0)


def test_first_year_totals_match_hand_calculation(mid):
    r = simulate(mid)
    assert r.first_year_benefit == pytest.approx(36.975, abs=1e-6)
    assert r.first_year_cost == pytest.approx(33.2, abs=1e-6)
    assert r.first_year_net == pytest.approx(3.775, abs=1e-6)


def test_payback_month_is_eleven(mid):
    r = simulate(mid)
    assert r.payback_month() == 11
    # 第 10 个月仍为负，第 11 个月转正
    assert r.cash_flow[10]["cumulative"] < 0
    assert r.cash_flow[11]["cumulative"] >= 0


def test_roi_formula(mid):
    r = simulate(mid)
    expected = (36.975 - 33.2) / 33.2
    assert r.roi(12) == pytest.approx(expected, abs=1e-6)


def test_steady_state_net(mid):
    r = simulate(mid)
    # 4.25 × 0.8 − 0.6 = 2.8 万/月
    assert r.steady_state_monthly_net() == pytest.approx(2.8, abs=1e-9)


# ---------------------------------------------------------------- 三档对比
def test_low_tier_numbers(tiers):
    r = simulate(tiers["低（MVP）"])
    assert r.first_year_net == pytest.approx(5.55, abs=1e-6)
    assert r.payback_month() == 9


def test_high_tier_not_paid_back_in_year_one(tiers):
    r = simulate(tiers["高（三期）"])
    assert r.first_year_net == pytest.approx(-71.875, abs=1e-6)
    assert r.payback_month() == -1


def test_tiers_are_ordered_by_one_off(tiers):
    low, mid, high = (tiers[k].one_off for k in ("低（MVP）", "中（一期）", "高（三期）"))
    assert low < mid < high


# ---------------------------------------------------------------- 敏感性
def test_sensitivity_is_monotonic_in_adoption(mid):
    rows = sensitivity(mid, (0.6, 0.8, 0.9), (2.0,))
    nets = [r["first_year_net"] for r in rows]
    assert nets == sorted(nets)


def test_sensitivity_worse_when_less_time_saved(mid):
    rows = sensitivity(mid, (0.8,), (2.0, 3.0))
    assert rows[0]["first_year_net"] > rows[1]["first_year_net"]


# ---------------------------------------------------------------- 输入校验
@pytest.mark.parametrize("field,value", [
    ("team_size", 0),
    ("one_off", -1.0),
    ("adoption", (1.2,)),
    ("hours_after", 6.0),       # 高于改造前
])
def test_invalid_assumptions_raise(mid, field, value):
    bad = Assumptions(**{**mid.__dict__, field: value})
    with pytest.raises(ValueError):
        simulate(bad)


def test_simulation_is_deterministic(mid):
    a = simulate(mid)
    b = simulate(mid)
    assert [m["cumulative"] for m in a.cash_flow] == [m["cumulative"] for m in b.cash_flow]
