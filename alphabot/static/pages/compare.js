// 对比页：期权结构的跨日对比——今天这次扫描 vs 上一次扫描（接替停摆的 Options compare daily 任务，v0.45.759）。
// 数据是每日扫描落盘的结果，全部只读；读数、变化、提示都由服务端算好（alphabot/options_compare.py），前端只排版。
// 提示阈值取自接口返回的 rules，不在这里另抄。本页不出方向判断、不出分数——只摆读数怎么变。
import { api, h, helpLink, num, usdShort, signedPct, panel, table, isNum, fill } from "../lib.js";
import { lines } from "../charts.js";
import { go } from "../app.js";

const DIR = { bullish: "看多", bearish: "看空", neutral: "中性" };
const dirChip = (d) => h("span", { class: `chip ${d === "bullish" ? "pos" : d === "bearish" ? "neg" : ""}` }, DIR[d] || d || "—");
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const signed = (v, dp = 2) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), dp)}` : "—");
// 一个格子：今天的值 + 下面一行小字写变化（变化算不出就不写，不写成 0）
const cell = (val, delta, deltaText) => h("span", {}, val,
  isNum(delta) && delta !== 0 ? h("div", { class: `sub ${tone(delta)}` }, deltaText) : null);
const ivrCell = (r, dv) => h("span", {}, isNum(r.iv_rank) ? num(r.iv_rank, 0) : "—",
  r.iv_rank_source === "hv_proxy" ? h("span", { class: "chip warn", title: "这一天没有足够的 IV 历史，用历史波动率的分位顶替——不是真正的 IV Rank" }, "HV 代") : null,
  dv && dv.available && dv.iv_rank_comparable === false ? h("div", { class: "sub" }, "两天口径不同，不比")
    : (dv && isNum(dv.iv_rank) && dv.iv_rank !== 0 ? h("div", { class: `sub ${tone(dv.iv_rank)}` }, signed(dv.iv_rank, 0)) : null));
const oiShort = (v) => (isNum(v) ? (v >= 1e6 ? `${num(v / 1e6, 2)}M` : v >= 1e3 ? `${num(v / 1e3, 0)}K` : num(v, 0)) : "—");
const prevNote = (r) => (r.prev_date ? `对比 ${r.prev_date}` + (r.scans_between ? `（中间有 ${r.scans_between} 个扫描日没有这只票）` : "") : "之前没有记录");

function dateSelect(dates, date, onChange) {
  return dates.length ? h("select", { class: "sel", "aria-label": "扫描日期", onchange: (e) => onChange(e.target.value) },
    [...dates].reverse().map((x) => h("option", { value: x, selected: x === date }, x))) : null;
}

function unreadableNote(d) {
  const bad = Object.entries(d.unreadable || {});
  return bad.length ? h("p", { class: "callout warn" }, "有扫描结果文件读不了，对比时越过了它们：", bad.map(([k, v]) => `${k}（${v}）`).join("；")) : null;
}

const CAVEAT = "数据是每日扫描收盘后记下的期权读数，不是实时。提示只说「哪个数变得多」，阈值是旧对比任务留下的经验值、未经检验；"
  + "综合分与方向也未被证实有预测力。异动流来自 CBOE 延迟快照，分不出买卖方向与开平仓。不构成投资建议。";

export async function renderCompare(root, ticker, alive) {
  let date = null;
  async function draw() {
    const q = date ? `?date=${encodeURIComponent(date)}` : "";
    const d = await api(ticker ? `/api/compare/${encodeURIComponent(ticker)}${q}` : `/api/compare${q}`);
    if (!alive()) return;
    date = d.date || date;
    const pick = (v) => { date = v; draw(); };
    if (ticker) drawTicker(root, d, pick); else drawOverview(root, d, pick);
  }
  await draw();
}

function drawOverview(root, d, pick) {
  const head = h("div", { class: "page-head" },
    h("div", {},
      h("h1", {}, "对比", h("span", { class: "sub" }, d.date ? `扫描 ${d.date}` + (d.prev_scan_date ? ` vs 上一次 ${d.prev_scan_date}` : "") : "")),
      h("p", { class: "lede" }, "每只标的的期权结构，今天这次扫描比上一次变了什么。有变化提示的排在前面；点行看单票详情。 ", helpLink("compare"))),
    h("div", { class: "controls" }, dateSelect(d.dates || [], d.date, pick)));
  if (!d.available) {
    fill(root, head, h("div", { class: "empty" }, d.reason_zh || "没有可对比的数据。"), unreadableNote(d));
    return;
  }
  const cols = [
    { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: "方向", render: (r) => h("span", {}, dirChip(r.direction), r.flipped ? h("span", { class: "chip warn" }, "翻转") : null) },
    { label: "综合分", cls: "r", render: (r) => cell(num(r.score, 2), r.delta.score, signed(r.delta.score, 2)) },
    { label: "现价", cls: "r", render: (r) => cell(num(r.spot, 2), r.delta.spot_pct, signedPct(r.delta.spot_pct, 2)) },
    { label: "P/C（全链）", cls: "r", render: (r) => cell(num(r.full_pc_ratio, 2), r.delta.full_pc_ratio, signed(r.delta.full_pc_ratio, 2)) },
    { label: "IV Rank", cls: "r", render: (r) => ivrCell(r, r.delta) },
    { label: "IV Skew", cls: "r", render: (r) => cell(num(r.skew, 3), r.delta.skew, signed(r.delta.skew, 3)) },
    { label: "全链 OI", cls: "r", render: (r) => cell(oiShort(r.full_oi), r.delta.full_oi_pct, signedPct(r.delta.full_oi_pct, 1)) },
    { label: "异动里 Call 占比", cls: "r", render: (r) => cell(isNum(r.call_pct) ? `${num(r.call_pct, 1)}%` : "—", r.delta.call_pct, signed(r.delta.call_pct, 1)) },
    { label: "变化提示", render: (r) => (r.alerts.length
      ? h("span", {}, r.alerts.map((a) => h("div", { class: "small" }, a.text)))
      : h("span", { class: "sub" }, r.prev_date ? (r.options_available ? "无" : "这天没有期权记录") : "之前没有记录")) },
  ];
  const withAlerts = d.rows.filter((r) => r.alert_count).length;
  fill(root, head, unreadableNote(d),
    panel(`${d.rows.length} 只标的 · ${withAlerts} 只有变化提示`,
      { sub: "小字是相对上一次有这只票的扫描的变化；上一次不是紧挨着的那个扫描日时，详情页会写明" },
      table(cols, d.rows, { onRow: (r) => go(`#/compare/${encodeURIComponent(r.ticker)}`) })),
    h("p", { class: "note small muted" }, CAVEAT));
}

