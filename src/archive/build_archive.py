# -*- coding: utf-8 -*-
"""问答助手会话归档：把平台导出的问答会话整理成 JSON / CSV 存到 src/archive/。

数据来源：
  discovery-report.json（仓库根）           —— 24 轮访谈问答（6 个岗位）
  docs/典型事件访谈.txt 里的第 7 轮         —— 平台补充轮次（2026-11-06）

输出：
  src/archive/qa_sessions.json   结构化：会话 + 轮次
  src/archive/qa_sessions.csv    扁平：一行一轮，Excel 可直接打开

用法：python src/archive/build_archive.py
"""

import csv
import json
import os
import re
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)                 # src/
ROOT = os.path.dirname(SRC)                 # 仓库根
REPORT = os.path.join(ROOT, "discovery-report.json")
SPEC = os.path.join(ROOT, "支付对账与差错追踪系统_交付", "webapp", "docs", "需求发现报告.md")
OUT_JSON = os.path.join(HERE, "qa_sessions.json")
OUT_CSV = os.path.join(HERE, "qa_sessions.csv")

# 平台补充轮次（docs/典型事件访谈.txt 第 7 轮），原样保留
SUPPLEMENT = [{
    "no": 7,
    "at": "2026-11-06T01:18:38.403Z",
    "question": "审计需要什么凭证，怎么追溯修改？",
    "answer": (
        "审计凭证这块，我要求的是能重建整条处理链：流水归集 → 渠道账单匹配 → 差异定位 → "
        "工单确认 → 人工调整 → 复核，每一步都要留痕[finance-reconciliation-f45]。"
        "具体到每次建议，要保留业务编号、资料版本、事实引用、建议结果、人工改动和签核人"
        "[finance-reconciliation-f45]。注意不记录账号口令或不必要的个人信息，这是红线"
        "[finance-reconciliation-f45]。\n\n"
        "追溯修改的逻辑是：任何人工改动都要能对上原始建议和签核人，否则我无法判断是纠错还是"
        "掩盖差异。资金调整环节仍要双人复核，不能用猜测匹配掩盖差异[finance-reconciliation-f40]。\n\n"
        "我关心的是：差异定位到工单确认之间，如果同一笔被多次改动，版本怎么串？这个目前尚未核实。"
        "建议把“改动前后值+改动原因+签核人”作为必填项，但这是待验证假设，需要和唐映秋那边"
        "确认资料责任归属后再定。"
    ),
    "speaker": "傅景",
    "role": "资金风险经理",
    "source": "平台补充轮次（2026-11-06）",
}]


def load_people() -> dict:
    """从需求发现报告里取 fXX → 姓名 的映射。"""
    text = open(SPEC, encoding="utf-8").read()
    sec = text.split("## 按角色整理的调查证据", 1)[1]
    mapping = {}
    for fid, name in re.findall(r"- \[(finance-reconciliation-f\d+)\] (.+?)：", sec):
        mapping[fid] = name
    return mapping


def build() -> dict:
    report = json.load(open(REPORT, encoding="utf-8"))
    fid2name = load_people()
    transcript = report["transcript"]

    turns_by_char = {}
    order = []
    for i, item in enumerate(transcript):
        if item["speaker"] != "character":
            continue
        q = ""
        for k in range(i - 1, -1, -1):
            if transcript[k]["speaker"] == "user":
                q = transcript[k]["text"].strip()
                break
        cid = item["characterId"]
        if cid not in turns_by_char:
            turns_by_char[cid] = []
            order.append(cid)
        turns_by_char[cid].append({
            "question": q,
            "answer": item["text"].strip(),
            "at": item["at"],
        })

    people = {}
    for cid in order:
        names = set()
        for t in turns_by_char[cid]:
            for fid in re.findall(r"finance-reconciliation-f\d+", t["answer"]):
                if fid in fid2name:
                    names.add(fid2name[fid])
        people[cid] = names

    sessions = []
    for cid in order:
        name = sorted(people[cid])[0] if people[cid] else cid
        turns = []
        for idx, t in enumerate(turns_by_char[cid], 1):
            cites = sorted({f.split("-")[-1]
                            for f in re.findall(r"finance-reconciliation-(f\d+)", t["answer"])})
            turns.append({
                "no": idx,
                "at": t["at"],
                "question": t["question"],
                "answer": t["answer"],
                "citations": cites,
                "source": "平台访谈导出（discovery-report.json）",
            })
        sessions.append({
            "session_id": f"s_{cid.split('-')[-1]}",
            "project": "finance-reconciliation",
            "stage": "opportunity",
            "respondent": name,
            "turns": turns,
        })

    # 补充轮次挂到傅景名下
    for s in sessions:
        if s["respondent"] == "傅景":
            for sup in SUPPLEMENT:
                s["turns"].append({
                    "no": sup["no"], "at": sup["at"], "question": sup["question"],
                    "answer": sup["answer"],
                    "citations": sorted({f.split("-")[-1]
                                         for f in re.findall(r"finance-reconciliation-(f\d+)",
                                                             sup["answer"])}),
                    "source": sup["source"],
                })

    return {
        "title": "问答助手会话归档",
        "project": "finance-reconciliation",
        "stage": "opportunity",
        "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": {
            "primary": "discovery-report.json（平台导出，24 轮问答）",
            "supplement": "平台补充轮次 2026-11-06（1 轮，见 docs/典型事件访谈.txt 第 7 轮）",
        },
        "stats": {
            "sessions": len(sessions),
            "turns": sum(len(s["turns"]) for s in sessions),
        },
        "sessions": sessions,
    }


def write_csv(data: dict) -> None:
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["会话ID", "项目", "阶段", "受访人", "轮次", "时间", "问题", "回答",
                    "引用事实", "来源"])
        for s in data["sessions"]:
            for t in s["turns"]:
                w.writerow([s["session_id"], s["project"], s["stage"], s["respondent"],
                            t["no"], t["at"], t["question"], t["answer"],
                            "、".join(t["citations"]), t["source"]])


def main() -> None:
    data = build()
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    write_csv(data)
    print(f"会话 {data['stats']['sessions']} 个，轮次 {data['stats']['turns']} 轮")
    print("written:", OUT_JSON)
    print("written:", OUT_CSV)


if __name__ == "__main__":
    main()
