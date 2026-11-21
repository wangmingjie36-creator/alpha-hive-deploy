// 接货页「此刻浮动」（v0.45.801）：账本持仓 × 现取 CBOE 报价 → 每条腿 / 每笔持股此刻的浮动、此刻净值。数都由 /api/wheel/live 算好，这里只排版。
// 只显示：不写账本、不改任何开仓 / 结算决定（账本仍只在每日扫描收盘后盯市）。
// 续刷：盘中每 `refresh_sec` 秒、其余时间每 15 分钟问一次本机服务；真正下不下载 CBOE 文件由服务端缓存决定。
import { api, h, num, money, isNum, panel, table, fill } from "../lib.js";

const sMoney = (v, dp = 0) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}$${num(Math.abs(v), dp)}` : "—");
const sPct = (v, dp = 2) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), dp)}%` : "—");
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const kpi = (k, v, cls = "", sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: `v ${cls}` }, v),
  sub ? h("div", { class: "small muted" }, sub) : null);
const IDLE_SEC = 900;
const AUTO_KEY = "wheel.live.auto";
const etTime = (iso) => (iso ? String(iso).slice(11, 19) : "—");

function readAuto() { try { return localStorage.getItem(AUTO_KEY) !== "0"; } catch { return true; } }
function saveAuto(v) { try { localStorage.setItem(AUTO_KEY, v ? "1" : "0"); } catch { /* 无痕窗口等：只是不记 */ } }

