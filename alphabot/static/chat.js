// 对话抽屉（v0.45.614）：每页右侧，Claude Opus 5.5 + 只读 Alpha Bot 工具 + 联网搜索。规则在 alphabot/chat.py。
// 默认关：开启要人点确认（每问都花 API 费用、数据会发给 Anthropic）。回答一律当纯文本显示（不当 HTML）；
// 搜索来源只显示网址文字、不做成链接——在桌面程序的窗口里点外链会把整个窗口带离 Alpha Bot。
// 抽屉的三块（头 / 消息 / 输入）只建一次（v0.45.616）：每次重建输入框会丢焦点，发完一问还得再点一下才能接着打字。
import { api, h, fill, money } from "./lib.js";

const drawer = document.getElementById("chat-drawer");
const toggle = document.getElementById("chat-toggle");
let status = null;        // GET /api/chat
let convId = null;        // 当前对话（历史存在服务端，关掉 Alpha Bot 就没了）
let log = [];             // 抽屉里显示的消息
let busy = false;
let headEl, bodyEl, formEl, ta, sendBtn;

// 当前页面 ⇒ 随消息发给服务端的上下文（服务端只认登记过的页面名与合规代码）
function pageContext() {
  const [p = "overview", a = "", b = ""] = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (p === "t") return { page: "ticker", ticker: a, tab: b };
  if (p === "intraday") return { page: "intraday", ticker: a };
  return { page: p };
}

const canAsk = () => status && !status.error && !status.demo && status.enabled && status.available !== false;

async function loadStatus() {
  try { status = await api("/api/chat"); }
  catch (e) { status = { error: e.message }; }
  render();
  if (!drawer.hidden && canAsk()) ta.focus();
}

async function setEnabled(on) {
  if (on) {
    const s = status || {};
    const ok = confirm(`开启对话？\n\n· 每问都会调用 Claude（${s.model || "Opus 5.5"}），按实际用量收 API 费用；每条回答下方显示本问花了多少，`
      + `每天上限 ${money(s.cap_usd)}（到了就停）。联网搜索每次另计费。\n`
      + "· 你的问题、正在看的页面，以及它为回答去取的 Alpha Bot 数据，都会发给 Anthropic 的 API（离开这台 Mac）。\n"
      + "· 联网搜索的结果来自网络，未经本系统核实；回答不构成投资建议。");
    if (!ok) return;
  }
  try { await api("/api/settings", { body: { chat_enabled: on } }); }
  catch (e) { alert(`设置失败：${e.message}`); return; }
  await loadStatus();
}

async function send() {
  const text = ta.value.trim();
  if (!text || busy) return;
  busy = true; ta.value = "";
  log.push({ role: "user", text }, { role: "pending" });
  render();
  try {
    const r = await api("/api/chat", { method: "POST", body: { message: text, conversation_id: convId, page: pageContext() } });
    convId = r.conversation_id;
    log.pop(); log.push({ role: "bot", r });
    if (status) status.spent_today = r.spent_today;
  } catch (e) {
    log.pop(); log.push({ role: "err", text: e.message });
    loadStatus();
  }
  busy = false;
  render();
  if (!drawer.hidden) ta.focus();
}

function newConversation() { convId = null; log = []; render(); ta.focus(); }

function botMessage(r) {
  const meta = [`本问 ${money(r.cost_usd, 3)}`, `API ${r.api_calls} 次`];
  if (r.searches) meta.push(`搜索 ${r.searches} 次`);
  if (r.served_by) meta.push(`由替补模型 ${r.served_by} 回答（原模型拒答）`);
  const tools = (r.tools || []).map((t) => `${t.name}${t.input && t.input.ticker ? " " + t.input.ticker : ""}${t.ok ? "" : "（失败）"}`);
  return h("div", { class: "chat-msg bot" },
    r.reply || "（没有文字回答）",
    ...(r.notes || []).map((n) => h("div", { class: "chat-note" }, n)),
    tools.length ? h("div", { class: "chat-meta" }, "取数：", tools.join("、")) : null,
    r.sources && r.sources.length ? h("ol", { class: "chat-sources" },
      ...r.sources.map((s) => h("li", {}, s.title ? `${s.title} — ` : "", h("span", { class: "chat-url" }, s.url)))) : null,
    h("div", { class: "chat-meta" }, meta.join(" · ")));
}

