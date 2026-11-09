// 接货页：接货型卖 put 纸面账本（XOM / WMT / NVDA；先卖 put、被行权接货后卖 covered call）。全部只读，不写账本。
// 规则冻结在 experiments/sell_put_wheel_prereg.md；它是测量账本、不是检验——页面不给「推荐」「综合分」。
// 数字（盈亏 / 基准 / 占用）全由服务端算好，前端只排版；会变的规则常量取自 /api/method 的 wheel.*（经 d.rules）。
import { api, h, helpLink, num, money, panel, table, isNum, fill } from "../lib.js";

const sMoney = (v, dp = 0) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}$${num(Math.abs(v), dp)}` : "—");
const sPct = (v, dp = 2) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), dp)}%` : "—");
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const kpi = (k, v, cls = "", sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: `v ${cls}` }, v),
  sub ? h("div", { class: "small muted" }, sub) : null);
const OUTCOME = { assigned: "被行权接货", expired: "作废" };

function legCell(leg) {
  if (!leg) return "—";
  const left = isNum(leg.days_to_expiry) ? (leg.days_to_expiry >= 0 ? `还有 ${leg.days_to_expiry} 天` : `已过期 ${-leg.days_to_expiry} 天`) : "";
  return h("span", {}, `${num(leg.strike, 2)} · ${leg.expiry}`, h("span", { class: "sub" }, ` ${left}`),
    leg.settle_deferred_since ? h("span", { class: "chip warn", title: leg.settle_deferred_reason_zh || leg.settle_deferred_reason || "" }, `结算延期（自 ${leg.settle_deferred_since}）`) : null);
}

function lotsPanel(d) {
  const cols = [
    { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: "状态", render: (r) => r.state_zh },
    { label: "张数", cls: "r", render: (r) => num(r.contracts, 0) },
    { label: "put（行权价 · 到期）", render: (r) => legCell(r.put) },
    { label: "put 权利金", cls: "r", render: (r) => (r.put ? h("span", {}, num(r.put.premium, 2),
      h("div", { class: "sub" }, `年化 ${isNum(r.put.annual_yield_pct) ? num(r.put.annual_yield_pct, 1) + "%" : "—"}`
        + (isNum(r.put.iv_rv) ? ` · IV÷RV ${num(r.put.iv_rv, 2)}` : ""))) : "—") },
    { label: "接货价", cls: "r", render: (r) => (r.put ? h("span", {}, num(r.put.accept_price, 2),
      r.put.accept_price_source === "placeholder" ? h("span", { class: "chip warn", title: "用户还没给接货价，这是开仓当天现价打折的占位" }, "占位") : null) : "—") },
    { label: "持股", cls: "r", render: (r) => (r.shares ? `${r.shares.n} 股 @ ${num(r.shares.assigned_price, 2)}` : "—") },
    { label: "call（行权价 · 到期）", render: (r) => (r.call ? h("span", {}, legCell(r.call),
      h("div", { class: "sub" }, `权利金 ${num(r.call.premium, 2)} · 年化 ${isNum(r.call.annual_yield_pct) ? num(r.call.annual_yield_pct, 1) + "%" : "—"}`
        + (isNum(r.call.iv_rv) ? ` · IV÷RV ${num(r.call.iv_rv, 2)}` : "")))
      : (r.shares && isNum(r.shares.call_trigger) ? h("span", { class: "sub" }, `收盘到 ${num(r.shares.call_trigger, 2)} 才卖`) : "—")) },
    { label: "累计权利金", cls: "r", render: (r) => money(r.premiums_collected_usd, 0) },
    { label: "持股浮动", cls: "r", render: (r) => (r.shares ? h("span", { class: tone(r.shares.unrealized_usd) }, sMoney(r.shares.unrealized_usd)) : "—") },
  ];
  return panel("当前持仓", { sub: "每个循环一条（加仓后同一只会有多条，各自到期、各自结算）；put 作废或被叫走后那一圈结束" },
    d.lots.length ? table(cols, d.lots) : h("div", { class: "empty" }, "现在没有持仓（还没开过，或这一圈刚结束、下一个交易日再开）。"));
}

function benchmarkPanel(d) {
  const cols = [
    { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: "开仓 → 到期", render: (r) => `${r.opened} → ${r.expiry}` },
    { label: "行权价 / 权利金", cls: "r", render: (r) => `${num(r.strike, 2)} / ${num(r.premium, 2)}` },
    { label: "开仓价 → 到期收盘", cls: "r", render: (r) => `${num(r.spot_at_open, 2)} → ${num(r.close_at_expiry, 2)}` },
    { label: "结果", render: (r) => OUTCOME[r.outcome] || r.outcome },
    { label: "卖 put 这一腿", cls: "r", render: (r) => h("b", { class: tone(r.wheel_put_pnl_usd) }, sMoney(r.wheel_put_pnl_usd)) },
    { label: "同价位限价买入", cls: "r", render: (r) => h("span", { class: tone(r.limit_buy_pnl_usd) },
      sMoney(r.limit_buy_pnl_usd), r.limit_buy_filled ? "" : h("span", { class: "sub" }, " 没成交")) },
    { label: "一直持有", cls: "r", render: (r) => h("span", { class: tone(r.hold_pnl_usd) }, sMoney(r.hold_pnl_usd)) },
  ];
  return panel("已结算的 put 与两条基准并排", { sub: "都在到期那一场的收盘上比，不含 covered call；不是检验，只是并排摆出来" },
    d.benchmarks.length ? table(cols, d.benchmarks) : h("div", { class: "empty" }, "还没有到期结算的 put（第一张要等到它的到期日之后）。"),
    h("p", { class: "note small muted" }, "限价买入：开仓后任一天收盘 ≤ 行权价就算成交（只有收盘价，所以是保守的成交判据）。三条线都不含手续费、不含提前行权。"));
}