function kpi(k, v, sub, cls = "") {
  return h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
    sub ? h("div", { class: `small ${cls || "muted"}` }, sub) : null);
}

function levelsTable(title, today, prev) {
  const prevSet = new Set((prev || []).map((x) => x.strike));
  const cols = [
    { label: title, render: (r) => h("span", {}, h("b", {}, num(r.strike, 2)), prev && !prevSet.has(r.strike) ? h("span", { class: "chip warn" }, "新") : null) },
    { label: "距现价", cls: "r", render: (r) => signedPct(r.dist_pct, 1) },
    { label: "OI", cls: "r", render: (r) => oiShort(r.oi) },
  ];
  return today.length ? table(cols, today) : h("div", { class: "empty" }, "这天没有记录。");
}

function drawTicker(root, d, pick) {
  const head = h("div", { class: "page-head" },
    h("div", {},
      h("h1", {}, `对比 · ${d.ticker}`, h("span", { class: "sub" }, d.date ? `扫描 ${d.date}` : "")),
      h("p", { class: "lede" }, h("a", { href: "#/compare" }, "← 全部标的"), " · ",
        h("a", { href: `#/deep/${encodeURIComponent(d.ticker)}` }, "深度报告"), " · ",
        h("a", { href: `#/t/${encodeURIComponent(d.ticker)}/levels` }, "去标的页现拉"), " · ", helpLink("compare"))),
    h("div", { class: "controls" }, dateSelect(d.dates || [], d.date, pick)));
  if (!d.available) {
    fill(root, head, h("div", { class: "empty" }, d.reason_zh || "没有可对比的数据。"), unreadableNote(d));
    return;
  }
  const t = d.today, p = d.prev, dl = d.delta, fs = t.flow_summary;
  const was = (v, dp = 2) => (p && isNum(v) ? `上一次 ${num(v, dp)}` : null);
  const kpis = h("div", { class: "kpis" },
    kpi("方向", dirChip(t.direction), p ? `上一次 ${DIR[p.direction] || p.direction || "—"}` : null, dl.direction_flipped ? "down" : ""),
    kpi("综合分", num(t.score, 2), isNum(dl.score) ? `${signed(dl.score, 2)}（${was(p.score)}）` : null, tone(dl.score)),
    kpi("现价", num(t.spot, 2), isNum(dl.spot_pct) ? `${signedPct(dl.spot_pct, 2)}（${was(p.spot)}）` : null, tone(dl.spot_pct)),
    kpi("P/C（全链）", num(t.full_pc_ratio, 2), isNum(dl.full_pc_ratio) ? `${signed(dl.full_pc_ratio, 2)}（${was(p.full_pc_ratio)}）` : null, tone(dl.full_pc_ratio)),
    kpi("全链 OI", oiShort(t.full_oi), isNum(dl.full_oi_pct) ? `${signedPct(dl.full_oi_pct, 1)}（上一次 ${oiShort(p.full_oi)}）` : null, tone(dl.full_oi_pct)),
    kpi("IV Rank", h("span", {}, isNum(t.iv_rank) ? num(t.iv_rank, 0) : "—",
      t.iv_rank_source === "hv_proxy" ? h("span", { class: "chip warn", title: "用历史波动率的分位顶替，不是真正的 IV Rank" }, "HV 代") : null),
      dl.available && dl.iv_rank_comparable === false ? "两天口径不同，不比" : (isNum(dl.iv_rank) ? `${signed(dl.iv_rank, 0)}（${was(p.iv_rank, 0)}）` : null), tone(dl.iv_rank)),
    kpi("IV / RV30", `${isNum(t.iv_current) ? num(t.iv_current, 1) : "—"} / ${isNum(t.rv_30d) ? num(t.rv_30d, 1) : "—"}`,
      isNum(dl.iv_current) ? `IV ${signed(dl.iv_current, 2)}` : null, tone(dl.iv_current)),
    kpi("IV Skew（虚值 put ÷ call）", num(t.skew, 3), isNum(dl.skew) ? `${signed(dl.skew, 3)}（${was(p.skew, 3)}）` : null, tone(dl.skew)),
    kpi("P/C（近端窗口）", num(t.pc_ratio, 2), p ? was(p.pc_ratio) : null),
    kpi("窗口 OI（4 个到期日）", oiShort(t.total_oi), p && isNum(p.total_oi) ? `上一次 ${oiShort(p.total_oi)}` : null),
    kpi("Max Pain（近 7 天）", num(t.max_pain, 2), isNum(dl.max_pain) && dl.max_pain !== 0 ? `${signed(dl.max_pain, 2)}` : null));

  const flowCols = [
    { label: "侧", render: (r) => h("span", { class: `chip ${r.side === "call" ? "pos" : "neg"}` }, r.side === "call" ? "CALL" : "PUT") },
    { label: "行权价", cls: "r", render: (r) => num(r.strike, 2) },
    { label: "到期", render: (r) => h("span", {}, r.expiry || "—", isNum(r.days_to_exp) ? h("span", { class: "sub" }, ` ${num(r.days_to_exp, 0)} 天`) : null) },
    { label: "虚值 %（负 = 实值）", cls: "r", render: (r) => signedPct(r.otm_pct, 1) },
    { label: "成交量", cls: "r", render: (r) => num(r.volume, 0) },
    { label: "OI", cls: "r", render: (r) => num(r.oi, 0) },
    { label: "量 ÷ OI", cls: "r", render: (r) => (isNum(r.vol_oi) ? `${num(r.vol_oi, 2)}×` : "—") },
    { label: "权利金金额", cls: "r", render: (r) => usdShort(r.premium_usd) },
  ];
  const flowSub = `共 ${fs.count} 条，列出金额最大的 ${t.flows.length} 条 · Call 成交量占 ${isNum(fs.call_pct) ? num(fs.call_pct, 1) + "%" : "—"}`
    + (isNum(dl.call_pct) ? `（${signed(dl.call_pct, 1)}）` : "")
    + ` · Call 成交重心 ${num(fs.call_centroid, 2)}` + (isNum(dl.call_centroid) ? `（${signed(dl.call_centroid, 2)}）` : "")
    + (fs.unclassified ? ` · ${fs.unclassified} 条分不出 call / put，没计入` : "");

  const hist = d.history || [];
  const x = hist.map((r) => r.date.slice(5));
  const mk = (title, series, yFmt) => {
    const el = h("div", { class: "chart short" });
    return [panel(title, {}, el), () => lines(el, { x, series, yFmt })];
  };
  const charts = hist.length >= 2 ? [
    mk("综合分", [{ name: "综合分", data: hist.map((r) => r.score) }], (v) => num(v, 1)),
    mk("P/C", [{ name: "全链", data: hist.map((r) => r.full_pc_ratio) }, { name: "近端窗口", data: hist.map((r) => r.pc_ratio), dash: true }], (v) => num(v, 2)),
    mk("IV 与 IV Rank", [{ name: "IV", data: hist.map((r) => r.iv_current) }, { name: "IV Rank", data: hist.map((r) => r.iv_rank), dash: true }], (v) => num(v, 0)),
    mk("OI", [{ name: "全链", data: hist.map((r) => r.full_oi) }, { name: "近端窗口（4 个到期日）", data: hist.map((r) => r.total_oi), dash: true }], oiShort),
  ] : [];

  fill(root, head, unreadableNote(d),
    panel("今天 vs 上一次", { sub: prevNote(d) }, kpis,
      t.options_available ? null : h("p", { class: "callout warn" }, "这一天这只标的没有期权分析记录，期权读数都是空的。"),
      h("p", { class: "note small muted" }, "「近端窗口」的两项只数扫描选中的 4 个到期日。月度到期日进出这个窗口时它们会大幅跳变（窗口换了，不是持仓变了），所以只摆出来、不算变化也不挂提示；看变化请看「全链」。")),
    panel("变化提示", { sub: "只陈述哪个数变得多，不解读成看涨 / 看跌" },
      d.alerts.length ? h("ul", {}, d.alerts.map((a) => h("li", {}, a.level === "flip" ? h("b", {}, a.text) : a.text)))
        : h("div", { class: "empty" }, p ? "没有超过阈值的变化。" : (d.reason_zh || "没东西可比。"))),
    panel("异动流（今天）", { sub: flowSub },
      t.flows.length ? table(flowCols, t.flows) : h("div", { class: "empty" }, "这天没有异动流记录。")),
    h("div", { class: "grid halves" },
      panel("Put 侧 OI 集中位", { sub: "扫描记为「支撑」；标「新」= 上一次不在前几档里" }, levelsTable("行权价", t.supports, p && p.supports)),
      panel("Call 侧 OI 集中位", { sub: "扫描记为「阻力」" }, levelsTable("行权价", t.resistances, p && p.resistances))),
    ...charts.map((c) => c[0]),
    hist.length >= 2 ? h("p", { class: "note small muted" }, `趋势：最近 ${hist.length} 个有这只票的扫描日（横轴是日期，缺扫描的日子不补点）。`) : null,
    h("p", { class: "note small muted" }, CAVEAT));
  charts.forEach((c) => c[1]());
}
