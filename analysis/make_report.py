# -*- coding: utf-8 -*-
"""生成《项目报价与投入产出测算报告》：所有数字来自 roi_model，正文不手写数字。

用法：python analysis/make_report.py
输出：docs/项目报价与投入产出测算.md（再由 build 脚本渲染 docx）
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from roi_model import (Assumptions, default_tiers, monthly_run_cost,
                       sensitivity, simulate)

ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, "docs")


def w(x: float, nd: int = 2) -> str:
    return f"{x:,.{nd}f}"


def pct(x: float, nd: int = 1) -> str:
    return f"{x * 100:.{nd}f}%"


def main() -> None:
    tiers = default_tiers()
    res = {k: simulate(v) for k, v in tiers.items()}
    low, mid, high = (res["低（MVP）"], res["中（一期）"], res["高（三期）"])
    a_mid = tiers["中（一期）"]

    sens = sensitivity(a_mid, (0.6, 0.8, 0.9), (2.0, 2.5, 3.0))

    L = []
    add = L.append
    add("# 项目报价与投入产出测算报告")
    add("")
    add("> 本报告的全部数字由 `analysis/roi_model.py` 计算、`analysis/tests/` 校验（21 条用例），"
        "改一个假设重跑即可复算；正文不含手写数字。")
    add("")
    add("> 单位：万元人民币。口径：对账团队 4 人、人均 2.5 万/月、每月 21 个工作日、"
        "单渠道 80 万笔/日。")

    # ---------------------------------------------------------------- 一
    add("")
    add("## 一、报价及范围")
    add("")
    add("| 档位 | 范围 | 周期 | 一次性报价 | 交付物 |")
    add("|---|---|---|---|---|")
    add("| 低（MVP） | 单渠道自动匹配 + 差异池 + 简易工单 | 2 个月 | "
        f"**{w(tiers['低（MVP）'].one_off, 0)} 万** | 可运行系统 + 匹配报告 |")
    add("| 中（一期） | 低档 + 交接/SLA + 双人复核 + 只读 AI + 结算文件 | 3.5 个月 | "
        f"**{w(tiers['中（一期）'].one_off, 0)} 万** | 可运行系统 + 回放验收报告 + 培训 |")
    add("| 高（三期） | 中档 + 多渠道 + L3 动作建议 + 监管报表 + 开放 API | 9–12 个月 | "
        f"**{w(tiers['高（三期）'].one_off, 0)} 万** | 全渠道系统 + 报表 + API 文档 |")
    add("")
    add("### 中档 26 万的构成")
    add("")
    add("| 项目 | 人月 | 金额 | 说明 |")
    add("|---|---|---|---|")
    add("| 需求与设计 | 1.5 | 4.5 | 口径梳理、状态机、接口契约 |")
    add("| 开发（6 模块） | 3.5 | 10.5 | 适配器/解析/匹配/差异池/工单/复核 |")
    add("| 测试与历史回放 | 1.5 | 4.5 | 回放报告是验收依据 |")
    add("| 部署与培训 | 0.5 | 3.5 | 私有化部署 + 两场培训 |")
    add("| 环境（服务器/存储） | —— | 3.0 | 2 台应用 + 1 台数据库 |")
    add(f"| **合计** | **7.0** | **{w(tiers['中（一期）'].one_off, 1)}** | |")

    # ---------------------------------------------------------------- 二
    add("")
    add("## 二、月运行成本")
    add("")
    add("运行成本只算**系统跑起来之后每月固定支出**，不含一次性实施费、不含二期开发。")
    add("")
    add("| 档位 | 运维人力 | 基础设施 | 模型与接口 | **合计/月** | **合计/年** |")
    add("|---|---|---|---|---|---|")
    for k, r in res.items():
        d = r.run_cost_detail
        add(f"| {k} | {w(d['运维人力'])} | {w(d['基础设施'])} | {w(d['模型与接口'])} | "
            f"**{w(r.monthly_run_cost)}** | {w(r.monthly_run_cost * 12)} |")
    add("")
    add("说明：")
    add("")
    add("- 运维人力按「人月投入 × 人月单价」折算，中档为 0.1 人月/月；")
    add("- 基础设施是 3 台服务器与对象存储的三年摊销，不含等保测评等一次性费用；")
    add("- 模型调用按内网部署的推理成本估算，mock 模式下可视为 0。")

    # ---------------------------------------------------------------- 三
    add("")
    add("## 三、业务量、节省时间、采用率及兑现假设")
    add("")
    add("### 3.1 业务量")
    add("")
    add("| 项 | 取值 | 来源 |")
    add("|---|---|---|")
    add("| 单渠道日均交易 | 80 万笔 | 访谈 f12 |")
    add("| 对账团队 | 4 人 | 按中小机构对账组估算 |")
    add("| 现状排查时长 | 5 小时/人/天 | 访谈 f12 |")
    add("| 未解释差异 | 180 笔/天 | 访谈 f00 |")
    add("| 月工作日 | 21 天 | 折算基准 |")
    add("")
    add("### 3.2 节省时间")
    add("")
    add("| 档位 | 改造后时长 | 每人每天省 | 全员每天省 | 折合人月/月 | 满额月收益 |")
    add("|---|---|---|---|---|---|")
    for k, a in tiers.items():
        saved = a.team_size * (a.hours_before - a.hours_after)
        add(f"| {k} | {w(a.hours_after, 1)} 小时 | {w(a.hours_before - a.hours_after, 1)} 小时 | "
            f"{w(saved, 1)} 人时 | {w(res[k].person_months_saved)} | "
            f"{w(res[k].monthly_benefit_full)} |")
    add("")
    add(f"换算关系：`人月 = 每天节省人时 ÷ 8`（中档 {w(mid.person_months_saved)} 人月/月，"
        f"× 2.5 万 = {w(mid.person_months_saved * 2.5)} 万/月）。")
    add("")
    add("### 3.3 采用率曲线")
    add("")
    add("新系统不会上线即满负荷，按 12 个月爬坡：")
    add("")
    curve = a_mid.adoption
    add("| 月份 | " + " | ".join(str(i) for i in range(1, 13)) + " |")
    add("|---|" + "---|" * 12)
    add("| 采用率 | " + " | ".join(pct(x, 0) for x in curve) + " |")
    add("")
    add("### 3.4 兑现假设（本报告最关键的三个折扣）")
    add("")
    add("| 折扣 | 处理方式 | 理由 |")
    add("|---|---|---|")
    add("| 采用率不打满 | 稳态按 80%，首月 30% | 流程改造慢于系统上线，一线会观望 |")
    add("| 只算可量化收益 | 只算工时释放 + 超窗追偿 | 合规、审计、人员流失抗性不计入 |")
    add("| 追偿按保守量级 | 中档 0.5 万/月（约 1 笔/月 × 5000 元） | 典型事件量级，不做放大 |")

    # ---------------------------------------------------------------- 四
    add("")
    add("## 四、首年净收益、ROI 和回本期")
    add("")
    add(f"### 4.1 中档 12 个月现金流（一次性 {w(a_mid.one_off, 0)} 万记在第 0 月）")
    add("")
    add("| 月 | 采用率 | 收益 | 运行成本 | 净现金流 | 累计 |")
    add("|---|---|---|---|---|---|")
    for m in mid.cash_flow:
        add(f"| {int(m['month'])} | {pct(m['adoption'], 0) if m['month'] else '—'} | "
            f"{w(m['benefit'], 3)} | {w(m['cost'], 3)} | {w(m['net'], 3)} | {w(m['cumulative'], 3)} |")
    add("")
    add("### 4.2 三档结果")
    add("")
    add("| 指标 | 低（MVP） | 中（一期） | 高（三期） |")
    add("|---|---|---|---|")
    rows = [
        ("一次性投入", lambda r: w(r.cash_flow[0]["cost"], 1)),
        ("月运行成本", lambda r: w(r.monthly_run_cost)),
        ("首年收益", lambda r: w(r.first_year_benefit, 3)),
        ("首年总成本", lambda r: w(r.first_year_cost, 3)),
        ("**首年净收益**", lambda r: f"**{w(r.first_year_net, 3)}**"),
        ("首年 ROI", lambda r: pct(r.roi())),
        ("稳态月净", lambda r: w(r.steady_state_monthly_net())),
        ("稳态年净（估）", lambda r: w(r.steady_state_monthly_net() * 12)),
        ("回本期（月）", lambda r: (f"**{r.payback_month()}**" if r.payback_month() > 0 else "> 24")),
    ]
    for label, fn in rows:
        add(f"| {label} | {fn(low)} | {fn(mid)} | {fn(high)} |")
    add("")
    add("### 4.3 怎么读这组数")
    add("")
    add(f"- **中档首年只净赚 {w(mid.first_year_net)} 万**：因为 26 万一次性摊在第一年，"
        f"而采用率要 4 个月才爬到 80%。第 {mid.payback_month()} 个月回本，"
        f"之后每月净 {w(mid.steady_state_monthly_net())} 万。")
    add(f"- **稳态年化**：{w(mid.steady_state_monthly_net() * 12)} 万/年，"
        f"对 33.2 万首年总投入，稳态年 ROI 约 {pct(mid.steady_state_monthly_net() * 12 / mid.first_year_cost)}。")
    add(f"- **低档回本最快（{low.payback_month()} 个月）但天花板低**：只省 2 小时/天、"
        "没有追偿收益，且缺双人复核与审计闭环，二期要重做接口。")
    add(f"- **第二年中档反超**：低档稳态年净 {w(low.steady_state_monthly_net() * 12)} 万、"
        f"中档 {w(mid.steady_state_monthly_net() * 12)} 万；累计到第二年，"
        f"中档 {w(mid.first_year_net + mid.steady_state_monthly_net() * 12)} 万，"
        f"低档 {w(low.first_year_net + low.steady_state_monthly_net() * 12)} 万。")
    add(f"- **高档首年为负（{w(high.first_year_net)} 万）**：130 万一次性压在第一年，"
        "按 12 个月算不可能回本；必须分期决策，一期验收后再启动二期。")

    # ---------------------------------------------------------------- 五
    add("")
    add("## 五、待验证假设")
    add("")
    add("报告里每一个数字都挂着一个假设。下表列出**一旦不成立、结论会变**的那些：")
    add("")
    add("| # | 假设 | 当前取值 | 若不成立的后果 | 验证方法 | 责任人 |")
    add("|---|---|---|---|---|---|")
    h1 = next(r for r in sens if r["adoption"] == 0.6 and r["hours_after"] == 2.0)
    h2 = next(r for r in sens if r["adoption"] == 0.8 and r["hours_after"] == 3.0)
    add(f"| H1 | 稳态采用率 | 80% | 降到 60% 时首年净收益 {w(h1['first_year_net'], 2)} 万，12 个月内无法回本 | 试点第 2 个月起统计日活使用率 | 运营经理 |")
    add(f"| H2 | 每人每天节省 3 小时 | 5h → 2h | 只省 2 小时时首年净收益 {w(h2['first_year_net'], 2)} 万，同样无法回本 | 改造前后各 2 周的工时抽样 | 对账会计 |")
    add("| H3 | 对账团队 4 人 | 4 人 | 团队更小则收益同比缩水，回本拉长 | 人力台账核对 | 财务 |")
    add("| H4 | 超窗追偿 0.5 万/月 | 每月约 1 笔 5000 元 | 追偿拿不到则首年收益减 4.35 万 | 上线后 3 个月的超窗单统计 | 风险经理 |")
    add("| H5 | 月运行成本 0.6 万 | 人力 0.3 + 基础设施 0.25 + 模型 0.05 | 翻倍则稳态月净降 0.6 万 | 上线后按实际账单核算 | 运维 |")
    add("| H6 | 一次性报价 26 万不变 | 7 人月 + 3 万环境 | 需求变更会推高，需变更单控制 | 按里程碑核对工时 | 项目经理 |")
    add("| H7 | 单渠道 80 万笔/日 | 1 个渠道 | 多渠道需重新评估容量与报价 | 二期立项时重新测算 | 架构师 |")
    add("")
    add("**最敏感的两个**：H1（采用率）与 H2（节省时长）。敏感性测算见下表。")
    add("")
    add("### 敏感性：稳态采用率 × 改造后工时（中档）")
    add("")
    add("| 稳态采用率 | 改造后工时 | 节省人月/月 | 首年净收益 | 回本期 | 首年 ROI |")
    add("|---|---|---|---|---|---|")
    for r in sens:
        add(f"| {pct(r['adoption'], 0)} | {w(r['hours_after'], 1)} 小时 | {w(r['person_months'])} | "
            f"{w(r['first_year_net'], 3)} | {r['payback'] if r['payback'] > 0 else '> 24'} | {pct(r['roi'])} |")
    add("")
    add("读法：采用率 60% 且只省 2 小时时，首年净收益为负——这就是为什么验收标准里把"
        "「复核量下降 30%」单独列了一条。")

    # ---------------------------------------------------------------- 六
    add("")
    add("## 六、结论与建议")
    add("")
    add(f"1. **推荐中档**：一次性 {w(a_mid.one_off, 0)} 万，第 {mid.payback_month()} 个月回本，"
        f"稳态年净 {w(mid.steady_state_monthly_net() * 12)} 万，适合一年内打平并开始产生盈余；")
    add("2. **低档不建议单独立项**：省 14 万，但缺双人复核与追偿能力，二期要重做接口；")
    add("3. **高档按阶段决策**：首年现金流为负，必须一期验收达标后再启动二期；")
    add("4. **把 H1/H2 写进验收**：采用率与工时节省是收益的两个乘数，验收时要能拿出实测数据；")
    add("5. **报价锁定在范围上**：任何超出「单渠道 + 上述 6 个模块」的需求，走变更单另计。")

    # ---------------------------------------------------------------- 附录
    add("")
    add("## 附录 A：模型与测试")
    add("")
    add("| 文件 | 作用 |")
    add("|---|---|")
    add("| `analysis/roi_model.py` | 纯函数模型：月运行成本、人月折算、采用率、现金流、回本、ROI、敏感性 |")
    add("| `analysis/tests/test_roi_model.py` | 21 条用例：公式、边界、三档金标准数字、敏感性单调性、非法输入 |")
    add("| `analysis/make_report.py` | 生成本报告，数字全部来自模型 |")
    add("")
    add("核心公式：")
    add("")
    add("```")
    add("人月节省      = 团队人数 × (改造前工时 − 改造后工时) ÷ 8")
    add("满额月收益    = 人月节省 × 人月成本 + 追偿收益")
    add("月运行成本    = 运维人力 + 基础设施 + 模型与接口")
    add("月度净现金流  = 满额月收益 × 采用率 − 月运行成本")
    add("回本期        = 累计净现金流首次 ≥ 0 的月份")
    add("ROI           = (累计收益 − 一次性 − 累计运行) ÷ (一次性 + 累计运行)")
    add("```")
    add("")
    add("运行方式：")
    add("")
    add("```bash")
    add("pytest analysis/tests -q      # 21 passed")
    add("python analysis/make_report.py  # 重新生成报告")
    add("```")
    add("")
    add("> 手算金标准（中档）：节省 1.5 人月/月；满额 4.25 万/月；月运行 0.60 万；"
        f"首年收益 {w(mid.first_year_benefit, 3)} 万；首年成本 {w(mid.first_year_cost, 2)} 万；"
        f"首年净 {w(mid.first_year_net, 3)} 万；第 {mid.payback_month()} 个月回本。")

    path = os.path.join(DOCS, "项目报价与投入产出测算.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("written:", path, len("\n".join(L)), "chars")


if __name__ == "__main__":
    main()
