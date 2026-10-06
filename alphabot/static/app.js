// Alpha Bot 前端入口：hash 路由 + 顶栏（搜索 / 交易时段）+ 各页面。
import { api, h, errorBox, loading, pref, setPref, fill } from "./lib.js";
import { disposeAll } from "./charts.js";
import { renderOverview } from "./pages/overview.js";
import { renderTicker } from "./pages/ticker.js";
import { renderIntraday } from "./pages/intraday.js";
import { renderLedger } from "./pages/ledger.js";
import { renderResults } from "./pages/results.js";
import { renderMethod } from "./pages/method.js";
import { renderHelp } from "./pages/help.js";

export const state = { meta: null };
const app = document.getElementById("app");

export async function refreshMeta() {
  state.meta = await api("/api/meta");
  const s = state.meta.session || {};
  const sess = document.getElementById("session");
  fill(sess, 
    h("span", { class: `dot ${s.live ? "on" : ""}`, "aria-hidden": "true" }),
    h("span", {}, s.live ? "美股盘中" : (s.trading_day ? "今日已收盘 / 未开盘" : "非交易日")),
    h("span", { class: "num" }, (s.now_et || "").slice(11, 16) + " ET"),
  );
  document.getElementById("demo-banner").hidden = !state.meta.demo;
  const dl = document.getElementById("ticker-list");
  fill(dl, ...(state.meta.watchlist?.tickers || []).map((t) => h("option", { value: t })));
  fill(document.getElementById("foot"),
    h("span", {}, `Alpha Bot ${state.meta.version} · 本机运行，只读卖权账本 · 以下为公开信息研究与情景推演，不构成投资建议。`),
    state.meta.can_shutdown ? h("button", { class: "link-btn", type: "button", onclick: stopServer }, "停止服务") : null,
  );
  return state.meta;
}

async function stopServer() {
  if (!confirm("停止 Alpha Bot 本机服务？盘中定时快照也会一起停。")) return;
  try { await api("/api/shutdown", { method: "POST" }); }
  catch (e) { alert(`停止失败：${e.message}`); return; }
  // 服务已停：别再轮询 / 路由，否则之后同端口再起服务时，这个旧页面会把顶栏、页脚又填回来
  clearInterval(metaTimer);
  window.removeEventListener("hashchange", route);
  seq++;
  disposeAll();
  fill(app, h("div", { class: "panel stopped" },
    h("h2", {}, "服务已停止"),
    h("p", {}, "可以关掉这个页面。再次使用：双击 Alpha Bot.app，或在终端运行 make alphabot。")));
  fill(document.getElementById("foot"));
  fill(document.getElementById("session"));
}

export function tickers() { return state.meta?.watchlist?.tickers || []; }
export function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }

document.getElementById("search").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("search-input");
  const t = input.value.trim().toUpperCase();
  if (!/^[A-Z_][A-Z0-9.\-]{0,9}$/.test(t)) { input.setCustomValidity("代码格式不对"); input.reportValidity(); return; }
  input.setCustomValidity(""); input.value = "";
  const onIntraday = location.hash.startsWith("#/intraday");
  go(onIntraday ? `#/intraday/${t}` : `#/t/${t}/levels`);
});
document.getElementById("search-input").addEventListener("input", (e) => e.target.setCustomValidity(""));

let seq = 0;
async function route() {
  const my = ++seq;
  disposeAll();
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const [page = "overview", a, b] = parts;
  const navKey = { t: "ticker" }[page] || page;
  document.querySelectorAll(".nav a").forEach((el) => {
    if (el.dataset.nav === navKey) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current");
  });
  fill(app, loading());
  const alive = () => my === seq;
  try {
    if (!state.meta) await refreshMeta();
    if (page === "overview") await renderOverview(app, alive);
    else if (page === "t") {
      const t = a ? a.toUpperCase() : pref("lastTicker", tickers()[0] || "NVDA");
      if (!a) { go(`#/t/${t}/levels`); return; }
      setPref("lastTicker", t);
      await renderTicker(app, t, b || "levels", alive);
    } else if (page === "intraday") {
      const focus = state.meta.settings?.focus || [];
      const t = a ? a.toUpperCase() : (focus[0] || pref("lastTicker", "NVDA"));
      if (!a) { go(`#/intraday/${t}`); return; }
      await renderIntraday(app, t, alive);
    } else if (page === "ledger") await renderLedger(app, alive);
    else if (page === "results") await renderResults(app, a, alive);
    else if (page === "method") await renderMethod(app, alive);
    else if (page === "help") await renderHelp(app, a, alive);
    else fill(app, h("div", { class: "empty" }, "没有这个页面。"), h("p", { class: "c" }, h("a", { href: "#/" }, "回总览")));
  } catch (e) {
    console.error(e);
    if (alive()) fill(app, errorBox(e));
  }
  if (alive()) app.focus({ preventScroll: true });
}

window.addEventListener("hashchange", route);
const metaTimer = setInterval(() => { refreshMeta().catch(() => {}); }, 60000);
route();
