# -*- coding: utf-8 -*-
"""生成五份可视化场景测试文档（正常 / 边界 / 对抗 / 权限 / 故障）。

每份包含：元信息、用例结果表、逐场景的「步骤—期望—实际—截图」、结论。
截图来自 docs/test-images/。
"""

import os

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
DOCS = os.path.join(REPO, "docs")
IMGS = os.path.join(DOCS, "test-images")

ENV = "127.0.0.1:8097 ｜ 模型 mock ｜ SQLite ｜ Python 3.14 + FastAPI"
WHEN = "2026-11-06"


def new_doc(title, subtitle):
    doc = Document()
    for name in ("Normal", "Heading 1", "Heading 2", "Title"):
        try:
            st = doc.styles[name]
            st.font.name = "微软雅黑"
            st.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
        except KeyError:
            pass
    doc.styles["Normal"].font.size = Pt(10.5)

    h = doc.add_heading(title, level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p = doc.add_paragraph()
    r = p.add_run(subtitle)
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0x77, 0x77, 0x77)
    return doc


def table(doc, header, rows, widths=None):
    t = doc.add_table(rows=0, cols=len(header))
    t.style = "Table Grid"
    for ri, row in enumerate([header] + rows):
        cells = t.add_row().cells
        for ci, val in enumerate(row):
            cells[ci].text = str(val)
            for para in cells[ci].paragraphs:
                for run in para.runs:
                    run.font.size = Pt(9)
                    run.bold = (ri == 0)
    return t


def shot(doc, path, caption):
    if not os.path.exists(path):
        doc.add_paragraph(f"（截图缺失：{os.path.basename(path)}）")
        return
    doc.add_picture(path, width=Cm(15.5))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap = doc.add_paragraph(caption)
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.runs[0].font.size = Pt(8.5)
    cap.runs[0].font.color.rgb = RGBColor(0x77, 0x77, 0x77)


def meta(doc, cases, passed, extra=""):
    table(doc, ["项", "内容"], [
        ["被测系统", "支付对账与差错追踪 MVP（src/）"],
        ["测试环境", ENV],
        ["执行时间", WHEN],
        ["执行人", "测试（自动化 + 手工脚本）"],
        ["用例数", f"{cases}（通过 {passed}）"],
        ["结论", "通过" if passed == cases else "存在未通过项，见下表"],
    ] + ([[ "备注", extra ]] if extra else []))


# ---------------------------------------------------------------- 正常
def build_normal():
    doc = new_doc("正常场景测试", "支付对账与差错追踪 MVP · 端到端主流程")
    meta(doc, 8, 8, "8 条正常用例全部自动化；下面给出其中 4 条的界面留证。")

    doc.add_heading("一、用例结果", level=1)
    table(doc, ["编号", "用例", "预期", "结果", "证据"], [
        ["N01", "制度问答带条款引用", "返回 sources 与条款号", "通过", "图 1"],
        ["N02", "无命中走兜底", "degraded=true，不调模型", "通过", "test_chat_falls_back"],
        ["N03", "批次与差异列表", "2 批次 / 微信 4 条差异", "通过", "图 2"],
        ["N04", "核验通过签发凭证", "passed=true，vt_ 前缀", "通过", "test_verify_passes"],
        ["N05", "建单金额服务端重查", "落库 1280.00", "通过", "test_create_ticket_uses_server_side_amount"],
        ["N06", "完整复核链路", "reviewed，复核人≠建单人", "通过", "图 4"],
        ["N07", "Agent 三确认链路", "三次确认后 done", "通过", "图 3、图 4"],
        ["N08", "测算结果可复算", "首年净 3.775 万", "通过", "test_first_year_totals"],
    ])

    doc.add_heading("二、过程与截图", level=1)

    doc.add_heading("步骤 1：制度问答", level=2)
    doc.add_paragraph("操作：在问答页输入「长款和短款有什么区别？」并发送。")
    doc.add_paragraph("期望：流式返回回答，末尾给出条款引用标签（第 2.1 条、第 2.2 条）。")
    shot(doc, os.path.join(IMGS, "N1_qa.png"), "图 1　问答返回条款摘录与引用标签")

    doc.add_heading("步骤 2：查看批次与差异", level=2)
    doc.add_paragraph("操作：切到「差错处理」，选择微信支付 2026-11-05 批次，点第一条差异。")
    doc.add_paragraph("期望：右侧显示差异详情（编号 d_001、长款、1280.00 元、渠道流水）。")
    shot(doc, os.path.join(IMGS, "N2_diffs.png"), "图 2　批次列表与差异详情")

    doc.add_heading("步骤 3：Agent 执行到第一个确认门", level=2)
    doc.add_paragraph("操作：切到「Agent 代办」，点「让 Agent 跑一遍」。")
    doc.add_paragraph("期望：三个只读工具自动完成（查批次/列差异/核验），在建单前停下并展示预览。")
    shot(doc, os.path.join(IMGS, "N3_agent_pending.png"), "图 3　Agent 停在建单确认门，展示金额与处理方式")

    doc.add_heading("步骤 4：连续确认直至完成", level=2)
    doc.add_paragraph("操作：依次确认建单、提交复核；把操作人切换为「傅景」后确认复核。")
    doc.add_paragraph("期望：六步全部完成，状态 done，工单进入 reviewed。")
    shot(doc, os.path.join(IMGS, "N4_agent_done.png"), "图 4　三次确认后全部步骤完成")

    doc.add_heading("三、结论", level=1)
    doc.add_paragraph("正常路径全部通过：问答带引用、批次与差异可查、Agent 在写操作前停下等确认、"
                      "换人复核后工单闭环。无阻塞缺陷。")
    doc.save(os.path.join(DOCS, "正常场景测试.docx"))


