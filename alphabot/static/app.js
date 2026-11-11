// Alpha Bot 前端入口：hash 路由 + 顶栏（搜索 / 交易时段）+ 各页面。
import { api, h, errorBox, loading, pref, setPref, fill } from "./lib.js";
import { disposeAll } from "./charts.js";
import { renderOverview } from "./pages/overview.js";
import { renderTicker } from "./pages/ticker.js";
import { renderIntraday } from "./pages/intraday.js";
import { renderLedger } from "./pages/ledger.js";
import { renderStraddle } from "./pages/straddle.js";
import { renderHedge } from "./pages/hedge.js";
import { renderWheel } from "./pages/wheel.js";
import { renderCompare } from "./pages/compare.js";
import { renderWheelScreen } from "./pages/wheel_screen.js";
import { renderResults } from "./pages/results.js";
import { renderMethod } from "./pages/method.js";
import { renderHelp } from "./pages/help.js";
import { initChat } from "./chat.js";

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
  // v0.45.713：启动时代理探活失败 ⇒ yfinance 直连（常被 Yahoo 限流），横幅讲明原因与修法
  const ob = state.meta.outbound;
  const obBanner = document.getElementById("outbound-banner");
  obBanner.hidden = !(ob && ob.mode === "unchanged");
  obBanner.textContent = obBanner.hidden ? "" : `未走代理 · ${ob.detail}`;
  // v0.45.726：磁盘上的代码比正在跑的新 ⇒ 横幅（生产代码目录每个扫描日自动快进；没重启的进程新旧混用）。
  // v0.45.730：服务端根本没报 code ⇒ 它比这个页面旧（726 前起的进程）——页面文件每次现读磁盘、磁盘只往前走，
  // 页面认得 code 而服务不报只可能是服务旧 ⇒ 同样挂横幅（此前这里只在页脚写「版本未知」，偏偏是该重启的时候）
  const serverOlder = !Object.prototype.hasOwnProperty.call(state.meta, "code");
  const cv = state.meta.code;
  const cvBanner = document.getElementById("code-banner");
  cvBanner.hidden = !(serverOlder || (cv && cv.stale === true));
  cvBanner.textContent = cvBanner.hidden ? "" : (serverOlder ? OLDER_SERVER_BANNER : codeBannerText(cv));
  cvBanner.title = cvBanner.hidden || serverOlder ? "" : (cv.on_disk_subject || "");
  const dl = document.getElementById("ticker-list");
  fill(dl, ...[...(state.meta.watchlist?.tickers || []), ...Object.values(state.meta.watchlist?.index_labels || {})]
    .map((t) => h("option", { value: t })));
  fill(document.getElementById("foot"),
    h("span", {}, `Alpha Bot ${state.meta.version} · `, codeLabel(cv),
      " · 本机运行，只读卖权账本、财报跨式账本与对冲账本 · 以下为公开信息研究与情景推演，不构成投资建议。"),
    state.meta.can_shutdown ? h("button", { class: "link-btn", type: "button", onclick: stopServer }, "停止服务") : null,
  );
  return state.meta;
}

// 页脚的代码版本（v0.45.726）：正在跑的提交 + 启动时刻；判不了照实写——判不了 ≠ 没更新
function codeLabel(cv) {
  if (!cv) return "代码版本：服务端没报（它比这个页面旧，见顶部横幅）";
  if (!cv.running) return h("span", { title: cv.reason || "" }, "代码版本判不了");
  const t = new Date(cv.started_at || "");
  const when = isNaN(t) ? "" : `，${t.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })} 启动`;
  return h("span", { title: cv.running_subject || "" }, `代码 ${cv.running}${when}`,
    cv.available ? null : `（有没有更新判不了：${cv.reason || "原因未知"}）`);
}

// 没重启的服务是新旧混用：页面文件刷新就是新的、部分功能第一次用到时才载入（也是新的），其余仍是启动时的旧代码
// （v0.45.730 更正：726 的横幅说新代码要重启后才生效，实际一部分新代码已经在用）
const RESTART_HOWTO = "新旧混用可能出错——请重启：页脚「停止服务」后，再双击 Alpha Bot.app 或重新运行启动命令";
const OLDER_SERVER_BANNER = "服务比页面旧 · 这个 Alpha Bot 进程是在代码更新之前启动的（还不会报代码版本），"
  + `页面已是新的、服务还是旧的，${RESTART_HOWTO}`;

function codeBannerText(cv) {
  const n = Number.isInteger(cv.behind) && cv.behind > 0 ? `新了 ${cv.behind} 个提交` : "与正在运行的不同";
  return `代码已更新 · 磁盘上的代码${n}（运行中 ${cv.running} → 磁盘 ${cv.on_disk}）。`
    + `服务还在跑启动时的代码，刷新后的页面和部分功能会读到新代码，${RESTART_HOWTO}`;
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
    h("p", {}, "可以关掉这个页面。再次使用：双击 Alpha Bot.app，或在终端运行 make -C ~/alpha-hive-prod alphabot。")));
  fill(document.getElementById("foot"));
  fill(document.getElementById("session"));
}

export function tickers() { return state.meta?.watchlist?.tickers || []; }
// 代码规范化：指数的展示名（SPX / ^SPX / $SPX）→ CBOE 文件名；映射表只取 /api/meta 的 index_labels（同服务端 INDEX_ALIASES）。
// 搜索框和路由都经它：#/t/SPX 与 #/t/_SPX 是同一页，侧栏高亮 / 自选判断只认一种写法
export function canonTicker(raw) {
  const s = String(raw || "").trim().toUpperCase();
  const byLabel = Object.fromEntries(Object.entries(state.meta?.watchlist?.index_labels || {}).map(([k, v]) => [v, k]));
  return byLabel[s.replace(/^[\^$]/, "")] || s;
}
export function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }
// 规范化重定向（缺代码 / 小写 / 指数展示名 → 规范写法）：替换当前历史项而不是新增一项，
// 否则「后退」会回到别名地址、又被重定向回来，退不出去（二次检查 #1）
function redirect(hash) { if (location.hash !== hash) location.replace(hash); else route(); }

document.getElementById("search").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("search-input");
  const t = canonTicker(input.value);
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
      const t = a ? canonTicker(a) : pref("lastTicker", tickers()[0] || "NVDA");
      if (!a || t !== a) { redirect(`#/t/${encodeURIComponent(t)}/${b || "levels"}`); return; }
      setPref("lastTicker", t);
      await renderTicker(app, t, b || "levels", alive);
    } else if (page === "intraday") {
      const focus = state.meta.settings?.focus || [];
      const t = a ? canonTicker(a) : (focus[0] || pref("lastTicker", "NVDA"));
      if (!a || t !== a) { redirect(`#/intraday/${encodeURIComponent(t)}`); return; }
      await renderIntraday(app, t, alive);
    } else if (page === "ledger") await renderLedger(app, alive);
    else if (page === "straddle") await renderStraddle(app, alive);
    else if (page === "hedge") await renderHedge(app, alive);
    else if (page === "wheel") await (a === "screen" ? renderWheelScreen(app, alive) : renderWheel(app, alive));
    else if (page === "compare") await renderCompare(app, a ? a.toUpperCase() : null, alive);
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
initChat();
route();