function body() {
  const s = status;
  if (!s) return [h("p", { class: "muted" }, "加载中…")];
  if (s.error) return [h("p", { class: "chat-msg err" }, `读不到对话状态：${s.error}`)];
  if (s.demo) return [h("p", { class: "muted" }, "演示模式不开对话：数据是合成的，花费也记不住。")];
  if (!s.enabled) {
    return [
      h("p", {}, "用 Claude 问 Alpha Bot 的数据：它能按需调只读工具取水平、路由、溢价、两本纸面账本、检验就绪度与口径规则，也能联网搜索。"),
      h("p", { class: "muted" }, `默认关。每问都产生 API 费用（每天上限 ${money(s.cap_usd)}）；你的问题与取到的数据会发给 Anthropic。详见帮助「对话」。`),
      h("button", { class: "btn primary", type: "button", onclick: () => setEnabled(true) }, "开启对话"),
    ];
  }
  if (s.available === false) {
    return [h("p", { class: "chat-msg err" }, "调不了 Claude API（取不到 API 客户端）。"),
      h("button", { class: "link-btn", type: "button", onclick: () => setEnabled(false) }, "关闭对话")];
  }
  const msgs = log.map((m) => {
    if (m.role === "user") return h("div", { class: "chat-msg user" }, m.text);
    if (m.role === "pending") return h("div", { class: "chat-msg bot muted" }, "思考与取数中…（复杂问题可能要一两分钟）");
    if (m.role === "err") return h("div", { class: "chat-msg err" }, m.text);
    return botMessage(m.r);
  });
  if (!msgs.length) msgs.push(h("p", { class: "muted" }, "例如：「NVDA 现在是什么 gamma 环境？月度路由为什么退到远档？」"));
  return msgs;
}

function render() {
  const s = status || {};
  const spent = s.enabled ? `今天 ${s.spent_today == null ? "—" : money(s.spent_today, 2)} / ${money(s.cap_usd, 2)}` : "";
  fill(headEl,
    h("strong", {}, "对话"),
    h("span", { class: "muted" }, spent),
    canAsk() ? h("button", { class: "link-btn", type: "button", onclick: newConversation }, "新对话") : null,
    h("button", { class: "link-btn", type: "button", "aria-label": "收起对话抽屉（Esc）", onclick: () => open(false) }, "收起"));
  fill(bodyEl, ...body());
  bodyEl.scrollTop = bodyEl.scrollHeight;
  formEl.hidden = !canAsk();
  ta.disabled = busy;
  sendBtn.disabled = busy;
  sendBtn.textContent = busy ? "等待回答…" : "发送";
}

// 抽屉从顶栏下沿开始（顶栏是 sticky，下沿固定）：不盖住顶栏右侧的「对话」按钮
function placeBelowTopbar() {
  const tb = document.querySelector(".topbar");
  drawer.style.top = tb ? `${Math.round(tb.getBoundingClientRect().bottom)}px` : "0px";
}

function open(on) {
  drawer.hidden = !on;
  toggle.setAttribute("aria-expanded", on ? "true" : "false");
  if (on) { placeBelowTopbar(); render(); loadStatus(); }
  else toggle.focus();
}

export function initChat() {
  if (!drawer || !toggle) return;
  headEl = h("div", { class: "chat-head" });
  bodyEl = h("div", { class: "chat-body" });
  ta = h("textarea", { "aria-label": "给 Claude 的消息", placeholder: "问点什么…（⌘ / Ctrl + Enter 发送）", maxlength: "4000" });
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.isComposing) { e.preventDefault(); send(); } });
  sendBtn = h("button", { class: "btn primary", type: "button", onclick: send }, "发送");
  formEl = h("div", { class: "chat-form" }, ta,
    h("div", { class: "row" }, h("button", { class: "link-btn", type: "button", onclick: () => setEnabled(false) }, "关闭对话"), sendBtn));
  fill(drawer, headEl, bodyEl, formEl);
  drawer.addEventListener("keydown", (e) => { if (e.key === "Escape" && !e.isComposing) { e.preventDefault(); open(false); } });
  window.addEventListener("resize", () => { if (!drawer.hidden) placeBelowTopbar(); });
  toggle.addEventListener("click", () => open(drawer.hidden));
}
