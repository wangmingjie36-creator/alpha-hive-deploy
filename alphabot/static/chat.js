// 对话抽屉（v0.45.614）：每页右侧，Claude Opus 5.5 + 只读 Alpha Bot 工具 + 联网搜索。规则在 alphabot/chat.py。
// 默认关：开启要人点确认（每问都花 API 费用、数据会发给 Anthropic）。回答一律当纯文本显示（不当 HTML）；
// 搜索来源只显示网址文字、不做成链接——在桌面程序的窗口里点外链会把整个窗口带离 Alpha Bot。
// 抽屉的三块（头 / 消息 / 输入）只建一次（v0.45.616）：每次重建输入框会丢焦点，发完一问还得再点一下才能接着打字。
// 历史对话（v0.45.808，用户 11-23 定「像 Claude Code 一样保留，不要的我自己归档」）：对话存在服务端磁盘上，「历史」列出来、
// 点开接着聊；归档只是从列表里藏起来，随时取消归档，没有删除。上次开着的对话记在 localStorage（只是方便，取不到就从新对话开始）。
import { api, h, fill, money } from "./lib.js";

const drawer = document.getElementById("chat-drawer");
const toggle = document.getElementById("chat-toggle");
let status = null;        // GET /api/chat
let convId = null;        // 当前对话（存在服务端磁盘上，v0.45.808）
let convArchived = false; // 当前对话是归档里点开的（再问一句服务端自动取消归档）
let log = [];             // 抽屉里显示的消息
let busy = false;
let view = "chat";        // "chat" | "history"
let showArchived = false; // 历史列表看的是不是「已归档」
let hist = null;          // 历史列表：{ data } | { error } | null（加载中）
let restored = false;     // 打开抽屉时是否已试过恢复上次的对话
let openSeq = 0, histSeq = 0;   // 异步取回的结果只认最后一次
const LAST_KEY = "alphabot.chat.last";

function remember(id) {
  try { if (id) localStorage.setItem(LAST_KEY, id); else localStorage.removeItem(LAST_KEY); } catch { /* 存不了就不记 */ }
}
function recalled() {
  try { return localStorage.getItem(LAST_KEY); } catch { return null; }
}
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
  catch (e) {
    // 已有能用的状态（对话进行中）⇒ 保留它、只加一条提示，不把整个对话区换成错误——发送成功后重取失败时，刚付钱的回答与
    // 输入框会一起消失（v0.45.637 二次检查 #2）；一开始就取不到才整个显示错误
    status = status && !status.error ? { ...status, status_error: e.message } : { error: e.message };
  }
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
    convId = r.conversation_id; convArchived = false; remember(convId);
    log.pop(); log.push({ role: "bot", r });
    loadStatus();          // 今天已花 / spend_error 只认服务端那一处（v0.45.637；此前只抄回 spent_today，spend_error 留旧值）
  } catch (e) {
    log.pop(); log.push({ role: "err", text: e.message });
    if (!ta.value) ta.value = text;      // 失败了把问题放回输入框：报错常说「可以再问一次」，别让人重打（v0.45.618）
    loadStatus();
  }
  busy = false;
  render();
  if (!drawer.hidden) ta.focus();
}

function newConversation() {
  if (busy) return;
  convId = null; convArchived = false; log = []; view = "chat"; remember(null);
  render(); if (canAsk()) ta.focus();
}

// 打开一场历史对话：显示存下的每一问（用户原话 + 当时的结果）；接着问就接在它后面
async function openConversation(id, { quiet = false } = {}) {
  if (busy) return;
  const before = convId, seq = ++openSeq;
  // 取回来之前人已经发了一问 / 换了对话 / 又点了另一场：这次结果作废（808 二次检查 #4：异步恢复会盖掉正在发的那问）
  const stale = () => busy || convId !== before || seq !== openSeq;
  try {
    const c = await api(`/api/chat/conversations/${encodeURIComponent(id)}`);
    if (stale()) return;
    convId = c.id; convArchived = !!c.archived; remember(convId);
    log = (c.display || []).map((d) => (d.role === "user" ? { role: "user", text: d.text } : { role: "bot", r: d.r || {} }));
  } catch (e) {
    if (stale()) return;
    if (quiet && e.status === 404) { remember(null); return; }      // 上次那场不在了：从新对话开始
    convId = null; convArchived = false; remember(null);
    log = [{ role: "err", text: `这场对话打不开：${e.message}` }];
  }
  view = "chat";
  render();
  if (canAsk()) ta.focus();
}

async function loadHistory() {
  const seq = ++histSeq;            // 快速来回切「已归档」时，晚到的旧回复别盖掉新的
  hist = null; render();
  let next;
  try { next = { data: await api(`/api/chat/conversations${showArchived ? "?archived=1" : ""}`) }; }
  catch (e) { next = { error: e.message }; }
  if (seq !== histSeq) return;
  hist = next;
  render();
}

