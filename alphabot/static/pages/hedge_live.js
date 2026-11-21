// 对冲页「此刻浮动」（v0.45.800）：账本里的持仓 × 现取报价 → 此刻的浮动盈亏 / 净值。数都由 /api/hedge/live 算好，这里只排版。
// 只显示：不写账本、不进任何对冲决定（决定仍只在扫描日收盘后做）。
// 续刷：盘中每 `refresh_sec` 秒、其余时间每 5 分钟问一次本机服务；真正打不打 Yahoo 由服务端缓存 / 失败冷却决定（请求量的硬上限在那边）。
import { api, h, num, money, isNum, panel, table, fill } from "../lib.js";

const sMoney = (v, dp = 2) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}$${num(Math.abs(v), dp)}` : "—");
const sPct = (v, dp = 2) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), dp)}%` : "—");
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const kpi = (k, v, cls = "", sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: `v ${cls}` }, v),
  sub ? h("div", { class: "small muted" }, sub) : null);
const STATE_ZH = { REGULAR: "盘中", PRE: "盘前", PREPRE: "盘前", POST: "盘后", POSTPOST: "已收盘", CLOSED: "已收盘" };
const IDLE_SEC = 300;
const AUTO_KEY = "hedge.live.auto";
const etTime = (iso) => (iso ? String(iso).slice(11, 19) : "—");
const qTime = (ts) => (isNum(ts) ? new Date(ts * 1000).toLocaleTimeString("zh-CN", { timeZone: "America/New_York", hour12: false }) : "—");

function readAuto() { try { return localStorage.getItem(AUTO_KEY) !== "0"; } catch { return true; } }
function saveAuto(v) { try { localStorage.setItem(AUTO_KEY, v ? "1" : "0"); } catch { /* 无痕窗口等：只是不记 */ } }

