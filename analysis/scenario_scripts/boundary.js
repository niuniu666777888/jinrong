// 边界场景：在页面上下文中直接调接口，把请求/响应渲染成报告页，便于截图留证。
(async () => {
  const H = (s) => s;
  const cases = [
    {
      id: "B01", name: "空问题被拒",
      req: "POST /api/chat  question='   '",
      call: () => fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: "   " }) }),
      check: (s, b) => s === 400 && b.code === 10401,
      expect: "400 / 10401",
    },
    {
      id: "B01b", name: "超长问题被拒（501 字）",
      req: "POST /api/chat  question='长'.repeat(501)",
      call: () => fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: "长".repeat(501) }) }),
      check: (s, b) => s === 400 && b.code === 10401,
      expect: "400 / 10401",
    },
    {
      id: "B02", name: "超窗差异核验被拦",
      req: "POST /api/diffs/d_004/verify（9 天前发生，窗口 7 天）",
      call: () => fetch("/api/diffs/d_004/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operator: "唐映秋" }) }),
      check: (s, b) => s === 200 && b.data.passed === false && b.data.issues.some((i) => i.rule === "OUT_OF_WINDOW"),
      expect: "passed=false + OUT_OF_WINDOW",
    },
    {
      id: "B03", name: "大额告警（>3000 元）",
      req: "同一响应中的 issues",
      call: () => fetch("/api/diffs/d_004/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operator: "唐映秋" }) }),
      check: (s, b) => b.data.issues.some((i) => i.rule === "HIGH_AMOUNT" && i.level === "warning"),
      expect: "HIGH_AMOUNT warning",
    },
    {
      id: "B04", name: "不存在的差异",
      req: "POST /api/diffs/d_999/verify",
      call: () => fetch("/api/diffs/d_999/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operator: "唐映秋" }) }),
      check: (s, b) => s === 404 && b.code === 10404,
      expect: "404 / 10404",
    },
  ];

  const rows = [];
  for (const c of cases) {
    const t0 = performance.now();
    let status = 0, body = null;
    try {
      const r = await c.call();
      status = r.status;
      body = await r.json().catch(() => null);
    } catch (e) {
      status = "ERR"; body = { message: e.message };
    }
    let ok = false;
    try { ok = c.check(status, body); } catch { ok = false; }
    rows.push({ ...c, status, body, ok, ms: Math.round(performance.now() - t0) });
  }

  const esc = (s) => String(s).replace(/[<>&]/g, (m) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[m]));
  const detail = (b) => {
    if (!b) return "-";
    if (b.data && b.data.issues) return b.data.issues.map((i) => `${i.level}:${i.rule}`).join(", ") || "无";
    if (b.data && b.data.passed !== undefined) return `passed=${b.data.passed}`;
    return b.message || JSON.stringify(b).slice(0, 60);
  };

  document.body.innerHTML = `
    <div style="font:14px/1.7 'Microsoft YaHei',sans-serif;background:#fff;min-height:100vh;padding:32px 40px;">
      <h1 style="font-size:22px;margin:0 0 4px;">边界场景测试 · 执行记录</h1>
      <p style="color:#6b7686;margin:0 0 20px;font-size:12px;">
        被测系统：支付对账与差错追踪 MVP ｜ 环境：127.0.0.1:8097 ｜ 模型：mock ｜
        执行时间：${new Date().toLocaleString("zh-CN")}
      </p>
      <table style="border-collapse:collapse;width:100%;font-size:13px;">
        <thead>
          <tr style="background:#f8fafd;color:#6b7686;text-align:left;">
            <th style="padding:8px 10px;border:1px solid #e3e8f0;">编号</th>
            <th style="padding:8px 10px;border:1px solid #e3e8f0;">用例</th>
            <th style="padding:8px 10px;border:1px solid #e3e8f0;">请求</th>
            <th style="padding:8px 10px;border:1px solid #e3e8f0;">期望</th>
            <th style="padding:8px 10px;border:1px solid #e3e8f0;">实际</th>
            <th style="padding:8px 10px;border:1px solid #e3e8f0;">结果</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => `
            <tr>
              <td style="padding:8px 10px;border:1px solid #e3e8f0;font-family:Consolas,monospace;">${r.id}</td>
              <td style="padding:8px 10px;border:1px solid #e3e8f0;">${esc(r.name)}</td>
              <td style="padding:8px 10px;border:1px solid #e3e8f0;color:#44506a;">${esc(r.req)}</td>
              <td style="padding:8px 10px;border:1px solid #e3e8f0;">${esc(r.expect)}</td>
              <td style="padding:8px 10px;border:1px solid #e3e8f0;">HTTP ${r.status}｜${esc(detail(r.body))}</td>
              <td style="padding:8px 10px;border:1px solid #e3e8f0;font-weight:700;color:${r.ok ? "#1e7d54" : "#b3261e"};">
                ${r.ok ? "通过" : "失败"}
              </td>
            </tr>`).join("")}
        </tbody>
      </table>
      <p style="margin-top:18px;font-size:13px;">
        合计 ${rows.length} 条，通过 <b style="color:#1e7d54">${rows.filter((r) => r.ok).length}</b>，
        失败 <b style="color:${rows.some((r) => !r.ok) ? "#b3261e" : "#6b7686"}">${rows.filter((r) => !r.ok).length}</b>。
      </p>
    </div>`;
  return rows.map((r) => `${r.id}:${r.ok ? "PASS" : "FAIL"}`).join(" ");
})()