function eventsPanel(d) {
  const cols = [
    { label: "日期", render: (r) => r.date }, { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: "事件", render: (r) => r.event_zh },
    { label: "行权价", cls: "r", render: (r) => num(r.strike, 2) },
    { label: "到期", render: (r) => r.expiry || "—" },
    { label: "权利金", cls: "r", render: (r) => (isNum(r.premium) ? num(r.premium, 2) : "—") },
    { label: "到期收盘", cls: "r", render: (r) => (isNum(r.close) ? num(r.close, 2) : "—") },
    { label: "一圈盈亏", cls: "r", render: (r) => (isNum(r.cycle_pnl_usd) ? h("b", { class: tone(r.cycle_pnl_usd) }, sMoney(r.cycle_pnl_usd)) : "—") },
  ];
  return panel("事件流（最近的在前）", {}, d.events.length ? table(cols, d.events) : h("div", { class: "empty" }, "还没有事件。"));
}

export async function renderWheel(root, alive) {
  const d = await api("/api/wheel");
  if (!alive()) return;
  const r = d.rules || {};
  const head = h("div", { class: "page-head" }, h("div", {},
    h("h1", {}, "接货型卖 put", h("span", { class: "sub" }, d.available ? `账本 ${d.as_of || "—"}` : "")),
    h("p", { class: "lede" }, `纸面账本（模拟成交，不连券商）。新开仓名单：${(r.universe || []).join(" / ") || "—"}（退出名单的标的，已开的那笔照常持有到结算）。先卖 put（30~50 天内、不高于接货价、IV ÷ RV ≥ 门槛的合格合约里，按账本规则「年化 × IV/RV」选一张），到期收盘低于行权价就接货，`
      + "接货后卖 covered call，被叫走一圈结束；没有止损止盈。这是测量账本，不是检验，也不给任何推荐。")),
    helpLink("wheel"));
  const tabs = h("p", { class: "small" }, h("b", {}, "账本"), "　·　", h("a", { href: "#/wheel/screen" }, "筛选：30 只标的里卖 put 好不好做 →"));
  if (!d.available && d.state_reason === "demo") {
    fill(root, head, tabs, panel(null, {}, h("div", { class: "empty" }, "演示模式不展示这本账（合成数据不是它的记录）。")));
    return;
  }
  if (!d.available) {
    fill(root, head, tabs, panel(null, {}, h("div", { class: "empty" }, "接货型卖 put 账本还没开：每日扫描跑过一次才会有（数据根里还没有 sell_put_state/）。"),
      h("p", { class: "small muted" }, `原因：${d.state_reason || "—"}。桌面程序用 --reset 重选数据根；终端运行要设 ALPHA_HIVE_HOME。`)));
    return;
  }
  const a = d.account || {};
  const warns = [];
  if ((d.lots || []).some((l) => l.put && l.put.accept_price_source === "placeholder")) {
    warns.push("接货价现在是占位（开仓当天现价打折，见规则），不是估值判断：用户还没给每只的具体接货价。");
  }
  for (const x of d.deferred || []) warns.push(`${x.ticker} 的 ${x.leg === "put" ? "put" : "call"}（到期 ${x.expiry}）还没结算：${x.reason_zh || x.reason}——拿不到到期那一场的收盘，不会拿前一天的价顶替。`);
  for (const [tk, why] of Object.entries(d.chain_errors || {})) warns.push(`${tk} 今天没取到期权链：${why}`);
  const skippedBits = (d.skipped || []).map((s) => `${s.ticker} ${s.leg === "put" ? "put" : "call"}：${s.reason_zh}${s.leg === "call" && s.detail ? `（${s.detail}）` : ""}`);
  const kpis = h("div", { class: "kpis" },
    kpi("净值", money(a.nav, 0), "", `起始 ${money(a.starting_capital, 0)}`),
    kpi("收益", sPct(a.return_pct, 2), tone(a.return_pct)),
    kpi("现金", money(a.cash, 0)), kpi("put 担保占用", money(a.reserved_puts, 0)),
    kpi("持股市值", money(a.shares_value, 0)), kpi("累计权利金", money(d.kpis?.premium_collected_usd, 0)));
  fill(root, head, tabs, ...warns.map((w) => h("p", { class: "callout warn" }, w)), kpis,
    skippedBits.length ? h("p", { class: "small muted" }, `今天没开 / 没卖的：${skippedBits.join("；")}`) : null,
    lotsPanel(d), benchmarkPanel(d), eventsPanel(d),
    h("p", { class: "note small muted" }, "成交一律按 bid（保守）、未到期的腿按 mid 盯市，所以刚开仓那天净值会比起始略低（点差）。"
      + "美式提前行权、保证金、分红、手续费都不建模。"));
}