export function renderWheelLive(host, routeAlive) {
  const alive = () => routeAlive() && host.isConnected;
  let last = null, timer = null, seq = 0, busy = false, err = null, auto = readAuto();

  async function load(force) {
    const mine = ++seq;
    clearTimeout(timer);
    busy = true; draw();
    try {
      const r = await api(`/api/wheel/live${force ? "?force=1" : ""}`);
      if (!alive() || mine !== seq) return;
      last = r; err = null;
    } catch (e) {
      if (!alive() || mine !== seq) return;
      err = e.message;
    }
    busy = false; draw(); again();
  }

  function again() {
    clearTimeout(timer);
    if (!auto) return;
    const sec = last?.session?.live && isNum(last?.refresh_sec) ? last.refresh_sec : IDLE_SEC;
    timer = setTimeout(() => { if (alive()) load(false); }, sec * 1000);
  }

  function controls() {
    return h("div", { class: "controls" },
      h("button", { class: "btn", type: "button", "aria-pressed": String(auto),
        onclick: () => { auto = !auto; saveAuto(auto); draw(); if (auto) load(false); else clearTimeout(timer); } },
      auto ? `自动刷新：开（盘中每 ${num((last?.refresh_sec ?? 120) / 60, 0)} 分钟）` : "自动刷新：关"),
      h("button", { class: "btn primary", type: "button", disabled: busy, onclick: () => load(true) }, busy ? "刷新中…（每只票约几秒）" : "立即刷新"));
  }

  function statusLine(r) {
    const s = r.session || {};
    const bits = [s.live ? "盘中" : `不在交易时段（${s.trading_day ? "今天已收盘或未开盘" : "今天不是交易日"}）：报价是最近一次收盘的`];
    for (const [tk, f] of Object.entries(r.fetched || {})) {
      const q = (r.quotes || {})[tk] || {};
      const src = f.source === "yahoo_quote" ? "Yahoo 报价（只有标的价）" : "CBOE 延迟报价";
      bits.push(`${tk}：${src}，取于 ${etTime(f.fetched_at)} ET${q.payload_last_trade_time ? `，文件里最后成交 ${String(q.payload_last_trade_time).slice(11, 16)}` : ""}`);
    }
    return h("p", { class: "small muted" }, bits.join(" · "));
  }

  function legRow(r, key) {
    const L = r[key];
    return {
      ticker: r.ticker, what: `${key} ${num(L.strike, 2)} · ${L.expiry}`, contracts: r.contracts,
      basis: num(L.premium, 2), close: L.close_mark, closeDate: L.close_mark_date,
      now: L.mid, quote: isNum(L.bid) && isNum(L.ask) ? `${num(L.bid, 2)} / ${num(L.ask, 2)}` : null,
      reason: L.reason, unreal: L.unrealized_usd, since: L.since_close_usd,
    };
  }

  function rowsOf(r) {
    const out = [];
    for (const x of r.rows || []) {
      if (x.put) out.push(legRow(x, "put"));
      if (x.call) out.push(legRow(x, "call"));
      if (x.shares) {
        const S = x.shares;
        out.push({ ticker: x.ticker, what: `持股 ${S.n} 股`, contracts: null, basis: num(S.assigned_price, 2),
          close: S.close_price, closeDate: null, now: S.price, quote: null, reason: S.reason,
          unreal: S.unrealized_usd, since: S.since_close_usd });
      }
    }
    return out;
  }

  function draw() {
    if (!alive()) return;
    const sub = "账本里的持仓 × 现取报价，按账本同一口径（mid 盯市）重算；只显示，不写账本";
    if (!last) {
      fill(host, panel("此刻浮动", { sub }, controls(), h("div", { class: err ? "empty err" : "empty" }, err ? `取不到：${err}` : "正在取报价…（每只有持仓的票下载一份 CBOE 文件）")));
      return;
    }
    const r = last;
    if (!r.available) {
      fill(host, panel("此刻浮动", { sub }, controls(), h("div", { class: "empty" }, `算不出来：${r.state_reason || "账本不可得"}`)));
      return;
    }
    const warns = [];
    if (err) warns.push(`最近一次没问到本机服务（${err}），下面是上一次的结果。`);
    for (const [tk, q] of Object.entries(r.quotes || {})) {
      if (q && !q.available) warns.push(`${tk} 报价没取到：${(r.quote_reasons || {})[q.reason] || q.reason || "未知"}${q.vintage_date ? `（文件日期 ${q.vintage_date}）` : ""}——这只票的行算不出来。`);
    }
    if ((r.missing || []).length) warns.push(`${r.missing.join("、")} 此刻算不出来，所以合计净值不给（不拿收盘 mark 顶上）。`);
    const cols = [
      { label: "标的", render: (x) => h("b", {}, x.ticker) },
      { label: "持仓", render: (x) => x.what },
      { label: "权利金 / 成本", cls: "r", render: (x) => x.basis },
      { label: "收盘 mark", cls: "r", render: (x) => h("span", {}, isNum(x.close) ? num(x.close, 2) : "—", x.closeDate ? h("span", { class: "sub" }, ` ${x.closeDate}`) : null) },
      { label: "此刻", cls: "r", render: (x) => (x.reason
        ? h("span", { class: "chip warn" }, (r.reasons || {})[x.reason] || x.reason)
        : h("span", {}, num(x.now, 2), x.quote ? h("div", { class: "sub" }, `bid / ask ${x.quote}`) : null)) },
      { label: "此刻浮动", cls: "r", render: (x) => h("b", { class: tone(x.unreal) }, sMoney(x.unreal)) },
      { label: "较收盘变动", cls: "r", render: (x) => h("span", { class: tone(x.since) }, sMoney(x.since)) },
    ];
    const rows = rowsOf(r);
    fill(host, panel("此刻浮动", { sub }, controls(), statusLine(r),
      warns.map((w) => h("p", { class: "callout warn" }, w)),
      h("div", { class: "kpis" },
        kpi("此刻净值", money(r.nav, 0), "", `账本 ${r.as_of || "—"} 收盘 ${money(r.close_nav, 0)}`),
        kpi("此刻收益", sPct(r.return_pct, 2), tone(r.return_pct), `起始 ${money(r.starting_capital, 0)}`),
        kpi("较收盘变动", sMoney(r.since_close_usd), tone(r.since_close_usd)),
        kpi("持仓合计浮动", sMoney(r.unrealized_usd), tone(r.unrealized_usd), "卖出腿 =（权利金 − 此刻 mid）× 100 × 张数")),
      rows.length ? table(cols, rows) : h("div", { class: "empty" }, "现在没有持仓。"),
      h("p", { class: "note small muted" }, "CBOE 的文件是延迟报价（约 15 分钟），服务端最多每 90 秒换一份；mid 判据与账本盯市相同（bid > 0 且 ask ≥ bid）。"
        + "卖出的 put / call：mid 跌 = 赚、涨 = 亏。")));
  }

  draw();
  load(false);
}
