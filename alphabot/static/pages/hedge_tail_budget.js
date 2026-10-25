// 对冲页的「组合尾部预算」面板（v0.45.649）：压力最差格 + 逐标的 / 板块贡献 + 卖跨式影子闸记录。全部只读、只观测。
// 数字全由服务端算好（tail_budget.py → 审计文件的 tail 一节 → alphabot/hedge.py::tail_overview），这里只排版。
import { h, num, money, panel, table, isNum } from "../lib.js";
import { lines, C } from "../charts.js";

const sMoney = (v, dp = 0) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}$${num(Math.abs(v), dp)}` : "—");
// 四舍五入成 0 的数不带符号（"−0.0%" 读起来像「变差了」，其实是「没变」）
const sPct = (v, dp = 1) => {
  if (!isNum(v)) return "—";
  const shown = num(Math.abs(v), dp);
  return `${Number(shown.replace(/,/g, "")) === 0 ? "" : v > 0 ? "+" : "−"}${shown}%`;
};
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const cellName = (c) => (c ? `现货 ${c.spot_pct > 0 ? "+" : ""}${c.spot_pct}% · IV ${c.iv_pts > 0 ? "+" : ""}${c.iv_pts} 点` : "—");
const KIND = { option: "跨式腿", stock: "股票", hedge: "SPY 覆盖", delta_hedge: "逐票对冲" };
const TITLE = "组合尾部预算（压力最差格的来源）";

const kpi = (k, v, sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
  sub ? h("div", { class: "small muted" }, sub) : null);

function kinds(by) {
  return Object.entries(by || {}).filter(([, v]) => isNum(v?.at_worst) && Math.abs(v.at_worst) >= 0.5)
    .map(([k, v]) => `${KIND[k] || k} ${sMoney(v.at_worst)}`).join(" · ") || "—";
}

function tickerTable(rows) {
  return table([
    { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: "板块", render: (r) => r.sector },
    { label: "最差格贡献", cls: "r", render: (r) => h("span", { class: tone(r.at_worst) }, sMoney(r.at_worst), h("span", { class: "sub" }, sPct(r.at_worst_pct))) },
    { label: "下行 / 平盘格", cls: "r", render: (r) => h("span", { class: tone(r.at_downside) }, sPct(r.at_downside_pct)) },
    { label: "其中（最差格）", render: (r) => h("span", { class: "small muted" }, kinds(r.by_kind)) },
  ], rows);
}

function sectorTable(rows) {
  return table([
    { label: "板块", render: (r) => h("b", {}, r.sector) },
    { label: "标的", render: (r) => h("span", { class: "small" }, (r.tickers || []).join("、")) },
    { label: "最差格贡献", cls: "r", render: (r) => h("span", { class: tone(r.at_worst) }, sMoney(r.at_worst), h("span", { class: "sub" }, sPct(r.at_worst_pct))) },
  ], rows);
}

function gateTable(T) {
  const budgets = T.budgets_pct || [];
  const cols = [
    { label: "日期", render: (r) => r.date },
    { label: "标的", render: (r) => h("b", {}, r.ticker), },
    { label: "方向", render: (r) => (r.side === "short" ? "卖出" : r.side === "long" ? "买入" : r.side || "—") },
    { label: "加上它之后最差格（% NAV）", cls: "r", render: (r) => (r.evaluable
      ? h("span", {}, `${num(r.loss_before_pct, 1)}% → `, h("b", {}, `${num(r.loss_after_pct, 1)}%`), h("span", { class: "sub" }, `边际 ${sPct(r.marginal_pct)}`))
      : h("span", { class: "small muted" }, r.reason_zh || "没法试算")) },
    ...budgets.map((b) => ({ label: `预算 ${b}%`, render: (r) => {
      if (!r.evaluable) return "—";
      if (!r.applies) return h("span", { class: "small muted" }, "不适用（买入）");
      return r.would_skip?.[b] ? h("span", { class: "chip warn" }, "会跳过") : h("span", { class: "chip" }, "放行");
    } })),
  ];
  return table(cols, T.gate_log);
}

export function tailBudgetPanel(T) {
  const node = h("div");
  const L = T?.latest;
  if (!L) {
    node.append(panel(TITLE, { sub: "只读观测" }, h("div", { class: "empty" },
      `还没有带尾部预算的审计记录${T?.n_audits_without_tail ? `（现有 ${T.n_audits_without_tail} 份审计是该功能上线之前的，没有这一节）` : ""}；下一个扫描日起自动出现。`)));
    return node;
  }
  const chartEl = h("div", { class: "chart short" });
  const breach = isNum(L.loss_pct_nav) && isNum(L.alert_pct) && L.loss_pct_nav > L.alert_pct;
  const series = (T.series || []).filter((s) => isNum(s.loss_pct_nav));
  const budgets = T.budgets_pct || [];
  node.append(panel(TITLE, { sub: `审计日 ${L.date} · NAV ${money(L.nav, 0)} · 只读观测，不改任何交易行为` },
    h("p", { class: "lede" },
      "组合的压力最差格几乎全部来自卖跨式（净空 Gamma / Vega，双向都亏）。这里把最差格拆到每个标的和板块，并记录「每笔新开的卖跨式，加上它之后会不会超过预算」。",
      h("b", {}, "预算与告警线是主观常数、没有证据说该是多少；影子闸只记录、不真跳过。")),
    L.partial ? h("p", { class: "callout warn" }, `压力表有行被剔除（${(L.excluded || []).join("；") || "—"}），实际最差格损失可能比这里更大。`) : null,
    h("div", { class: "kpis" },
      kpi("全组合最差格", h("span", { class: "down" }, sPct(-L.loss_pct_nav, 1)), `${cellName(L.worst)} · ${sMoney(L.worst?.pnl)}`),
      kpi("告警线", `${num(L.alert_pct, 0)}% NAV`, breach ? "已超线（每个扫描日 P2）" : "线内"),
      kpi("下行 / 平盘侧最差格", h("span", { class: "down" }, sPct(L.downside_worst?.pct_nav, 1)), cellName(L.downside_worst)),
      kpi("账目核对", `残差 ${sMoney(L.residual_usd, 2)}`, "各标的贡献之和与全组合最差格的差")),
    series.length > 1 ? chartEl : null,
    h("h3", { class: "small" }, "逐标的贡献（最差格；负 = 亏）"), tickerTable(L.by_ticker || []),
    h("div", { style: { height: "12px" } }),
    h("h3", { class: "small" }, "按板块"), sectorTable(L.by_sector || []),
    h("div", { style: { height: "12px" } }),
    h("h3", { class: "small" }, `影子闸记录（预算 ${budgets.map((b) => `${b}%`).join(" / ") || "—"} NAV；${T.n_gate_entries || 0} 笔）`),
    (T.gate_log || []).length ? gateTable(T) : h("div", { class: "empty" }, "上线以来还没有新开的跨式（只在入场当天评估）。"),
    h("p", { class: "small muted" }, T.gate_basis ? `口径：${T.gate_basis}。` : "",
      "不是反事实：它回答「给定当天实际已有的账，这一笔会不会被挡」，不模拟「前一笔被挡后后一笔就放得进来」。"
      + "评估用入场当天的收盘标记价，不是入场那一刻的报价；那天审计没跑成就没有记录。")));
  node._after = () => {
    if (series.length <= 1) return null;
    const c = C();
    return lines(chartEl, { x: series.map((s) => s.date.slice(5)), yFmt: (v) => `${num(v, 0)}%`, yMin: 0,
      series: [{ name: "最差格损失（% NAV）", data: series.map((s) => s.loss_pct_nav), color: c.neg, width: 2, symbol: series.length < 20 }],
      hmarks: isNum(L.alert_pct) ? [{ value: L.alert_pct, label: `告警线 ${num(L.alert_pct, 0)}%`, color: c.ink2 }] : [] });
  };
  return node;
}
