# -*- coding: utf-8 -*-
"""业务测算模型：报价、月运行成本、收益、ROI 与回本期。

单位统一用「万元」和「月」。所有公式都是纯函数，便于测试与复算。

关键口径
--------
- 人月成本 = 节省工时 / 8（因为 1 人月 ≈ 21 工作日 × 8 小时，而月内工作日数在分子分母
  同时出现被约掉，所以直接从「每天省几小时」换算到「每月省几个人月」）。
- 月度收益 = 节省人月 × 人月成本 × 采用率 + 追偿收益 × 采用率。
- 回本期 = 累计净现金流首次由负转正的月份（一次性投入记在第 0 个月）。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

HOURS_PER_DAY = 8.0


@dataclass(frozen=True)
class Assumptions:
    """一档方案的输入假设。金额单位：万元。"""

    name: str
    team_size: int                 # 对账团队人数
    cost_per_person_month: float   # 人月综合成本
    hours_before: float            # 现状每人每天查异常小时数
    hours_after: float             # 改造后每人每天小时数
    one_off: float                 # 一次性报价（实施 + 环境）
    adoption: Tuple[float, ...]    # 上线后 12 个月的采用率
    recover_per_month: float = 0.0  # 满负荷时的追偿收益（万元/月）
    ops_people_month: float = 0.1   # 运维人力投入（人月/月）
    ops_cost_per_month: float = 3.0  # 运维人月成本
    infra_per_month: float = 0.25   # 服务器 / 存储 / 备份摊销
    llm_per_month: float = 0.05     # 模型与接口调用

    def validate(self) -> None:
        if self.team_size <= 0:
            raise ValueError("团队人数必须为正")
        if self.hours_after > self.hours_before:
            raise ValueError("改造后工时不应高于改造前")
        if not self.adoption:
            raise ValueError("采用率曲线不能为空")
        if any(not 0 <= a <= 1 for a in self.adoption):
            raise ValueError("采用率必须在 [0,1] 内")
        if self.one_off < 0:
            raise ValueError("报价不能为负")


@dataclass
class Result:
    monthly_run_cost: float
    run_cost_detail: Dict[str, float]
    monthly_benefit_full: float
    person_months_saved: float
    cash_flow: List[Dict[str, float]] = field(default_factory=list)

    @property
    def first_year_benefit(self) -> float:
        return round(sum(m["benefit"] for m in self.cash_flow), 6)

    @property
    def first_year_cost(self) -> float:
        return round(sum(m["cost"] for m in self.cash_flow), 6)

    @property
    def first_year_net(self) -> float:
        return round(self.first_year_benefit - self.first_year_cost, 6)

    def payback_month(self) -> int:
        """累计现金流转正的月份；未回本返回 -1。"""
        for m in self.cash_flow:
            if m["cumulative"] >= 0:
                return int(m["month"])
        return -1

    def steady_state_monthly_net(self) -> float:
        adopt = self.cash_flow[-1]["adoption"]
        return round(self.monthly_benefit_full * adopt - self.monthly_run_cost, 6)

    def roi(self, months: int = 12) -> float:
        """投入产出比 = 净收益 / 总投入（一次性 + 前 months 个月的运行成本）。"""
        rows = self.cash_flow[1:months + 1]
        benefit = sum(m["benefit"] for m in rows)
        cost = self.cash_flow[0]["cost"] + sum(m["cost"] for m in rows)
        if cost == 0:
            return 0.0
        return round((benefit - cost) / cost, 6)


def monthly_run_cost(a: Assumptions) -> Tuple[float, Dict[str, float]]:
    """月度运行成本：运维人力 + 基础设施 + 模型调用。"""
    detail = {
        "运维人力": round(a.ops_people_month * a.ops_cost_per_month, 6),
        "基础设施": round(a.infra_per_month, 6),
        "模型与接口": round(a.llm_per_month, 6),
    }
    return round(sum(detail.values()), 6), detail


def person_months_saved(a: Assumptions) -> float:
    """满负荷下每月节省的人月数。"""
    hours_saved_per_day = a.team_size * (a.hours_before - a.hours_after)
    return round(hours_saved_per_day / HOURS_PER_DAY, 6)


def monthly_benefit_full(a: Assumptions) -> float:
    """采用率 100% 时的月度收益（工时 + 追偿）。"""
    labor = person_months_saved(a) * a.cost_per_person_month
    return round(labor + a.recover_per_month, 6)


def adoption_curve(a: Assumptions, months: int = 12) -> List[float]:
    """把 12 个月采用率曲线补齐/截断到指定月数（不足则沿用最后一个值）。"""
    curve = list(a.adoption)
    if len(curve) < months:
        curve += [curve[-1]] * (months - len(curve))
    return [round(x, 6) for x in curve[:months]]


def simulate(a: Assumptions, months: int = 12) -> Result:
    """按月模拟现金流。一次性投入记在第 0 个月。"""
    a.validate()
    run_cost, detail = monthly_run_cost(a)
    full = monthly_benefit_full(a)

    flow: List[Dict[str, float]] = [{
        "month": 0,
        "adoption": 0.0,
        "benefit": 0.0,
        "cost": round(a.one_off, 6),
        "net": round(-a.one_off, 6),
        "cumulative": round(-a.one_off, 6),
    }]
    cum = -a.one_off
    for i, adopt in enumerate(adoption_curve(a, months), 1):
        benefit = round(full * adopt, 6)
        cost = round(run_cost, 6)
        net = round(benefit - cost, 6)
        cum = round(cum + net, 6)
        flow.append({
            "month": i,
            "adoption": round(adopt, 6),
            "benefit": benefit,
            "cost": cost,
            "net": net,
            "cumulative": cum,
        })

    return Result(
        monthly_run_cost=run_cost,
        run_cost_detail=detail,
        monthly_benefit_full=full,
        person_months_saved=person_months_saved(a),
        cash_flow=flow,
    )


# ---------------------------------------------------------------- 三档方案
def default_tiers() -> Dict[str, Assumptions]:
    """低 / 中 / 高 三档的标准假设，供报告与测试共用。"""
    ramp = (0.30, 0.50, 0.70, 0.80, 0.80, 0.80, 0.80, 0.80, 0.80, 0.80, 0.80, 0.80)
    return {
        "低（MVP）": Assumptions(
            name="低（MVP）", team_size=4, cost_per_person_month=2.5,
            hours_before=5.0, hours_after=3.0, one_off=12.0, adoption=ramp,
            recover_per_month=0.0, ops_people_month=0.05, infra_per_month=0.15,
        ),
        "中（一期）": Assumptions(
            name="中（一期）", team_size=4, cost_per_person_month=2.5,
            hours_before=5.0, hours_after=2.0, one_off=26.0, adoption=ramp,
            recover_per_month=0.5, ops_people_month=0.1, infra_per_month=0.25,
        ),
        "高（三期）": Assumptions(
            name="高（三期）", team_size=8, cost_per_person_month=2.5,
            hours_before=5.0, hours_after=2.0, one_off=130.0, adoption=ramp,
            recover_per_month=1.25, ops_people_month=0.25, infra_per_month=0.6,
            llm_per_month=0.15,
        ),
    }


def sensitivity(base: Assumptions, adoption_levels: Sequence[float],
                hours_after_levels: Sequence[float]) -> List[Dict[str, float]]:
    """对「稳态采用率 × 改造后工时」做二维敏感性。"""
    rows: List[Dict[str, float]] = []
    for adopt in adoption_levels:
        for hours_after in hours_after_levels:
            a = Assumptions(**{**base.__dict__,
                               "adoption": tuple([adopt] * 12),
                               "hours_after": hours_after})
            r = simulate(a)
            rows.append({
                "adoption": adopt,
                "hours_after": hours_after,
                "person_months": r.person_months_saved,
                "first_year_net": r.first_year_net,
                "payback": r.payback_month(),
                "roi": r.roi(),
            })
    return rows
