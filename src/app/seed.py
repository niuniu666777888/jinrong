"""种子数据：制度条款、渠道、批次、差异。

全部为合成数据。差异的发生时间相对「当前时间」生成，
这样 7 天回溯窗口的判定在演示时始终有意义。
"""

from typing import Any, Dict, List

from . import db

POLICIES: List[Dict[str, str]] = [
    {
        "id": "p_1_1", "clause_no": "1.1", "title": "对账口径：结算日与支付日",
        "content": "渠道账单按结算日（T 日交易的资金实际入账日）出具，内部流水按支付日记录。"
                   "跨日退款在渠道侧落在退款当日，在内部侧仍关联原支付日，因此对账前必须先把"
                   "两侧的时间字段统一到同一口径，否则会稳定产生假性差异。",
    },
    {
        "id": "p_1_2", "clause_no": "1.2", "title": "主键与匹配规则",
        "content": "逐笔匹配以渠道流水号为主键，内部侧对应商户订单号；两者关系不完整时，"
                   "依次回退到渠道号 + 金额 + 时间窗匹配。任一路径匹配不上的记录进入差异池，"
                   "不得直接判为平账。",
    },
    {
        "id": "p_2_1", "clause_no": "2.1", "title": "长款：渠道有、内部无",
        "content": "长款指渠道存在交易记录、内部无有效订单（平台多收）。"
                   "处理方式为补单或原路退款，补单须核对商户订单号与用户身份，"
                   "退款须原路退回且保留渠道回执。长款不得直接转为收入。",
    },
    {
        "id": "p_2_2", "clause_no": "2.2", "title": "短款：内部有、渠道无",
        "content": "短款指内部显示支付成功、渠道无对应流水（平台少收）。"
                   "处理方式为向渠道调单核查，核查期内可先补偿扣款或冻结订单；"
                   "补偿扣款必须留痕，并在渠道确认后冲回。",
    },
    {
        "id": "p_2_3", "clause_no": "2.3", "title": "回溯窗口",
        "content": "差错处理须在差异发生后 7 天内完成。超过 7 天的差异不得按常规流程建单，"
                   "须转资金风险经理评估，并按渠道协议的争议时效判断是否还能追偿。",
    },
    {
        "id": "p_2_4", "clause_no": "2.4", "title": "双人复核",
        "content": "任何资金调整须由两名不同经办人完成：一人建单并提交，另一人复核签核。"
                   "建单人与复核人不得为同一账号，系统层应拒绝同一人既建单又复核。",
    },
    {
        "id": "p_2_5", "clause_no": "2.5", "title": "大额差异",
        "content": "单笔差异金额超过 3000 元的，建单时须标注风险等级为高，并抄送资金风险经理。"
                   "超过 10 万元或涉及客户备付金的，只能走资金调整审批，不得以账务订正规避。",
    },
    {
        "id": "p_3_1", "clause_no": "3.1", "title": "手续费合并入账",
        "content": "渠道按批次扣收手续费并合并入账的，单笔金额会与内部流水不一致。"
                   "此类差异应按批次汇总核对，不得逐笔建差错单；汇总后仍不平的余额再进入差异池。",
    },
    {
        "id": "p_3_2", "clause_no": "3.2", "title": "跨日退款",
        "content": "退款请求在当日发起、渠道次日入账的，内部侧按退款事件时间记账，渠道侧按入账日。"
                   "对账时须以退款单号关联，两侧都有记录即视为匹配，不因日期不同判为差异。",
    },
]

CHANNELS = [
    {"id": "ch_wx", "name": "微信支付", "code": "WX"},
    {"id": "ch_ali", "name": "支付宝", "code": "ALI"},
]

BATCHES = [
    {"id": "b_wx_1105", "channel_id": "ch_wx", "settle_date": "2026-11-05",
     "trade_count": 128_430, "amount": 8_642_190.55, "status": "已对账"},
    {"id": "b_ali_1105", "channel_id": "ch_ali", "settle_date": "2026-11-05",
     "trade_count": 96_218, "amount": 5_103_882.10, "status": "已对账"},
]

DAY = 86_400_000

# age_days 用于演示：9 天前的差异会被回溯窗口拦下
DIFFS = [
    {"id": "d_001", "batch_id": "b_wx_1105", "diff_type": "长款", "amount": 1280.00,
     "channel_trade_no": "WX202611050001", "internal_order_no": "IN202611050001",
     "age_days": 1, "reason": "渠道有交易，内部无对应订单"},
    {"id": "d_002", "batch_id": "b_wx_1105", "diff_type": "短款", "amount": 560.00,
     "channel_trade_no": "WX202611050002", "internal_order_no": "IN202611050002",
     "age_days": 1, "reason": "内部支付成功，渠道无流水"},
    {"id": "d_003", "batch_id": "b_wx_1105", "diff_type": "金额不符", "amount": 15.00,
     "channel_trade_no": "WX202611050088", "internal_order_no": "IN202611050088",
     "age_days": 2, "reason": "手续费合并入账导致金额差"},
    {"id": "d_004", "batch_id": "b_wx_1105", "diff_type": "长款", "amount": 5000.00,
     "channel_trade_no": "WX202610270007", "internal_order_no": "IN202610270007",
     "age_days": 9, "reason": "渠道有交易，内部无对应订单（超期）"},
    {"id": "d_005", "batch_id": "b_ali_1105", "diff_type": "短款", "amount": 88.00,
     "channel_trade_no": "ALI202611050031", "internal_order_no": "IN202611050031",
     "age_days": 3, "reason": "内部支付成功，渠道无流水"},
]


def is_empty() -> bool:
    return db.q1("SELECT COUNT(*) AS n FROM policy")["n"] == 0


def seed() -> None:
    """幂等：只在库为空时灌数据。"""
    if not is_empty():
        return

    now = db.now_ms()
    for p in POLICIES:
        db.execute(
            "INSERT INTO policy (id, doc, clause_no, title, content) VALUES (?,?,?,?,?)",
            (p["id"], "支付对账与差错处理规范（2026 版）", p["clause_no"], p["title"], p["content"]),
        )
    for c in CHANNELS:
        db.execute("INSERT INTO channel (id, name, code) VALUES (?,?,?)",
                   (c["id"], c["name"], c["code"]))
    for b in BATCHES:
        db.execute("INSERT INTO batch (id, channel_id, settle_date, trade_count, amount, status) "
                   "VALUES (?,?,?,?,?,?)",
                   (b["id"], b["channel_id"], b["settle_date"], b["trade_count"],
                    b["amount"], b["status"]))
    for d in DIFFS:
        db.execute(
            "INSERT INTO diff_record (id, batch_id, diff_type, channel_trade_no, "
            "internal_order_no, amount, occurred_at, reason, status) VALUES (?,?,?,?,?,?,?,?, 'pending')",
            (d["id"], d["batch_id"], d["diff_type"], d["channel_trade_no"],
             d["internal_order_no"], d["amount"], now - d["age_days"] * DAY, d["reason"]),
        )


def counts() -> Dict[str, int]:
    names = {"policy": "policy", "channel": "channel", "batch": "batch", "diff": "diff_record"}
    return {k: db.q1(f"SELECT COUNT(*) AS n FROM {v}")["n"] for k, v in names.items()}
