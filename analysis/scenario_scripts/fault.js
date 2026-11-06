// 故障场景：重启后数据保留、未知资源、模型降级、未知路由
(async () => {
  const rows = [];
  const add = (id, name, req, expect, actual, ok) => rows.push({ id, name, req, expect, actual, ok });

  // F01 服务重启后数据保留
  {
    const r = await fetch("/api/tickets");
    const b = await r.json();
    const list = b.data || [];
    const kept = list.length > 0 && list.every((t) => t.ticket_no && t.status);
    add("F01", "服务重启后工单数据保留", "停服 → 重启 → GET /api/tickets",
        "工单仍在，状态未丢",
        `HTTP ${r.status}｜工单 ${list.length} 张${list[0] ? "，如 " + list[0].ticket_no + "（" + list[0].status + "）" : ""}`,
        kept);
  }

  // F02 未知 Agent 会话
  {
    const r = await fetch("/api/agent/ar_not_exist");
    const b = await r.json();
    add("F02", "未知 Agent 会话", "GET /api/agent/ar_not_exist", "404 / 10404",
        `HTTP ${r.status}｜${b.message}`, r.status === 404 && b.code === 10404);
  }

  // F03 模型未配置时降级
  {
    const h = await (await fetch("/api/health")).json();
    const c = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: "长款怎么处理？" }),
    });
    const text = await c.text();
    const ok = h.data.model === "mock" && text.includes("event: done");
    add("F03", "未配置模型时自动降级", "GET /api/health + POST /api/chat",
        "model=mock 且问答仍可完成",
        `model=${h.data.model}｜问答 ${text.includes("event: done") ? "正常返回 done" : "未完成"}`,
        ok);
  }

  // F04 未知路由返回 JSON 而不是堆栈
  {
    const r = await fetch("/api/not-exist");
    const text = await r.text();
    const noTrace = !/Traceback|File "|sqlite3\./i.test(text);
    add("F04", "未知接口不泄露内部信息", "GET /api/not-exist",
        "404 且无堆栈", `HTTP ${r.status}｜响应 ${text.slice(0, 40).replace(/\n/g, " ")}`, r.status === 404 && noTrace);
  }

  const esc = (s) => String(s).replace(/[<>&]/g, (m) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[m]));
  document.body.innerHTML = `
    <div style="font:14px/1.7 'Microsoft YaHei',sans-serif;background:#fff;min-height:100vh;padding:32px 40px;">
      <h1 style="font-size:22px;margin:0 0 4px;">故障场景测试 · 执行记录</h1>
      <p style="color:#6b7686;margin:0 0 20px;font-size:12px;">
        被测系统：支付对账与差错追踪 MVP ｜ 环境：127.0.0.1:8097（已重启一次）｜ 执行时间：${new Date().toLocaleString("zh-CN")}
      </p>
      <table style="border-collapse:collapse;width:100%;font-size:13px;">
        <thead><tr style="background:#f8fafd;color:#6b7686;text-align:left;">
          <th style="padding:8px 10px;border:1px solid #e3e8f0;">编号</th>
          <th style="padding:8px 10px;border:1px solid #e3e8f0;">故障注入</th>
          <th style="padding:8px 10px;border:1px solid #e3e8f0;">操作</th>
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
            <td style="padding:8px 10px;border:1px solid #e3e8f0;font-weight:700;color:${r.ok ? "#1e7d54" : "#b3261e"};">${r.ok ? "符合预期" : "不符合"}</td>
          </tr>`).join("")}
        </tbody>
      </table>
      <p style="margin-top:18px;font-size:13px;">
        合计 ${rows.length} 条，符合预期 <b style="color:#1e7d54">${rows.filter((r) => r.ok).length}</b>，
        不符合 <b style="color:${rows.some((r) => !r.ok) ? "#b3261e" : "#6b7686"}">${rows.filter((r) => !r.ok).length}</b>。
      </p>
    </div>`;
  return rows.map((r) => `${r.id}:${r.ok ? "PASS" : "FAIL"}`).join(" ");
})()
