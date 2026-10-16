// 卖权页「怎么卖」：选中结构的下单腿 / 推荐价位 / 标的价位 / 步骤与风险。
// 全部数字由服务端 alphabot/howto.py 算好（同一份 structure_quote 报价），这里只排版，不另写公式。
import { h, num, money, pct, signedPct, isNum, panel, table, seg, STRUCT_LABEL, plain, pref, setPref } from "../lib.js";

const ACTION = { sell: "卖出开仓", buy: "买入开仓" };
const CP = { P: "put", C: "call" };
const KIND_ORDER = { wing_strike: 0, em_edge: 1, breakeven: 2, short_strike: 3, zero_gamma: 4, major: 5 };

const list = (tag, items) => h(tag, {}, (items || []).map((x) => h("li", {}, plain(x))));
const perShare = (x) => (isNum(x) ? `${money(x)} / 股` : "—");
const perContract = (x) => (isNum(x) ? `${money(x, 0)} / 张` : "—");

function legsTable(tier, S) {
  const cols = [
    { label: "动作", render: (l) => h("b", {}, ACTION[l.action] || l.action) },
    { label: "合约", render: (l) => h("span", {}, `${CP[l.cp] || l.cp} ${num(l.strike)}`, h("span", { class: "sub" }, l.symbol || "")) },
    { label: "距现价", cls: "r", render: (l) => signedPct(l.dist_pct, 1) },
    { label: "|Δ|", cls: "r", render: (l) => num(Math.abs(l.delta ?? NaN), 3) },
    { label: "σ 距离", cls: "r", render: (l) => num(Math.abs(l.sigma_distance ?? NaN), 2) },
    { label: "P(ITM)", cls: "r", render: (l) => pct(l.itm_prob) },
    { label: "bid", cls: "r", render: (l) => money(l.bid) },
    { label: "ask", cls: "r", render: (l) => money(l.ask) },
    { label: "中价", cls: "r", render: (l) => h("span", {}, money(l.mid), h("span", { class: "sub" }, `点差 ${pct(l.spread_pct, 0)}`)) },
  ];
  return table(cols, tier.legs || []);
}

function priceTable(tier, tenor, R) {
  const L = tier.limit, X = tier.exit, P = tier.payout;
  const rows = [
    ["起挂限价（净信用）", h("b", {}, perShare(L.start)),
      `${perContract(L.start_per_contract)} · 单笔回报 ${pct(L.yield_start, 2)}。组合中价：卖腿 mid 之和 − 买腿 mid 之和，限价单、当日有效。`],
    ["底线价（不低于它）", h("b", {}, perShare(L.floor)),
      `${perContract(L.floor_per_contract)} · 单笔回报 ${pct(L.yield_floor, 2)}。自然价与中价之间 ${Math.round((R.floor_share || 0) * 100)}% 处；从起挂价逐步让价，到底线仍不成交就今天不做。`],
    ["自然价（表里的权利金）", perShare(L.natural), `${perContract(L.natural_per_contract)}。卖按 bid、买按 ask，立刻成交的最差价——别追到这里。`],
    ["止盈买回价", h("b", {}, `≤ ${perShare(X.take_profit_debit)}`),
      `按起挂价成交计：成交权利金的 ${Math.round((R.profit_take || 0) * 100)}%（${perContract(X.take_profit_per_contract)}）。成交后立刻挂 GTC 买入平仓。`],
    ["止损买回价", X.stop_debit === null || X.stop_debit === undefined ? h("span", { class: "muted" }, "不适用") : h("b", {}, `≥ ${perShare(X.stop_debit)}`),
      X.stop_debit === null || X.stop_debit === undefined ? plain(X.stop_note || "—")
        : `成交权利金的 ${num(R.stop_mult, 1)} 倍（再亏约 ${perContract(X.stop_loss_per_contract)}）。到价即整体平仓，不要「再等等」。`],
    ["管理日", X.manage ? h("b", {}, X.manage.date) : "—",
      X.manage ? `到期前 ${X.manage.dte} 天：无论盈亏，评估平仓或换仓（${tenor === "monthly" ? "月度" : "周度"}期限）。` : "到期日不可得。"],
    ["最大盈利 / 张", perContract(P.max_profit_per_contract), "按起挂价成交计；到期价外时全部留下。"],
    ["最大亏损 / 张", P.max_loss_unbounded ? h("b", {}, "无上限") : perContract(P.max_loss_per_contract),
      P.max_loss_unbounded ? "上方（call 侧）亏损没有封顶——只做能承受的张数，并严格执行止损与价位提醒。" : "最坏情形（按自然价口径）：卖 put 是标的跌到 0，价差 / 铁鹰是到期收盘越过保护腿。"],
    ["占用资金 / 张", perContract(P.collateral_per_contract), "卖 put 现金担保 K、卖 call 备兑 S、价差 / 铁鹰 = 最大亏损、宽跨只计 put 侧；券商实际保证金可能更高。"],
  ];
  return table([{ label: "项目" }, { label: "价位", cls: "r" }, { label: "怎么用" }].map((c, i) => ({ ...c, render: (r) => r[i] })), rows);
}