export function renderHedgeLive(host, routeAlive) {
  const alive = () => routeAlive() && host.isConnected;
  let last = null, timer = null, seq = 0, busy = false, err = null, auto = readAuto();

  async function load(force) {
    const mine = ++seq;
    clearTimeout(timer);
    busy = true; draw();
    try {
      const r = await api(`/api/hedge/live${force ? "?force=1" : ""}`);
      if (!alive() || mine !== seq) return;
      last = r; err = null;
    } catch (e) {
      if (!alive() || mine !== seq) return;
      err = e.message;                             // 一次没问到（本机服务重启等）不该让续刷从此停掉
    }
    busy = false; draw(); again();
  }

  function again() {
    clearTimeout(timer);
    if (!auto) return;
    const live = last?.session?.live;
    const sec = live && isNum(last?.refresh_sec) ? last.refresh_sec : IDLE_SEC;
    timer = setTimeout(() => { if (alive()) load(false); }, sec * 1000);
  }

  function controls() {
    return h("div", { class: "controls" },
      h("button", { class: "btn", type: "button", "aria-pressed": String(auto),
        onclick: () => { auto = !auto; saveAuto(auto); draw(); if (auto) load(false); else clearTimeout(timer); } },
      auto ? `自动刷新：开（盘中每 ${num(last?.refresh_sec ?? 60, 0)} 秒）` : "自动刷新：关"),
      h("button", { class: "btn primary", type: "button", disabled: busy, onclick: () => load(true) }, busy ? "刷新中…" : "立即刷新"));
  }

  function statusLine(r) {
    const q = r.quotes || {}, s = r.session || {};
    const row = r.spy?.row || (r.delta?.rows || [])[0];
    const bits = [];
    bits.push(s.live ? "盘中" : `不在交易时段（${s.trading_day ? "今天已收盘或未开盘" : "今天不是交易日"}）：报价是最近一次收盘`);
    if (q.fetched_at) bits.push(`报价取于 ${etTime(q.fetched_at)} ET（${q.source === "demo" ? "演示合成" : "Yahoo 报价接口"}${isNum(q.age_sec) && q.age_sec > 0 ? `，${num(q.age_sec, 0)} 秒前` : ""}）`);
    if (row && isNum(row.quote_time)) bits.push(`最后成交 ${qTime(row.quote_time)} ET${row.market_state ? `（${STATE_ZH[row.market_state] || row.market_state}）` : ""}`);
    if (row && isNum(row.delay_min)) bits.push(row.delay_min > 0 ? `交易所延迟 ${num(row.delay_min, 0)} 分钟` : "实时");
    return h("p", { class: "small muted" }, bits.join(" · "));
  }

  function spyBlock(r) {
    const S = r.spy || {}, row = S.row;
    if (S.flat) return h("div", { class: "empty" }, "SPY 覆盖账空仓：没有浮动。");
    if (!row) return h("div", { class: "empty" }, r.errors?.positions ? `持仓读不了：${r.errors.positions}` : "SPY 覆盖账还没有持仓记录。");
    const why = row.reason ? (r.reasons || {})[row.reason] || row.reason : null;
    const md = S.mark_date || "—";
    return [
      why ? h("p", { class: "callout warn" }, `${row.ticker} 此刻算不出来：${why}${isNum(row.price) ? `（报价 ${money(row.price)}）` : ""}。下面只剩账本 ${md} 收盘的数，没拿它当此刻。`) : null,
      h("div", { class: "kpis" },
        kpi(`${row.ticker} 现价`, money(row.price), tone(row.since_mark_pct), isNum(row.since_mark_pct) ? `较 ${md} 收盘标记 ${sPct(row.since_mark_pct)}` : `${md} 收盘标记 ${money(row.mark)}`),
        kpi("持有", `${row.shares > 0 ? "+" : row.shares < 0 ? "−" : ""}${num(Math.abs(row.shares ?? 0), 0)} 股`, "", `均价 ${money(row.avg_price)}`),
        kpi("此刻浮动", sMoney(S.unrealized_usd), tone(S.unrealized_usd), `${md} 收盘时 ${sMoney(S.close_unrealized_usd)}`),
        kpi(`较 ${md} 收盘变动`, sMoney(S.since_mark_usd), tone(S.since_mark_usd), "持仓自那次收盘后没动过"),
        kpi("此刻累计盈亏（= 净值）", sMoney(S.nav), tone(S.nav), `${md} 收盘时 ${sMoney(S.close_nav)} · 已实现 ${sMoney(S.realized_usd)}`)),
    ];
  }

  function deltaBlock(r) {
    const D = r.delta || {};
    if (!D.opened || !(D.rows || []).length) return null;
    const cols = [
      { label: "标的", render: (x) => h("b", {}, x.ticker) },
      { label: "持股", cls: "r", render: (x) => num(x.shares, 0) },
      { label: "均价", cls: "r", render: (x) => money(x.avg_price) },
      { label: "收盘标记", cls: "r", render: (x) => h("span", {}, money(x.mark), h("span", { class: "sub" }, x.mark_date || "")) },
      { label: "现价", cls: "r", render: (x) => (x.reason ? h("span", { class: "chip warn" }, (r.reasons || {})[x.reason] || x.reason) : money(x.price)) },
      { label: "此刻浮动", cls: "r", render: (x) => h("span", { class: tone(x.unrealized_usd) }, sMoney(x.unrealized_usd)) },
      { label: "较收盘变动", cls: "r", render: (x) => h("span", { class: tone(x.since_mark_usd) }, sMoney(x.since_mark_usd)) },
    ];
    return [h("h3", {}, "逐票对冲账"),
      h("p", { class: "small muted" }, `合计此刻浮动 ${sMoney(D.unrealized_usd)} · 较收盘变动 ${sMoney(D.since_mark_usd)} · 此刻净值 ${sMoney(D.nav)}`
        + (D.unrealized_usd == null ? "（有一只算不出来，合计不给）" : "")),
      table(cols, D.rows)];
  }

  function draw() {
    if (!alive()) return;
    const sub = "账本里的持仓 × 现取报价；只显示，不写账本、不进对冲决定（决定仍只在扫描日收盘后做）";
    if (!last) {
      fill(host, panel("此刻浮动", { sub }, controls(), h("div", { class: err ? "empty err" : "empty" }, err ? `取不到：${err}` : "正在取报价…")));
      return;
    }
    const r = last;
    if (!r.available) {
      fill(host, panel("此刻浮动", { sub }, controls(), h("div", { class: "empty" }, r.reason === "no_ledger" ? "没找到对冲账本，算不了此刻浮动。" : `算不出来：${r.reason || "未知"}`)));
      return;
    }
    const f = r.quotes?.refresh_failed;
    const warns = [];
    if (err) warns.push(`最近一次没问到本机服务（${err}），下面是上一次的结果。`);
    if (f) warns.push(`报价刷新失败：${f.reason_zh || f.reason}${isNum(f.retry_after_sec) ? `，约 ${num(Math.max(f.retry_after_sec, 1), 0)} 秒后再试` : ""}`
      + (r.quotes?.fetched_at ? `；沿用 ${etTime(r.quotes.fetched_at)} ET 取到的那一份。` : "；手里没有任何报价。"));
    fill(host, panel("此刻浮动", { sub }, controls(), statusLine(r),
      warns.map((w) => h("p", { class: "callout warn" }, w)),
      spyBlock(r), deltaBlock(r),
      h("p", { class: "note small muted" }, "「此刻浮动」=（现价 − 均价）× 股数；「较收盘变动」=（现价 − 账本收盘标记价）× 股数。"
        + "报价离收盘标记价超过 " + num(r.implausible_pct, 0) + "% 当坏报价不用；盘中报价不是今天这一场的也不用——算不出来就写算不出来，不拿收盘价顶。")));
  }

  draw();
  load(false);
}
