// 公共小工具：取数、格式化、DOM 构造（一律 textContent，不拼 innerHTML——原因串 / 代码来自外部数据）。

export async function api(path, opts = {}) {
  const init = { headers: { Accept: "application/json" }, ...opts };
  if (opts.body !== undefined) {
    init.method = opts.method || "PUT";
    init.headers = { ...init.headers, "Content-Type": "application/json", "X-AlphaBot": "1" };
    init.body = JSON.stringify(opts.body);
  } else if (opts.method && opts.method !== "GET") {
    init.headers = { ...init.headers, "X-AlphaBot": "1" };
  }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* 非 JSON 响应按错误处理 */ }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const isNum = (v) => typeof v === "number" && Number.isFinite(v);

export function num(v, dp = 2) {
  if (!isNum(v)) return "—";
  return v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
export const money = (v, dp = 2) => (isNum(v) ? (v < 0 ? "−$" : "$") + num(Math.abs(v), dp) : "—");
export function pct(v, dp = 1) {
  // v 为小数（0.123 → 12.3%）
  return isNum(v) ? `${(v * 100).toFixed(dp)}%` : "—";
}
export function pctPts(v, dp = 1) {
  // v 已是百分数（3.2 → 3.2%）
  return isNum(v) ? `${v.toFixed(dp)}%` : "—";
}
export function usdShort(v) {
  // GEX 类金额：$1.23B / $45.6M / $789K，带符号
  if (!isNum(v)) return "—";
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(0)}K`;
  return `${s}$${a.toFixed(0)}`;
}
export const distPct = (level, spot) => (isNum(level) && isNum(spot) && spot > 0 ? (level / spot - 1) * 100 : null);
export function signedPct(v, dp = 1) {
  return isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(dp)}%` : "—";
}

export const SIGN_LABEL = { positive: "正 gamma", negative: "负 gamma", zero: "零" };
export const CURVE_LABEL = {
  crosses: "有过零点", all_positive: "全段为正", all_negative: "全段为负",
  insufficient_contracts: "合约不足",
};
export const ROUTE_LABEL = { base: "基线档 0.20Δ", far: "远档 0.10Δ", unavailable: "不可用" };
export const ROUTE_SHORT = { base: "base", far: "far", unavailable: "—" };
export const STRUCT_LABEL = {
  short_put: "卖 put", short_call: "卖 call", bull_put_spread: "牛市 put 价差",
  bear_call_spread: "熊市 call 价差", strangle: "宽跨", iron_condor: "铁鹰",
};
export const TENOR_LABEL = { monthly: "月度 21–45 天", weekly: "周度 7–20 天" };
export const EARN_LABEL = { none: "无", after_expiry: "到期后", before_expiry: "到期前 ⚠", unknown: "未知" };
export const SOURCE_LABEL = {
  cboe_close: "收盘后快照", cboe_intraday: "盘中（15 分钟延迟）", cboe_stale_intraday: "收盘后仍是盘中文件",
  demo: "演示数据",
};

export function signChip(sign) {
  if (sign === "positive") return h("span", { class: "chip pos" }, "正 gamma");
  if (sign === "negative") return h("span", { class: "chip neg" }, "负 gamma");
  if (sign === "zero") return h("span", { class: "chip" }, "零");
  return h("span", { class: "chip" }, "不可得");
}

export function timeAgo(sec) {
  if (!isNum(sec)) return "";
  if (sec < 60) return `${Math.round(sec)} 秒前`;
  return `${Math.round(sec / 60)} 分钟前`;
}

export function panel(title, opts = {}, ...body) {
  return h("section", { class: `panel ${opts.class || ""}` },
    (title || opts.right) ? h("div", { class: "panel-head" },
      h("div", {}, title ? h("h2", {}, title) : null, opts.sub ? h("div", { class: "small muted" }, opts.sub) : null),
      opts.right || null) : null,
    ...body);
}

export function seg(options, value, onChange, label) {
  const wrap = h("div", { class: "seg", role: "group", "aria-label": label || "" });
  for (const [v, text] of options) {
    wrap.append(h("button", {
      type: "button", "aria-pressed": String(v === value),
      onclick: () => { if (v !== value) onChange(v); },
    }, text));
  }
  return wrap;
}

export function table(cols, rows, opts = {}) {
  const thead = h("thead", {}, h("tr", {}, cols.map((c) => h("th", { class: c.cls || "", scope: "col" }, c.label))));
  const tbody = h("tbody");
  for (const r of rows) {
    const tr = h("tr", { class: opts.rowClass ? opts.rowClass(r) : "" });
    if (opts.onRow) { tr.classList.add("link"); tr.tabIndex = 0; tr.addEventListener("click", () => opts.onRow(r));
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") opts.onRow(r); }); }
    for (const c of cols) {
      const v = c.render(r);
      tr.append(v instanceof Node && v.tagName === "TD" ? v : h("td", { class: c.cls || "" }, v));
    }
    tbody.append(tr);
  }
  return h("div", { class: "table-wrap" }, h("table", { class: opts.class || "" }, thead, tbody));
}

export function downloadCSV(filename, cols, rows) {
  const esc = (s) => { const t = String(s ?? ""); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
  const lines = [cols.map((c) => esc(c.label)).join(",")];
  for (const r of rows) lines.push(cols.map((c) => esc(c.csv ? c.csv(r) : "")).join(","));
  const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = h("a", { href: URL.createObjectURL(blob), download: filename });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export function loading(text = "加载中…") { return h("div", { class: "empty" }, text); }
export function errorBox(e) { return h("div", { class: "empty err" }, `出错了：${e.message || e}`); }

const store = (() => { try { return window.localStorage; } catch { return null; } })();
export function pref(key, fallback) {
  try { const v = store && store.getItem(`alphabot:${key}`); return v === null || v === undefined ? fallback : JSON.parse(v); }
  catch { return fallback; }
}
export function setPref(key, value) {
  try { store && store.setItem(`alphabot:${key}`, JSON.stringify(value)); } catch { /* 隐私模式等：只是不记住 */ }
}

// replaceChildren 会把 null 渲染成字面 "null"；条件渲染一律经这里
export function fill(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}

// 服务端文案沿用本地 markdown 报告的 **加粗** 记号；前端是纯文本，去掉记号
export const plain = (s) => String(s ?? "").replace(/\*\*/g, "");
