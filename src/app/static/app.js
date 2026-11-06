/* 前端：问答（SSE）+ 差错处理闭环 */

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const state = {
  user: null,
  batchId: "",
  diff: null,
  token: "",
  busy: false,
};

/* ---------------- 通用 ---------------- */
function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), ms);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("登录已过期"); }
  const data = await res.json().catch(() => ({ code: -1, message: "响应不是 JSON" }));
  if (!res.ok || data.code !== 0) {
    throw new Error(data.message || `HTTP ${res.status}`);
  }
  return data.data;
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("#env").textContent = `模型 ${h.model} · 条款 ${h.policies} 条 · ${h.db}`;
  } catch (e) {
    $("#env").textContent = "服务未就绪";
  }
}

/* ---------------- 标签页 ---------------- */
function activateTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  $(`#panel-${name}`).classList.add("active");
  if (name === "diff" && !state.batchId) loadBatches();
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    activateTab(tab.dataset.tab);
    history.replaceState(null, "", tab.dataset.tab === "qa" ? location.pathname : `#${tab.dataset.tab}`);
  });
});

/* ---------------- 当前用户 ---------------- */
async function loadMe() {
  const me = await fetch("/api/auth/me").then(async (r) => {
    if (r.status === 401) { location.href = "/login"; throw new Error("未登录"); }
    const b = await r.json();
    if (b.code !== 0) throw new Error(b.message);
    return b.data;
  });
  state.user = me;
  $("#whoami").textContent = `${me.name}（${me.role === "admin" ? "风险岗" : "对账岗"}）`;
  return me;
}

$("#logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  location.href = "/login";
});

/* ---------------- 问答 ---------------- */
const messages = $("#messages");
const qaEmpty = $("#qa-empty");
const askForm = $("#ask-form");
const askBtn = $("#ask-btn");
const questionBox = $("#question");

function addMessage(role, text) {
  qaEmpty.style.display = "none";
  const wrap = el("div", `msg ${role}`);
  wrap.appendChild(el("div", "role", role === "user" ? "我" : "助手"));
  const body = el("div", "body", text);
  wrap.appendChild(body);
  messages.appendChild(wrap);
  messages.scrollTop = messages.scrollHeight;
  return { wrap, body };
}

function addSources(wrap, items) {
  if (!items.length) return;
  const box = el("div", "sources");
  items.forEach((s) => box.appendChild(el("span", "source", `第${s.clause_no}条 ${s.title}`)));
  wrap.appendChild(box);
  messages.scrollTop = messages.scrollHeight;
}

document.querySelectorAll(".chip").forEach((c) => {
  c.addEventListener("click", () => {
    questionBox.value = c.textContent;
    askForm.requestSubmit();
  });
});

askForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.busy) return;
  const q = questionBox.value.trim();
  if (!q) return;

  state.busy = true;
  askBtn.disabled = true;
  questionBox.value = "";
  addMessage("user", q);
  const { wrap, body } = addMessage("assistant", "");
  const status = el("div", "status", "正在检索制度…");
  wrap.appendChild(status);

  let sourcesDrawn = false;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.message || `HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    const handle = (frame) => {
      const lines = frame.split("\n");
      const ev = lines.find((l) => l.startsWith("event:"))?.slice(6).trim();
      const raw = lines.filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("");
      if (!ev || !raw) return;
      const data = JSON.parse(raw);
      if (ev === "status") status.textContent = data.text;
      if (ev === "sources" && !sourcesDrawn) { sourcesDrawn = true; addSources(wrap, data.items || []); }
      if (ev === "delta") { status.remove(); body.textContent += data.text; messages.scrollTop = messages.scrollHeight; }
      if (ev === "error") throw new Error(data.message);
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        handle(frame);
      }
    }
  } catch (err) {
    status.remove();
    body.textContent = body.textContent || `出错了：${err.message}`;
    toast(err.message);
  } finally {
    state.busy = false;
    askBtn.disabled = false;
  }
});

questionBox.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    askForm.requestSubmit();
  }
});

/* ---------------- 差错：批次与差异 ---------------- */
async function loadBatches() {
  try {
    const rows = await api("/api/batches");
    const tbody = $("#batch-table tbody");
    tbody.innerHTML = "";
    rows.forEach((b) => {
      const tr = el("tr");
      tr.innerHTML = `<td>${b.channel}</td><td>${b.settle_date}</td>
        <td>${b.trade_count.toLocaleString()}</td><td>${b.amount.toLocaleString(undefined, { minimumFractionDigits: 2 })}</td>
        <td>${b.diff_count}</td>`;
      tr.addEventListener("click", () => selectBatch(b, tr));
      tbody.appendChild(tr);
    });
  } catch (err) { toast(err.message); }
}

async function selectBatch(batch, tr) {
  document.querySelectorAll("#batch-table tbody tr").forEach((r) => r.classList.remove("sel"));
  tr.classList.add("sel");
  state.batchId = batch.id;
  try {
    const rows = await api(`/api/diffs?batch_id=${encodeURIComponent(batch.id)}`);
    const tbody = $("#diff-table tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="hint">该批次没有差异</td></tr>';
      return;
    }
    rows.forEach((d) => {
      const tr2 = el("tr");
      tr2.innerHTML = `<td>${d.diff_type}</td><td>${d.amount.toFixed(2)}</td>
        <td>${d.channel_trade_no}</td><td>${statusBadge(d.status)}</td>`;
      tr2.addEventListener("click", () => selectDiff(d, tr2));
      tbody.appendChild(tr2);
    });
  } catch (err) { toast(err.message); }
}

function statusBadge(s) {
  const map = { pending: ["待处理", "warn"], ticketed: ["已建单", "ok"], closed: ["已关单", "ok"] };
  const [label, cls] = map[s] || [s, ""];
  return `<span class="badge ${cls}">${label}</span>`;
}

function selectDiff(d, tr) {
  document.querySelectorAll("#diff-table tbody tr").forEach((r) => r.classList.remove("sel"));
  tr.classList.add("sel");
  state.diff = d;
  state.token = "";
  renderDiff();
}

function renderDiff() {
  const box = $("#diff-detail");
  const d = state.diff;
  if (!d) { box.className = "card empty-card"; box.textContent = "尚未选择差异"; return; }
  box.className = "card";
  box.innerHTML = `
    <dl class="kv">
      <dt>差异编号</dt><dd>${d.diff_id}</dd>
      <dt>类型 / 金额</dt><dd>${d.diff_type} · ${d.amount.toFixed(2)} 元</dd>
      <dt>渠道流水</dt><dd>${d.channel_trade_no}</dd>
      <dt>内部订单</dt><dd>${d.internal_order_no}</dd>
      <dt>发生时间</dt><dd>${d.age_days} 天前</dd>
      <dt>原因</dt><dd>${d.reason}</dd>
      <dt>状态</dt><dd>${statusBadge(d.status)}</dd>
    </dl>
    <div id="diff-issues"></div>
    <div class="actions">
      <button id="btn-verify" class="primary" ${d.status !== "pending" ? "disabled" : ""}>核验差异</button>
    </div>
    <div id="diff-next"></div>`;
  $("#btn-verify")?.addEventListener("click", doVerify);
}

async function doVerify() {
  const btn = $("#btn-verify");
  btn.disabled = true;
  btn.textContent = "核验中…";
  try {
    const r = await api(`/api/diffs/${state.diff.diff_id}/verify`, { method: "POST" });
    state.token = r.token || "";
    const issues = $("#diff-issues");
    issues.innerHTML = r.issues.length
      ? `<ul class="issues">${r.issues.map((i) => `<li class="${i.level}">${i.message}</li>`).join("")}</ul>`
      : '<p class="status">核验通过，未发现阻断项。</p>';
    const next = $("#diff-next");
    if (r.passed) {
      const allow = { "长款": ["补单", "退款"], "短款": ["调单", "补偿扣款"], "金额不符": ["订正"] }[state.diff.diff_type] || [];
      next.innerHTML = `
        <div class="actions">
          <select id="handling">${allow.map((h) => `<option>${h}</option>`).join("")}</select>
          <button id="btn-create" class="primary">建差单</button>
        </div>
        <div class="token">核验凭证：${state.token}</div>`;
      $("#btn-create").addEventListener("click", doCreate);
    } else {
      next.innerHTML = '<p class="status">未通过核验，不能建单。</p>';
    }
  } catch (err) {
    toast(err.message);
  } finally {
    btn.textContent = "核验差异";
  }
}

async function doCreate() {
  const btn = $("#btn-create");
  btn.disabled = true;
  try {
    const ticket = await api("/api/tickets", {
      method: "POST",
      body: JSON.stringify({
        diff_id: state.diff.diff_id,
        token: state.token,
        handling: $("#handling").value,
      }),
    });
    toast(`已建单 ${ticket.ticket_no}`);
    state.diff.status = "ticketed";
    renderDiff();
    loadBatches();
    loadTickets();
  } catch (err) {
    toast(err.message);
    btn.disabled = false;
  }
}

/* ---------------- 差错：工单 ---------------- */
async function loadTickets() {
  try {
    state.tickets = await api("/api/tickets");
    renderTickets(state.tickets);
  } catch (err) { toast(err.message); }
}

function ticketBadge(s) {
  const map = { created: ["待提交", "warn"], submitted: ["待复核", "warn"], reviewed: ["已复核", "ok"] };
  const [label, cls] = map[s] || [s, ""];
  return `<span class="badge ${cls}">${label}</span>`;
}

function renderTickets(list) {
  const box = $("#tickets");
  if (!list || !list.length) { box.className = "tickets empty-card"; box.textContent = "暂无工单"; return; }
  box.className = "tickets card";
  box.innerHTML = "";
  list.forEach((t) => {
    const div = el("div", "ticket");
    const isCreator = state.user && t.created_by === state.user.name;
    div.innerHTML = `
      <div class="ticket-head">
        <span class="ticket-no">${t.ticket_no}</span>
        ${ticketBadge(t.status)}
        <span class="status">${t.diff_type} · ${t.handling} · ${t.amount.toFixed(2)} 元</span>
      </div>
      <div class="status">建单：${t.created_by}${t.reviewed_by ? " · 复核：" + t.reviewed_by : ""}</div>
      <div class="actions">
        <button data-act="submit" ${t.status !== "created" ? "disabled" : ""}>提交复核</button>
        <button data-act="approve" ${t.status !== "submitted" || isCreator ? "disabled" : ""}>复核通过</button>
        <button data-act="reject" ${t.status !== "submitted" || isCreator ? "disabled" : ""}>退回</button>
      </div>
      ${isCreator && t.status === "submitted" ? '<div class="status">当前操作人是建单人，不能复核（双人复核）</div>' : ""}`;
    div.querySelectorAll("button[data-act]").forEach((b) => {
      b.addEventListener("click", () => ticketAction(t, b.dataset.act));
    });
    box.appendChild(div);
  });
}

async function ticketAction(ticket, act) {
  try {
    if (act === "submit") {
      await api(`/api/tickets/${ticket.ticket_id}/submit`, { method: "POST" });
      toast("已提交复核");
    } else {
      await api(`/api/tickets/${ticket.ticket_id}/review`, {
        method: "POST",
        body: JSON.stringify({ approve: act === "approve" }),
      });
      toast(act === "approve" ? "复核通过" : "已退回");
    }
    loadTickets();
  } catch (err) { toast(err.message); }
}


/* ---------------- Agent 代办 ---------------- */
const agentLog = () => $("#agent-log");
const agentPending = () => $("#agent-pending");

function renderAgent(run) {
  const box = agentLog();
  box.innerHTML = "";
  (run.events || []).forEach((e) => {
    const div = el("div", "ticket");
    div.innerHTML = `<div class="ticket-head">
        <span class="badge ${e.ok ? "ok" : "warn"}">${e.ok ? "完成" : "停止"}</span>
        <span class="status">${e.tool}</span>
      </div>`;
    box.appendChild(div);
  });

  const p = run.pending;
  if (!p) {
    agentPending().innerHTML = `<p class="status">状态：${run.status}</p>`;
    return;
  }
  const rows = Object.entries(p.preview || {})
    .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  agentPending().innerHTML = `
    <div class="card">
      <div class="ticket-head">
        <span class="badge warn">待确认</span>
        <span class="status">${p.label}</span>
      </div>
      <dl class="kv">${rows}</dl>
      <div class="actions">
        <button id="agent-ok" class="primary">确认执行</button>
        <button id="agent-no">拒绝</button>
      </div>
    </div>`;
  $("#agent-ok").addEventListener("click", () => agentConfirm(run.run_id, true));
  $("#agent-no").addEventListener("click", () => agentConfirm(run.run_id, false));
}

async function agentRun() {
  const goal = $("#agent-goal").value.trim();
  if (!goal) return;
  $("#agent-run").disabled = true;
  try {
    const run = await api("/api/agent/run", {
      method: "POST",
      body: JSON.stringify({ goal }),
    });
    renderAgent(run);
  } catch (err) { toast(err.message); }
  finally { $("#agent-run").disabled = false; }
}

async function agentConfirm(runId, approved) {
  try {
    const run = await api(`/api/agent/${runId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    });
    renderAgent(run);
    if (run.status === "done") { loadTickets(); loadBatches(); }
  } catch (err) { toast(err.message); }
}

// 用事件委托绑定，避免面板重渲染后丢监听
document.addEventListener("click", (e) => {
  if (e.target && e.target.id === "agent-run") agentRun();
});

/* ---------------- 启动 ---------------- */

loadMe().then(() => { loadHealth(); loadTickets(); }).catch(() => {});
const hashTab = location.hash.replace("#", "");
if (["qa", "diff", "agent"].includes(hashTab)) activateTab(hashTab);