async function setArchived(id, archived) {
  if (busy && id === convId) return;
  try { await api(`/api/chat/conversations/${encodeURIComponent(id)}`, { method: "POST", body: { archived } }); }
  catch (e) { alert(`${archived ? "归档" : "取消归档"}失败：${e.message}`); return; }
  if (id === convId) {
    if (archived) { convId = null; convArchived = false; log = []; remember(null); }   // 归档了正开着的那场 ⇒ 回到新对话
    else convArchived = false;
  }
  loadHistory();
}

function showHistory(on) {
  view = on ? "history" : "chat";
  if (on) loadHistory(); else render();
}

function when(sec) {
  if (!sec) return "—";
  const d = new Date(sec * 1000), p = (n) => String(n).padStart(2, "0");
  const md = `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  return d.getFullYear() === new Date().getFullYear() ? md : `${d.getFullYear()}-${md}`;
}

function historyBody() {
  const out = [h("div", { class: "chat-hist-bar" },
    h("strong", {}, showArchived ? "已归档的对话" : "历史对话"),
    hist && hist.data ? h("button", { class: "link-btn", type: "button",
      onclick: () => { showArchived = !showArchived; loadHistory(); } },
    showArchived ? `返回未归档（${hist.data.active_count}）` : `已归档（${hist.data.archived_count}）`) : null)];
  if (!hist) return [...out, h("p", { class: "muted" }, "加载中…")];
  if (hist.error) return [...out, h("p", { class: "chat-msg err" }, `读不到历史对话：${hist.error}`)];
  const rows = hist.data.conversations || [];
  if (!rows.length) {
    out.push(h("p", { class: "muted" }, showArchived ? "没有已归档的对话。" : "还没有存下来的对话。问一句就会出现在这里。"));
    return out;
  }
  for (const c of rows) {
    const meta = c.error ? c.error : `${when(c.updated)} · ${c.turns} 问${c.unsaved ? " · 没存上盘" : ""}`;
    out.push(h("div", { class: `chat-hist-row${c.id === convId ? " current" : ""}` },
      h("button", { class: "chat-hist-open", type: "button", disabled: busy || !!c.error,
        title: c.title, onclick: () => openConversation(c.id) },
      h("span", { class: "chat-hist-title" }, c.title),
      h("span", { class: c.error ? "chat-hist-meta warn" : "chat-hist-meta" }, meta)),
      c.error ? null : h("button", { class: "link-btn", type: "button", disabled: busy && c.id === convId,
        title: busy && c.id === convId ? "正在等这场的回答，答完再归档" : null, onclick: () => setArchived(c.id, !showArchived) },
        showArchived ? "取消归档" : "归档")));
  }
  return out;
}

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
  if (convArchived) msgs.unshift(h("p", { class: "muted" }, "这场对话已归档；再问一句会自动取消归档。"));
  // 花费记录读不了：顶栏只能显示「—」，原因（与发送时被拒的那句相同）放在最上面（v0.45.637，635 二次检查 #2）
  if (s.spend_error) msgs.unshift(h("p", { class: "chat-msg err" }, s.spend_error));
  if (s.status_error) msgs.unshift(h("p", { class: "chat-msg err" }, `对话状态暂时取不到（${s.status_error}）：上面的「今天已花」可能不是最新的。`));
  return msgs;
}

const canBrowse = () => status && !status.error && !status.demo;

function render() {
  const s = status || {};
  const spent = s.enabled ? `今天 ${s.spent_today == null ? "—" : money(s.spent_today, 2)} / ${money(s.cap_usd, 2)}` : "";
  const inHistory = view === "history" && canBrowse();
  fill(headEl,
    h("strong", {}, "对话"),
    h("span", { class: "muted" }, spent),
    canBrowse() ? h("button", { class: "link-btn", type: "button", "aria-pressed": inHistory ? "true" : "false",
      onclick: () => showHistory(!inHistory) }, inHistory ? "返回" : "历史") : null,
    canAsk() ? h("button", { class: "link-btn", type: "button", disabled: busy, onclick: newConversation }, "新对话") : null,
    h("button", { class: "link-btn", type: "button", "aria-label": "收起对话抽屉（Esc）", onclick: () => open(false) }, "收起"));
  fill(bodyEl, ...(inHistory ? historyBody() : body()));
  bodyEl.scrollTop = inHistory ? 0 : bodyEl.scrollHeight;
  formEl.hidden = inHistory || !canAsk();
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
  if (on) {
    placeBelowTopbar(); render(); loadStatus();
    // 第一次打开：接着上次开着的那场（服务端已存盘）；不在了就从新对话开始
    if (!restored) { restored = true; const last = recalled(); if (last && !convId && !log.length) openConversation(last, { quiet: true }); }
  } else toggle.focus();
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
