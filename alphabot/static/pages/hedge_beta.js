// 对冲页的「压力网格 β 窗口对照」（v0.45.662）：同一份审计，用审计里的 60 日 β（现状）与 250 日 β 各算一遍压力网格 / 尾部归因，并排摆出来。
// **只展示**：不进审计、不进告警、不改 SPY 覆盖的任何决定。数字全由服务端算好（tail_budget.beta_sensitivity → alphabot/hedge.py →
// GET /api/hedge/beta），这里只排版。250 日 β 现取（yfinance、限流下约半分钟），所以是单独的接口，不阻塞对冲页其余部分。
import { api, h, num, panel, table, isNum, fill, loading, errorBox } from "../lib.js";

const TITLE = "压力网格的 β 窗口对照（60 日 vs 250 日）";
// 四舍五入成 0 的数不带符号（同尾部预算面板）
const sPct = (v, dp = 1) => {
  if (!isNum(v)) return "—";
  const shown = num(Math.abs(v), dp);
  return `${Number(shown.replace(/,/g, "")) === 0 ? "" : v > 0 ? "+" : "−"}${shown}%`;
};
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const sBeta = (v) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), 2)}` : "—");
const cellName = (c) => (c ? `现货 ${c.spot_pct > 0 ? "+" : ""}${c.spot_pct}% · IV ${c.iv_pts > 0 ? "+" : ""}${c.iv_pts} 点` : "—");
const BAND = { inside: "带内", above: "上穿带", below: "下穿带" };
const say = (reasons, code) => (reasons || {})[code] || code || "—";

// 两列并排的汇总：每行 = 一个指标，左 = 60 日（现状），右 = 250 日
function summaryTable(R) {
  const B = R.base, A = R.alt;
  const rows = [
    { name: "全网格最差格", base: B.worst, alt: A.worst,
      b: sPct(-B.loss_pct_nav), a: sPct(-A.loss_pct_nav),
      bx: B.breach ? h("span", { class: "chip warn" }, `破 ${num(R.alert_pct, 0)}% 线`) : h("span", { class: "chip" }, "线内"),
      ax: A.breach ? h("span", { class: "chip warn" }, `破 ${num(R.alert_pct, 0)}% 线`) : h("span", { class: "chip" }, "线内"),
      bc: cellName(B.worst), ac: cellName(A.worst) },
    { name: "下行 / 平盘侧最差格", note: "SPY 认沽面板的规模目标", b: sPct(B.downside_worst?.pct_nav), a: sPct(A.downside_worst?.pct_nav),
      bx: null, ax: isNum(R.downside_ratio) ? h("span", { class: "small muted" }, `= 现状的 ${num(R.downside_ratio, 2)} 倍`) : null,
      bc: cellName(B.downside_worst), ac: cellName(A.downside_worst) },
    { name: "组合 β·$Delta", note: "SPY 覆盖建议吃的那个量（含现有 SPY 覆盖）", b: sPct(B.beta_dd_pct), a: sPct(A.beta_dd_pct),
      bx: B.band_status ? h("span", { class: `chip${B.band_status === "inside" ? "" : " warn"}` }, BAND[B.band_status] || B.band_status) : null,
      ax: A.band_status ? h("span", { class: `chip${A.band_status === "inside" ? "" : " warn"}` }, BAND[A.band_status] || A.band_status) : null,
      bc: isNum(R.band_pct) ? `带 ±${num(R.band_pct, 0)}% NAV` : "", ac: "" },
  ];
  return table([
    { label: "指标", render: (r) => h("span", {}, h("b", {}, r.name), r.note ? h("div", { class: "small muted" }, r.note) : null) },
    { label: `${R.base_label} β（现状）`, cls: "r", render: (r) => h("span", {}, h("b", { class: tone(parseFloat(String(r.b).replace("−", "-"))) }, r.b), " ", r.bx,
      r.bc ? h("div", { class: "small muted" }, r.bc) : null) },
    { label: `${R.alt_label} β`, cls: "r", render: (r) => h("span", {}, h("b", { class: tone(parseFloat(String(r.a).replace("−", "-"))) }, r.a), " ", r.ax,
      r.ac ? h("div", { class: "small muted" }, r.ac) : null) },
  ], rows);
}

function flipsTable(R) {
  if (!(R.flips || []).length) {
    return h("div", { class: "empty" }, "持仓标的里没有 β 符号相反或两套差 ≥ 0.5 的。",
      h("div", { class: "small muted" }, "这不等于 β 没问题：两套都为负的标的（例如能源股）不会列在这里，看下面逐标的表里的 β 一列。"));
  }
  return table([
    { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: `${R.base_label} β`, cls: "r", render: (r) => sBeta(r.base_beta) },
    { label: `${R.alt_label} β`, cls: "r", render: (r) => sBeta(r.alt_beta) },
    { label: "", render: (r) => (r.sign_flip ? h("span", { class: "chip warn" }, "符号相反") : h("span", { class: "chip" }, "差 ≥ 0.5")) },
    { label: "净 $Delta（% NAV）", cls: "r", render: (r) => h("span", { class: tone(r.net_dollar_delta_pct_nav) }, sPct(r.net_dollar_delta_pct_nav)) },
  ], R.flips);
}

function tickerTable(R) {
  return table([
    { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: "β（60 → 250）", cls: "r", render: (r) => h("span", { class: "small" }, `${sBeta(r.base_beta)} → ${sBeta(r.alt_beta)}`) },
    { label: "最差格贡献", cls: "r", render: (r) => h("span", {}, h("span", { class: tone(r.base_at_worst_pct) }, sPct(r.base_at_worst_pct)), " → ",
      h("b", { class: tone(r.alt_at_worst_pct) }, sPct(r.alt_at_worst_pct))) },
    { label: "下行 / 平盘格贡献", cls: "r", render: (r) => h("span", {}, h("span", { class: tone(r.base_at_downside_pct) }, sPct(r.base_at_downside_pct)), " → ",
      h("b", { class: tone(r.alt_at_downside_pct) }, sPct(r.alt_at_downside_pct))) },
  ], R.by_ticker || []);
}

// 压力网格（% NAV）：行 = 现货冲击，列 = IV 平移
function gridTable(S, title) {
  const cells = S.cells || [];
  const spots = [...new Set(cells.map((c) => c.spot_pct))].sort((a, b) => a - b);
  const ivs = [...new Set(cells.map((c) => c.iv_pts))].sort((a, b) => a - b);
  const at = (sp, iv) => cells.find((c) => c.spot_pct === sp && c.iv_pts === iv);
  const worst = cells.reduce((m, c) => (!m || c.pnl < m.pnl ? c : m), null);
  const cols = [{ label: "现货 \\ IV", render: (r) => h("b", {}, `${r.sp > 0 ? "+" : ""}${r.sp}%`) },
    ...ivs.map((iv) => ({ label: `${iv > 0 ? "+" : ""}${iv} 点`, cls: "r", render: (r) => {
      const c = at(r.sp, iv);
      const isWorst = worst && c && c.spot_pct === worst.spot_pct && c.iv_pts === worst.iv_pts;
      return h("span", { class: tone(c?.pct_nav), style: isWorst ? { fontWeight: "700", textDecoration: "underline" } : {} }, sPct(c?.pct_nav));
    } }))];
  return h("div", {}, h("h4", { class: "small" }, title), table(cols, spots.map((sp) => ({ sp }))));
}

export async function renderBetaSens(host, alive, force = false) {
  fill(host, panel(TITLE, {}, loading("正在取 250 日 β 并重算压力网格（限流下约半分钟）…")));
  let R;
  try { R = await api("/api/hedge/beta" + (force ? "?force=1" : "")); } catch (e) {
    if (alive()) fill(host, panel(TITLE, {}, errorBox(e)));
    return;
  }
  if (!alive()) return;
  try {
    body(host, alive, R);
  } catch (e) {
    fill(host, panel(TITLE, {}, errorBox(e), h("p", { class: "small muted" }, "返回的数据形状没认出来；其余对冲页内容不受影响。")));
  }
}

function body(host, alive, R) {
  const reload = h("button", { type: "button", class: "btn", onclick: () => renderBetaSens(host, alive, true) }, "重新取 β");
  const meta = R.meta || { reasons: {} };
  const lede = h("p", { class: "lede" },
    "压力网格里每个标的的现货冲击 = β × 大盘冲击，β 是审计里近 60 个交易日对 SPY 的回归。能源股（XOM / CVX）的 60 日 β 是负的，"
    + "网格在「SPY −10%」那一格里让它们上涨，下行格里的能源损失其实是股价涨造成的。这里用 250 日 β 把同一份审计再算一遍，并排摆出来。",
    h("b", {}, "没有哪个窗口是真值，两套都是模型假设；这里只展示，不进审计、不进告警、不改 SPY 覆盖的任何决定。"));
  if (!R.available) {
    fill(host, panel(TITLE, { right: reload }, lede,
      h("p", { class: "callout warn" }, `现在算不出来：${R.reason_zh || say(meta.reasons, R.reason)}`),
      (R.fetch?.failed || []).length ? h("p", { class: "small muted" }, `取 β 失败：${R.fetch.failed.map((f) => `${f.ticker}（${f.reason}）`).join("、")}`) : null));
    return;
  }
  const f = R.fetch || {};
  fill(host, panel(TITLE, { right: reload, sub: `审计日 ${R.date} · ${R.base_label} β = 审计里的值，${R.alt_label} β 现取（yfinance，${R.cache_age_sec > 1 ? `${num(R.cache_age_sec / 60, 0)} 分钟前取的` : "刚取的"}）${R.demo ? " · 演示数据" : ""}` },
    lede,
    R.partial ? h("p", { class: "callout warn" }, `混合口径：${(R.missing || []).length ? `${R.missing.join("、")} 没有 ${R.alt_label} β，沿用了 ${R.base_label} 值；` : ""}`
      + `${R.base.partial || R.alt.partial ? "压力表有行被剔除，最差格可能被低估；" : ""}对照里这几只两套是同一个 β。`) : null,
    summaryTable(R),
    h("p", { class: "small muted" }, "下行 / 平盘侧最差格就是「尾部对冲候选」面板定规模用的那一格；未触上限时认沽单位数按它的比例缩放。"
      + "β·$Delta 含现有 SPY 空头覆盖——它是 SPY 覆盖建议吃的那个量，所以 β 窗口一换，建议是否出带就可能不同（这里只展示，不改成交）。"),
    h("div", { style: { height: "12px" } }),
    h("h3", { class: "small" }, "β 符号相反或差 ≥ 0.5 的持仓标的"), flipsTable(R),
    h("div", { style: { height: "12px" } }),
    h("h3", { class: "small" }, "逐标的贡献（% NAV；60 日 → 250 日；负 = 亏）"), tickerTable(R),
    h("div", { style: { height: "12px" } }),
    h("h3", { class: "small" }, "压力网格（% NAV；粗体下划线 = 最差格）"),
    h("div", { class: "grid halves" }, gridTable(R.base, `${R.base_label} β（现状）`), gridTable(R.alt, `${R.alt_label} β`)),
    h("p", { class: "small muted" }, `取数：${f.n_ok ?? "—"} / ${f.n_requested ?? "—"} 只取到 ${R.alt_label} β`,
      (f.failed || []).length ? `；没取到：${f.failed.map((x) => `${x.ticker}（${x.reason}）`).join("、")}` : "", "。",
      "日线来自 yfinance（复权收盘），对 SPY 日收益做 OLS，取最近 250 个两边都有的交易日、少于 200 个就判不了。")));
}