# ---------------------------------------------------------------- 边界 / 对抗 / 权限 / 故障
def build_simple(filename, title, subtitle, rows, shots, conclusion, note=""):
    cases = len(rows)
    passed = sum(1 for r in rows if r[-1] == "通过" or r[-1] == "拦截" or r[-1] == "拒绝")
    doc = new_doc(title, subtitle)
    meta(doc, cases, passed, note)
    doc.add_heading("一、用例结果", level=1)
    table(doc, ["编号", "用例", "期望", "实际", "结果"],
          [[r[0], r[1], r[2], r[3], r[4]] for r in rows])
    doc.add_heading("二、执行记录截图", level=1)
    for path, cap in shots:
        shot(doc, path, cap)
    doc.add_heading("三、结论", level=1)
    doc.add_paragraph(conclusion)
    doc.save(os.path.join(DOCS, filename))


def main():
    build_normal()

    build_simple(
        "边界场景测试.docx", "边界场景测试", "支付对账与差错追踪 MVP · 输入与规则边界",
        [["B01", "空问题被拒", "400 / 10401", "HTTP 400「问题不能为空」", "通过"],
         ["B01b", "超长问题（501 字）", "400 / 10401", "HTTP 400「问题过长，请控制在 500 字以内」", "通过"],
         ["B02", "超窗差异核验", "passed=false + OUT_OF_WINDOW", "拦截，返回 OUT_OF_WINDOW", "通过"],
         ["B03", "大额告警", "HIGH_AMOUNT warning", "返回 warning 级告警", "通过"],
         ["B04", "不存在的差异", "404 / 10404", "HTTP 404「差异 d_999 不存在」", "通过"]],
        [(os.path.join(IMGS, "B_all.png"),
          "图 1　边界用例执行记录：空问题、超长问题、回溯窗口、大额告警、不存在的差异")],
        "5 条边界用例全部通过。执行中发现一处口径不一致：超长问题原先返回 10400（参数格式错误），"
        "与空问题的 10401 不统一，已改为统一返回 10401 并补测试。",
        "另：凭证过期边界（B06）需改环境变量 TTL，本次未执行，见《测试方案》3.2。")

    build_simple(
        "对抗场景测试.docx", "对抗场景测试", "支付对账与差错追踪 MVP · 凭证与提示词攻击面",
        [["A01", "伪造核验凭证", "400 凭证无效", "HTTP 400 拒绝建单", "拦截"],
         ["A02", "凭证跨差异复用", "400 不匹配", "HTTP 400 拒绝", "拦截"],
         ["A03", "同一凭证重放", "首次 200，二次 409", "HTTP 409「已使用过」", "拦截"],
         ["A04", "提示词注入", "不泄露系统提示词", "未泄露，仅返回条款或兜底", "拦截"]],
        [(os.path.join(IMGS, "A_all.png"),
          "图 1　对抗用例执行记录：伪造凭证、跨差异复用、重放建单、提示词注入")],
        "4 条对抗用例全部被拦。凭证校验覆盖了伪造、张冠李戴与重放三条路径；"
        "提示词注入未导致系统提示词泄露。",
        "上传白名单（A05）属交付版能力，本次未执行。")

    build_simple(
        "权限场景测试.docx", "权限场景测试", "支付对账与差错追踪 MVP · 四眼原则与状态机",
        [["P-01", "建单人给自己复核", "403", "HTTP 403「不能是同一人」", "拒绝"],
         ["P-02", "Agent 写操作必须确认", "停在 G1", "waiting_confirm / create_ticket", "拒绝"],
         ["P-03", "会话终止后仍确认", "409", "HTTP 409「没有待确认的操作」", "拒绝"],
         ["P-04", "已提交工单重复提交", "409", "HTTP 409「状态为 submitted」", "拒绝"]],
        [(os.path.join(IMGS, "P_all.png"),
          "图 1　权限用例执行记录：自审被拒、确认门、终止后确认、重复提交")],
        "4 条权限用例全部被拒。四眼原则在服务端强制（403），不依赖前端隐藏按钮；"
        "状态机对已提交/已终止的操作返回 409。",
        "会话越权（方案 P02/P03）属交付版能力，MVP 无会话体系，本次未执行。")

    build_simple(
        "故障场景测试.docx", "故障场景测试", "支付对账与差错追踪 MVP · 重启与降级",
        [["F-01", "重启后工单保留", "数据完整", "工单仍在，状态未丢", "通过"],
         ["F-02", "未知 Agent 会话", "404 / 10404", "HTTP 404「会话不存在」", "通过"],
         ["F-03", "未配置模型自动降级", "model=mock 且问答可用", "降级成功，问答正常", "通过"],
         ["F-04", "未知接口不泄露堆栈", "404 无堆栈", "返回 404，无内部信息", "通过"]],
        [(os.path.join(IMGS, "F_all.png"),
          "图 1　故障用例执行记录：重启后数据保留、未知会话、模型降级、未知接口")],
        "4 条用例符合预期。服务重启后工单数据完整保留；未配置模型时自动降级为 mock，问答仍可用；"
        "未知接口返回 404 且不含堆栈。",
        "渠道文件未到、解析失败、数据库不可用、流式中断（方案 F01/F02/F03/F05）需演练环境，本次未执行。")

    for name in ("正常场景测试.docx", "边界场景测试.docx", "对抗场景测试.docx",
                 "权限场景测试.docx", "故障场景测试.docx"):
        p = os.path.join(DOCS, name)
        print(f"{name:20s} {os.path.getsize(p):>7,} 字节")


if __name__ == "__main__":
    main()