function levelsTable(tier, S) {
  const rows = [...(tier.levels || []).map((l) => ({ ...l })), { kind: "spot", price: S, dist_pct: 0, label: "现价" }]
    .sort((a, b) => a.price - b.price || (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9));
  return table([
    { label: "标的价位", cls: "r", render: (l) => h("b", {}, num(l.price)) },
    { label: "距现价", cls: "r", render: (l) => (l.kind === "spot" ? "—" : signedPct(l.dist_pct, 1)) },
    { label: "含义", render: (l) => plain(l.label) },
  ], rows, { rowClass: (l) => (l.kind === "spot" ? "spot-row" : "") });
}

export function howtoPanel({ hw, tenor, pick, S, redraw }) {
  if (!hw || hw.available === false) {
    return panel("怎么卖", {}, h("p", { class: "callout warn" }, `「怎么卖」不可得：${plain(hw?.reason || "服务端没有给出这一栏")}。上面的表仍然有效。`));
  }
  const guide = hw.guide?.[pick];
  const per = hw.tenors?.[tenor]?.[pick];
  if (!guide || !per) {
    return panel(`怎么卖 · ${STRUCT_LABEL[pick] || pick}`, {}, h("div", { class: "empty" }, "这个期限没有可展示的下单信息（该到期日没有候选）。"));
  }
  let which = pref("howtoTier", "route");
  if (which === "route" && !per.route) which = "base";
  const tier = per[which];
  const control = per.route && per.base
    ? seg([["route", "路由档（推荐）"], ["base", "基线档 0.20Δ"]], which, (x) => { setPref("howtoTier", x); redraw(); }, "档位")
    : null;
  const head = panel(`怎么卖 · ${guide.name}`, {
    sub: `${guide.oneline}　观点：${guide.stance}`, right: control, class: "howto",
  });
  const body = [];
  if (!tier) {
    body.push(h("p", { class: "callout warn" }, "这个结构在所选档位没有可用的腿（路由判不可用）。"));
  } else {
    (tier.cautions || []).forEach((c) => body.push(h("p", { class: `callout ${c.level === "warn" ? "warn" : ""}` }, plain(c.text))));
    if (!tier.quotable) {
      body.push(h("p", { class: "callout warn" }, `这一档不可报价：${tier.reason || "—"}。下面只列出涉及的合约，金额不可当成交价。`));
      if ((tier.legs || []).length) body.push(legsTable(tier, S));
    } else {
      body.push(h("h3", {}, "① 下单腿"), legsTable(tier, S),
        h("p", { class: "note" }, `${which === "route" ? "路由档是系统按环境路由规则选的那一档，也是这里的推荐。" : "基线档是固定 0.20Δ 的对照，不是推荐。"}多腿结构请用组合单一次报。`),
        h("h3", {}, "② 推荐价位"), priceTable(tier, tenor, hw.rules || {}),
        h("h3", {}, "③ 标的价位（设提醒用）"), levelsTable(tier, S));
    }
  }
  const steps = h("div", { class: "grid halves howto-steps" },
    h("div", {}, h("h3", {}, "④ 怎么下单"), h("p", { class: "small muted" }, "下单前"), list("ol", hw.before),
      h("p", { class: "small muted" }, `${guide.name}的具体做法`), list("ul", guide.order),
      h("p", { class: "small muted" }, "成交后"), list("ol", hw.after),
      h("h3", {}, "盈亏口径"), list("ul", guide.pnl)),
    h("div", {}, h("h3", {}, "⑤ 怎么管理"), list("ul", guide.manage),
      h("h3", {}, "⑥ 主要风险"), list("ul", guide.risks),
      h("h3", {}, "什么时候别用"), h("p", { class: "small" }, plain(guide.avoid)),
      h("p", { class: "small muted" }, `仓位：通行做法是把单笔最大可承受亏损控制在账户的 ${(hw.rules?.loss_budget_pct || []).join("–")}% 以内，`
        + "再按上表「每张最大亏损」（无上限的结构按止损线的亏损）反推张数。这是通用风控惯例，不是针对你的建议。")));
  head.append(...body, steps, h("p", { class: "note" }, plain(hw.disclaimer)));
  return head;
}
