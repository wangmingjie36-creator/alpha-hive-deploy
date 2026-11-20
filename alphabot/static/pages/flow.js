// 标的 · 资金（v0.45.795）：个股按单笔金额分档的资金流（东方财富）+ SPY 与 11 只板块 SPDR 的申购赎回（道富）。
// 数都由 /api/money-flow 算好（口径见帮助页 #/help/flow）；合计、按周都在服务端，这里只排版、上色。
// 两半各自取、各自失败：一半取不到不影响另一半。请求量的上限在服务端（缓存 + 失败冷却），本页不定时续刷。
import { api, h, num, isNum, panel, seg, table, downloadCSV, pref, setPref, fill, loading, timeAgo, usdShort, signedPct } from "../lib.js";
import { columns } from "../charts.js";

const PREF_KEY = "flow.tier";

// 正负同时用符号与颜色表达（红绿没过色盲校验）
const signed = (v) => (isNum(v) ? h("span", { class: v > 0 ? "up" : v < 0 ? "down" : "" }, `${v > 0 ? "+" : ""}${usdShort(v)}`) : "—");
const range = (a, b) => (a === b ? a.slice(5) : `${a.slice(5)} – ${b.slice(5)}`);

export async function flow(body, d, t, routeAlive, force) {
  const alive = () => routeAlive() && body.isConnected;
  let tier = pref(PREF_KEY, "main");
  let r;
  fill(body, loading("取资金流…"));
  try {
    r = await api(`/api/money-flow/${encodeURIComponent(t)}${force ? "?refresh=1" : ""}`);
  } catch (e) {
    if (!alive()) return;
    fill(body, h("div", { class: "empty err" }, `资金页取不到：${e.message}`));
    return;
  }
  if (!alive()) return;
  const R = r.rules || {};
  const TIER = R.tiers || {};

  function stockPart() {
    const s = r.stock || {};
    if (!s.available) {
      return panel(`${t} 个股资金流`, {}, h("p", { class: "callout warn" }, `取不到：${s.reason_zh || s.reason}${s.detail ? `（${s.detail}）` : ""}`));
    }
    const g = s.range || {};
    const chartEl = h("div", { class: "chart short" });
    const days = [...(s.days || [])].reverse();             // 图按时间正序
    const draw = () => columns(chartEl, { x: days.map((x) => x.date.slice(5) + (x.partial ? "*" : "")), values: days.map((x) => x[tier]) });
    const tierSeg = seg(Object.entries(TIER), tier, (v) => { tier = v; setPref(PREF_KEY, v); draw(); }, "档位");
    const tot = s.totals || {};
    const kpi = (k, v, sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
      sub ? h("div", { class: "small muted" }, sub) : null);
    const kpis = h("div", { class: "kpis" },
      kpi("区间", range(g.start || "", g.end || ""), `${num(g.n_days, 0)} 个交易日`),
      kpi("股价", `${num(g.close_start, 2)} → ${num(g.close_end, 2)}`, signedPct(g.change_pct, 1)),
      kpi("主力（超大 + 大单）合计", signed(tot.main), `超大单 ${usdShort(tot.super)} · 大单 ${usdShort(tot.large)}`),
      kpi("中单 + 小单合计", signed(isNum(tot.medium) && isNum(tot.small) ? tot.medium + tot.small : null),
        `中单 ${usdShort(tot.medium)} · 小单 ${usdShort(tot.small)}`));
    const wkCols = [
      { label: "周", render: (w) => range(w.start, w.end) + (w.partial ? " *" : ""), csv: (w) => `${w.start}~${w.end}` },
      { label: "天数", cls: "r", render: (w) => num(w.n_days, 0), csv: (w) => w.n_days },
      ...["main", "super", "large", "medium", "small"].map((k) => ({ label: TIER[k] || k, cls: "r", render: (w) => signed(w[k]), csv: (w) => w[k] })),
      { label: "周末收盘", cls: "r", render: (w) => num(w.close_end, 2), csv: (w) => w.close_end },
    ];
    const dayCols = [
      { label: "日期", render: (x) => x.date + (x.partial ? " *" : ""), csv: (x) => x.date },
      { label: "收盘", cls: "r", render: (x) => num(x.close, 2), csv: (x) => x.close },
      { label: "涨跌", cls: "r", render: (x) => signedPct(x.pct, 2), csv: (x) => x.pct },
      ...["main", "super", "large", "medium", "small"].map((k) => ({ label: TIER[k] || k, cls: "r", render: (x) => signed(x[k]), csv: (x) => x[k] })),
    ];
    const rf = s.refresh_failed;
    const el = panel(`${t} 个股资金流`, {
      sub: `东方财富 · 取于 ${timeAgo(s.age_sec)}` + ((s.days || []).some((x) => x.partial) ? " · * = 今天还没收盘，数会变" : ""),
      right: h("button", { class: "btn", type: "button", onclick: () => downloadCSV(`${t}_资金流_${g.end || ""}.csv`, dayCols, s.days || []) }, "导出 CSV"),
    },
    rf ? h("p", { class: "callout warn" }, `刷新失败（${rf.reason_zh}），下面是 ${timeAgo(s.age_sec)} 取的那一份`) : null,
    kpis,
    h("div", { class: "controls" }, tierSeg), chartEl,
    h("h3", {}, "按周"), table(wkCols, s.weeks || [], { class: "flow" }),
    h("details", {}, h("summary", {}, "逐日"), table(dayCols, s.days || [], { class: "flow" })));
    return [el, draw];
  }

  function etfPart() {
    const e = r.etf || {};
    if (!e.available) {
      return panel("板块基金申购赎回", {}, h("p", { class: "callout warn" }, `取不到：${e.reason_zh || e.reason}${e.detail ? `（${e.detail}）` : ""}`));
    }
    const funds = e.funds || [];
    const cols = [
      { label: "周", render: (w) => {
        if (w.total) return h("b", {}, `最近 ${R.etf_days ?? "—"} 个交易日`);
        const c = Object.values(w.cells || {})[0];
        return c ? range(c.start, c.end) : w.week;
      } },
      ...funds.map((f) => ({ label: h("span", { title: f.name }, f.symbol, h("span", { class: "sub" }, ` ${f.name}`)), cls: "r",
        render: (w) => (w.cells?.[f.symbol] ? (w.total ? h("b", {}, signed(w.cells[f.symbol].flow)) : signed(w.cells[f.symbol].flow)) : "—") })),
    ];
    // 合计放在同一张表的第一行（单独一张表列宽对不齐）
    const totals = { week: "合计", total: true, cells: Object.fromEntries(funds.map((f) => [f.symbol, { flow: f.total }])) };
    const errs = Object.entries(e.errors || {});
    const gaps = funds.filter((f) => f.n_span_gaps);
    return panel("板块基金申购赎回（SPY + 11 只板块 SPDR）", {
      sub: `道富官网净值历史 · 资金流 = 份额变化 × 净值 · 取于 ${timeAgo(e.age_sec)}` + (funds[0] ? ` · 最新到 ${funds[0].last_date}` : ""),
    },
    e.refresh_failed ? h("p", { class: "callout warn" }, `刷新失败（${e.refresh_failed.reason_zh}），下面是旧的一份`) : null,
    errs.length ? h("p", { class: "callout warn" }, `没取到：${errs.map(([s, x]) => `${s}（${x.zh}）`).join("、")}`) : null,
    table(cols, [totals, ...(e.week_table || [])], { class: "flow" }),
    gaps.length ? h("p", { class: "small muted" }, `${gaps.map((f) => f.symbol).join("、")} 的文件里有几天两行之间隔了不止一个交易日（或日历判不了），那几行的变化跨了几天。`) : null,
    h("p", { class: "small muted" }, "SMH / SOXX 等非道富基金没有可程序读取的份额历史，这里没有。"));
  }

  const [stockEl, drawChart] = (() => { const x = stockPart(); return Array.isArray(x) ? x : [x, null]; })();
  fill(body,
    h("p", { class: "callout" }, r.disclaimer),
    stockEl,
    etfPart(),
    panel("怎么读", {}, h("ul", {}, (R.caveats || []).map((c) => h("li", {}, c)))));
  if (drawChart) drawChart();
}
