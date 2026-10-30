/* ============================================================================
   管理后台公共脚本
   ----------------------------------------------------------------------------
   只做两件全局的事：
   1. toast()  —— 统一的轻提示，页面里任何地方都能调；
   2. 时间格式化 —— 服务端输出的是毫秒时间戳，交给浏览器按本地时区渲染，
      避免"服务器在东八区、用户在西五区"时显示错时间。
============================================================================ */

/**
 * 轻提示。
 * @param {string} msg  提示内容
 * @param {'ok'|'error'} type
 */
function toast(msg, type = 'ok') {
  const el = document.getElementById('toast');
  if (!el) return alert(msg);

  el.textContent = msg;
  el.className = 'toast show' + (type === 'error' ? ' err' : '');

  clearTimeout(el._timer);
  el._timer = setTimeout(() => {
    el.className = 'toast';
  }, 2600);
}

/**
 * 把页面上所有 [data-ts] 元素填成可读时间。
 * 用 textContent 而不是 innerHTML：数据来自数据库，不给 XSS 留口子。
 */
function renderTimestamps() {
  document.querySelectorAll('[data-ts]').forEach((el) => {
    const ts = Number(el.dataset.ts);
    if (!ts) return;

    const d = new Date(ts);
    const pad = (n) => String(n).padStart(2, '0');
    el.textContent =
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  });
}

/* 统一的 fetch 包装：既解析业务错误码，也挡住网络异常 */
async function api(url, options = {}) {
  const resp = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });

  // 40101：登录过期，直接跳登录页，不要在原地弹错误
  if (resp.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('登录已过期');
  }

  const json = await resp.json().catch(() => null);
  if (!json) throw new Error(`响应不是 JSON（HTTP ${resp.status}）`);
  if (json.code !== 0) throw new Error(`[${json.code}] ${json.message}`);
  return json.data;
}

document.addEventListener('DOMContentLoaded', renderTimestamps);
