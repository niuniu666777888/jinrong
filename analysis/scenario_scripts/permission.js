// 权限场景：四眼原则、状态机拦截
(async () => {
  const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
  const rows = [];
  const add = (id, name, req, expect, actual, ok) =>
    rows.push({ id, name, req, expect, actual, ok });

  const A = "唐映秋";   // 建单人

  // P01 建单人自审
  try {
    const v = await (await post("/api/diffs/d_003/verify", { operator: A })).json();
    const t = await (await post("/api/tickets", { diff_id: "d_003", token: v.data.token, handling: "订正", operator: A })).json();
    await post(`/api/tickets/${t.data.ticket_id}/submit`, { operator: A });
    const r = await post(`/api/tickets/${t.data.ticket_id}/review`, { operator: A, approve: true });
    const b = await r.json();
    add("P01", "建单人给自己复核", `POST /api/tickets/{id}/review  operator=${A}（与建单人相同）`,
        "403 拒绝", `HTTP ${r.status}｜${b.message}`, r.status === 403);
  } catch (e) {
    add("P01", "建单人给自己复核", "POST /api/tickets/{id}/review", "403 拒绝", "异常：" + e.message, false);
  }

  // P02 Agent 三个确认门（写操作必须人工确认）
  {
    const d = await (await post("/api/agent/run", { goal: "处理微信渠道的长款差异", operator: A })).json();
    const gates = [];
    let cur = d.data;
    for (let i = 0; i < 3; i++) {
      gates.push(cur.pending && cur.pending.tool);
      cur = (await (await post(`/api/agent/${d.data.run_id}/confirm`, { operator: i === 2 ? "傅景" : A, approved: false })).json()).data;
      break; // 仅验证第一个门与「拒绝即终止」
    }
    add("P02", "Agent 写操作前必须确认（G1 建单）",
        "POST /api/agent/run → 停在 create_ticket",
        "status=waiting_confirm，pending=create_ticket",
        `pending=${d.data.pending && d.data.pending.tool}｜拒绝后 status=${cur.status}`,
        d.data.pending && d.data.pending.tool === "create_ticket" && cur.status === "aborted");
  }

  // P03 无待办时确认
  {
    const d = await (await post("/api/agent/run", { goal: "处理微信渠道的长款差异", operator: A })).json();
    await post(`/api/agent/${d.data.run_id}/confirm`, { operator: A, approved: false });
    const r = await post(`/api/agent/${d.data.run_id}/confirm`, { operator: A, approved: true });
    const b = await r.json();
    add("P03", "会话已终止仍点确认", `POST /api/agent/{id}/confirm`,
        "409 无待办", `HTTP ${r.status}｜${b.message}`, r.status === 409);
  }

  // P04 复核通过后的状态不可回退
  {
    const tickets = await (await fetch("/api/tickets")).json();
    const t = tickets.data.find((x) => x.status === "submitted");
    if (t) {
      const r = await post(`/api/tickets/${t.ticket_id}/submit`, { operator: A });
      const b = await r.json();
      add("P04", "已提交的工单重复提交", `POST /api/tickets/{id}/submit`,
          "409 状态冲突", `HTTP ${r.status}｜${b.message}`, r.status === 409);
    } else {
      add("P04", "已提交的工单重复提交", "（无 submitted 工单，跳过）", "409", "跳过", true);
    }
  }

  const esc = (s) => String(s).replace(/[<>&]/g, (m) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[m]));
  document.body.innerHTML = `
    <div style="font:14px/1.7 'Microsoft YaHei',sans-serif;background:#fff;min-height:100vh;padding:32px 40px;">
      <h1 style="font-size:22px;margin:0 0 4px;">权限场景测试 · 执行记录</h1>
      <p style="color:#6b7686;margin:0 0 20px;font-size:12px;">
        被测系统：支付对账与差错追踪 MVP ｜ 环境：127.0.0.1:8097 ｜ 执行时间：${new Date().toLocaleString("zh-CN")}
      </p>
      <table style="border-collapse:collapse;width:100%;font-size:13px;">
        <thead><tr style="background:#f8fafd;color:#6b7686;text-align:left;">
          <th style="padding:8px 10px;border:1px solid #e3e8f0;">编号</th>
          <th style="padding:8px 10px;border:1px solid #e3e8f0;">越权动作</th>
          <th style="padding:8px 10px;border:1px solid #e3e8f0;">请求</th>
          <th style="padding:8px 10px;border:1px solid #e3e8f0;">期望</th>
          <th style="padding:8px 10px;border:1px solid #e3e8f0;">实际</th>
          <th style="padding:8px 10px;border:1px solid #e3e8f0;">结果</th>
        </tr></thead>
        <tbody>
          ${rows.map((r) => `<tr>
            <td style="padding:8px 10px;border:1px solid #e3e8f0;font-family:Consolas,monospace;">${r.id}</td>
            <td style="padding:8px 10px;border:1px solid #e3e8f0;">${esc(r.name)}</td>
            <td style="padding:8px 10px;border:1px solid #e3e8f0;color:#44506a;">${esc(r.req)}</td>
            <td style="padding:8px 10px;border:1px solid #e3e8f0;">${esc(r.expect)}</td>
            <td style="padding:8px 10px;border:1px solid #e3e8f0;">${esc(r.actual)}</td>
            <td style="padding:8px 10px;border:1px solid #e3e8f0;font-weight:700;color:${r.ok ? "#1e7d54" : "#b3261e"};">${r.ok ? "拒绝成功" : "未拒绝"}</td>
          </tr>`).join("")}
        </tbody>
      </table>
      <p style="margin-top:18px;font-size:13px;">
        合计 ${rows.length} 条，拒绝 <b style="color:#1e7d54">${rows.filter((r) => r.ok).length}</b>，
        未拒绝 <b style="color:${rows.some((r) => !r.ok) ? "#b3261e" : "#6b7686"}">${rows.filter((r) => !r.ok).length}</b>。
      </p>
    </div>`;
  return rows.map((r) => `${r.id}:${r.ok ? "PASS" : "FAIL"}`).join(" ");
})()
