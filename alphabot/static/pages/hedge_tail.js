// 对冲页的「尾部对冲候选」面板（v0.45.643）：SPY 认沽后比例价差叠到组合压力网格上。全部只读，不写账本、不成交。
// 数字全由服务端算好（alphabot/tail_hedge.py），这里只排版；原因 / 旗标 / 提醒的文案取自服务端 meta，前端不另抄。
import { api, h, num, money, panel, table, isNum, fill, loading, errorBox } from "../lib.js";
import { lines, C } from "../charts.js";

const sMoney = (v, dp = 0) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}$${num(Math.abs(v), dp)}` : "—");
// 四舍五入成 0 的数不带符号（"−0%" 读起来像「变差了」，其实是「没变」）
const sPct = (v, dp = 2) => {
  if (!isNum(v)) return "—";
  const shown = num(Math.abs(v), dp);
  return `${Number(shown.replace(/,/g, "")) === 0 ? "" : v > 0 ? "+" : "−"}${shown}%`;
};
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const cellName = (c) => (c ? `现货 ${c.spot_pct > 0 ? "+" : ""}${c.spot_pct}% · IV ${c.iv_pts > 0 ? "+" : ""}${c.iv_pts} 点` : "—");
const GREEK_LABEL = {
  beta_dollar_delta: "β·$Delta", dollar_delta: "$Delta", gamma_dollar_per_1pct: "Γ（1% 移动）",
  vega_dollar_per_pt: "Vega / 波动率点", theta_dollar_per_day: "Theta / 日",
};
const TITLE = "尾部对冲候选（SPY 认沽后比例价差）";

// 「code」或「code:后缀」→ 人话；表里没有就原样显示（不吞）
function say(table_, code) {
  const [head, ...rest] = String(code).split(":");
  const base = (table_ || {})[head];
  return base ? (rest.length ? `${base}（${rest.join(":")}）` : base) : String(code);
}

const candName = (c) => `卖 Δ${num(c.short_level, 2)} · 买 ${c.ratio}×Δ${num(c.long_level, 2)}`;
const strikes = (c) => {
  const s = (c.legs || []).find((l) => l.role === "short"), l = (c.legs || []).find((x) => x.role === "long");
  return s && l ? `卖 ${num(s.strike, 0)} / 买 ${num(l.strike, 0)}` : "—";
};

function baselineStrip(b, chain) {
  const kpi = (k, v, sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
    sub ? h("div", { class: "small muted" }, sub) : null);
  const dw = b.downside_worst_cell;
  return h("div", { class: "kpis" },
    kpi("基线最差格", h("span", { class: "down" }, sPct(b.worst_cell?.pct_nav, 1)), `${cellName(b.worst_cell)} · ${sMoney(b.worst_cell?.pnl)}`),
    kpi("下行侧最差格（对它定规模）", h("span", { class: "down" }, sPct(dw?.pct_nav, 1)), dw ? `${cellName(dw)} · ${sMoney(dw.pnl)}` : "—"),
    kpi("审计日 / NAV", b.date, `NAV ${money(b.nav, 0)} · SPY ${money(b.spy_price)}${b.adjusted_for_trade ? " · 已含当日 SPY 成交" : ""}`),
    kpi("期权链", `${chain?.expiry || "—"}（${chain?.dte ?? "—"} 天）`, `SPY ${money(chain?.spot)} · ${chain?.n_puts_in_expiry ?? "—"} 个认沽（行权价）`));
}

function candTable(t, selId, onPick) {
  const cols = [
    { label: "候选", render: (c) => h("b", {}, candName(c)) },
    { label: "行权价", render: (c) => strikes(c) },
    { label: "单位", cls: "r", render: (c) => (c.available ? `${c.sizing.n_units} 组（卖 ${c.sizing.contracts_short} / 买 ${c.sizing.contracts_long}）` : "—") },
    { label: "净成本", cls: "r", render: (c) => (c.available
      ? h("span", { class: c.unit.is_credit ? "up" : "" }, `${c.unit.is_credit ? "收 " : "付 "}${money(Math.abs(c.sizing.net_cost_usd), 0)}`,
        h("span", { class: "sub" }, `${num(Math.abs(c.sizing.net_cost_pct_nav), 2)}% NAV`)) : "—") },
    { label: "卖出腿名义额", cls: "r", render: (c) => (c.available ? `${num(c.sizing.short_notional_pct_nav, 0)}% NAV` : "—") },
    { label: "下行最差格 前 → 后", cls: "r", render: (c) => (c.available && c.effect.downside_worst_before
      ? h("span", {}, `${sMoney(c.effect.downside_worst_before.pnl)} → `, h("b", {}, sMoney(c.effect.downside_worst_after?.pnl))) : "—") },
    { label: "已抵消（目标格）", cls: "r", render: (c) => (c.available && isNum(c.sizing.offset_achieved)
      ? h("span", { class: tone(c.sizing.offset_achieved) }, sPct(c.sizing.offset_achieved * 100, 0).replace(/^\+/, ""),
        h("span", { class: "sub" }, `目标 ${num(c.sizing.offset_target * 100, 0)}%`)) : "—") },
    { label: "变差的格", cls: "r", render: (c) => (c.available ? `${c.effect.n_cells_worse} / ${c.effect.n_cells}` : "—") },
    { label: "到期最大亏损", cls: "r", render: (c) => (c.available ? h("span", { class: "down" }, sMoney(c.expiry.max_loss_usd)) : "—") },
    { label: "说明", render: (c) => (c.available
      ? h("span", { class: "small muted", title: (c.flags || []).map((f) => say(t.meta.flags, f)).join("\n") },
        (c.flags || []).map((f) => say(t.meta.flags_short, f)).join(" · "))
      : h("span", { class: "small muted" }, c.reason_zh || say(t.meta.reasons, c.reason))) },
  ];
  return table(cols, t.candidates, { onRow: (c) => c.available && onPick(c.id), rowClass: (c) => (c.id === selId ? "sel" : "") });
}

function gridTable(cand, grid, target) {
  const by = new Map(cand.effect.cells.map((c) => [`${c.spot_pct}|${c.iv_pts}`, c]));
  const cols = [{ label: "SPY 涨跌", render: (sp) => h("b", {}, `${sp > 0 ? "+" : ""}${sp}%`) }].concat(grid.iv_pts.map((iv) => ({
    label: `IV ${iv > 0 ? "+" : ""}${iv} 点`, cls: "r",
    render: (sp) => {
      const c = by.get(`${sp}|${iv}`);
      if (!c) return "—";
      const isT = target && target.spot_pct === sp && target.iv_pts === iv;
      return h("span", { title: `基线 ${sMoney(c.base)} · 对冲 ${sMoney(c.hedge)} · 合计 ${sMoney(c.after)}` },
        h("b", { class: tone(c.after) }, sMoney(c.after)), isT ? " ◎" : "",
        h("span", { class: `sub ${tone(c.hedge)}` }, `对冲 ${sMoney(c.hedge)}`));
    },
  })));
  return table(cols, [...grid.spot_pct].sort((a, b) => a - b));
}

function greeksTable(g) {
  if (!g) return h("div", { class: "empty" }, "Greeks 算不出：有腿缺 gamma / vega / theta（交易所报价没给）。");
  const cols = [
    { label: "组合 Greeks（占 NAV %）", render: (k) => GREEK_LABEL[k] },
    { label: "对冲前", cls: "r", render: (k) => sPct(g.before_pct_nav[k], 3) },
    { label: "候选带来", cls: "r", render: (k) => sMoney(g.sized_usd[k]) },
    { label: "对冲后", cls: "r", render: (k) => h("b", {}, sPct(g.after_pct_nav[k], 3)) },
  ];
  return table(cols, Object.keys(GREEK_LABEL));
}

function legsTable(c) {
  const cols = [
    { label: "腿", render: (l) => (l.role === "short" ? "卖出" : "买入") },
    { label: "张数", cls: "r", render: (l) => `${l.qty_contracts > 0 ? "+" : "−"}${Math.abs(l.qty_contracts)} / 单位` },
    { label: "行权价", cls: "r", render: (l) => num(l.strike, 0) },
    { label: "Δ", cls: "r", render: (l) => num(l.delta, 3) },
    { label: "bid / ask", cls: "r", render: (l) => `${num(l.bid, 2)} / ${num(l.ask, 2)}` },
    { label: "点差", cls: "r", render: (l) => (isNum(l.spread_pct) ? `${num(l.spread_pct * 100, 1)}%` : "—") },
    { label: "IV", cls: "r", render: (l) => (isNum(l.iv) ? `${num(l.iv * 100, 1)}%` : "—") },
    { label: "未平仓量", cls: "r", render: (l) => (l.oi ? num(l.oi, 0) : "未知") },
  ];
  return table(cols, c.legs);
}

function detail(t, c) {
  const e = c.effect, x = c.expiry, curveEl = h("div", { class: "chart short" });
  const target = c.sizing.target_cell;
  const node = h("div", {},
    h("p", { class: "small" }, `规模：${c.sizing.basis === "offset_target"
      ? `在下行侧最差格（${cellName(target)}）抵消 ${num(c.sizing.offset_target * 100, 0)}% 亏损所需的整数单位`
      : `按 1 单位显示——${c.sizing.reason_zh || "定不了规模"}`}。`
      + `实际抵消 ${isNum(c.sizing.offset_achieved) ? num(c.sizing.offset_achieved * 100, 0) + "%" : "—"}；`
      + `全网格最差 ${sPct(e.worst_before_pct_nav, 1)} → ${sPct(e.worst_after_pct_nav, 1)} NAV（${cellName(e.worst_after)}）；`
      + `候选自己最亏的格 ${sMoney(e.worst_hedge_cell.pnl)}（${cellName(e.worst_hedge_cell)}）。`),
    (c.flags || []).map((f) => h("p", { class: "callout warn" }, say(t.meta.flags, f))),
    h("h3", { class: "small" }, "压力网格：对冲后合计（◎ = 定规模的目标格；小字 = 候选自己的贡献）"),
    gridTable(c, t.grid, target),
    h("div", { style: { height: "12px" } }),
    h("div", { class: "grid halves" },
      h("div", {}, h("h3", { class: "small" }, "持有到期的盈亏（按定好的规模）"),
        curveEl,
        h("p", { class: "small muted" }, `最大亏损 ${sMoney(x.max_loss_usd)}，出现在 SPY ${num(x.at_spot, 0)}（${sPct(x.at_spot_pct, 1)}）；`
          + `盈亏平衡点 ${(x.breakevens || []).map((b) => num(b, 0)).join("、") || "—"}；上行方向 ${sMoney(x.upside_usd)}。`
          + `单位成本：成交价 ${money(c.unit.cost_executable_usd, 0)}，中间价 ${money(c.unit.cost_mid_usd, 0)}（跨点差多付 ${money(c.unit.cross_spread_usd, 0)}）。`)),
      h("div", {}, h("h3", { class: "small" }, "组合 Greeks 的变化"), greeksTable(c.greeks))),
    h("div", { style: { height: "12px" } }),
    h("h3", { class: "small" }, "两条腿（每单位）"),
    legsTable(c));
  node._after = () => {
    const col = C();
    lines(curveEl, { x: x.curve.map((p) => `${p.spot_pct}%`), yFmt: (v) => sMoney(v),
      series: [{ name: "到期盈亏", data: x.curve.map((p) => p.pnl), color: col.ink, width: 2 }],
      hmarks: [{ value: 0, label: "", color: col.lineStrong, type: "solid" }] });
  };
  return node;
}

export async function renderTailHedge(host, alive, force = false) {
  fill(host, panel(TITLE, {}, loading("正在拉 SPY 期权链并叠加压力网格…")));
  let t;
  try { t = await api("/api/hedge/tail" + (force ? "?force=1" : "")); } catch (e) {
    if (alive()) fill(host, panel(TITLE, {}, errorBox(e)));
    return;
  }
  if (!alive()) return;
  const reload = h("button", { type: "button", class: "btn", onclick: () => renderTailHedge(host, alive, true) }, "重新拉取");
  const meta = t.meta || { reasons: {}, flags: {}, caveats: {} };
  t.meta = meta;
  const lede = h("p", { class: "lede" },
    "组合最差的压力格是「大幅波动 + IV 上升」，SPY 股票对冲管不到它。后比例（卖 1 张较近的认沽、买 2 张更远的认沽）在下跌里净多 Gamma / Vega，"
    + "这里把候选叠到压力网格上，看哪些格变好、哪些格变差。", h("b", {}, "只读分析，不是推荐、不写账本；"), "候选按固定顺序排，不按效果排。");
  if (!t.available) {
    fill(host, panel(TITLE, { right: reload }, lede,
      h("p", { class: "callout warn" }, `现在算不出来：${t.reason_zh || say(meta.reasons, t.reason)}${t.chain_reason ? `（取链：${t.chain_reason}）` : ""}`),
      t.baseline ? baselineStrip(t.baseline, null) : null));
    return;
  }
  let sel = (t.candidates.find((c) => c.available) || {}).id || null;
  const detailHost = h("div");
  const listHost = h("div");
  const draw = () => {
    fill(listHost, candTable(t, sel, (id) => { sel = id; draw(); }));
    const c = t.candidates.find((x) => x.id === sel);
    if (!c) { fill(detailHost, h("div", { class: "empty" }, "没有可用候选。")); return; }
    const d = detail(t, c);
    fill(detailHost, panel(`${candName(c)} · ${strikes(c)}`, { sub: "点上面的行切换候选" }, d));
    d._after();
  };
  fill(host, panel(TITLE, { right: reload, sub: `审计日 ${t.baseline.date} · ${t.cache_age_sec > 1 ? `缓存 ${num(t.cache_age_sec, 0)} 秒前` : "刚拉取"}` },
    lede,
    (t.flags || []).map((f) => h("p", { class: "callout warn" }, say(meta.flags, f))),
    baselineStrip(t.baseline, t.chain),
    h("div", { style: { height: "12px" } }),
    listHost),
  h("div", { style: { height: "16px" } }),
  detailHost,
  h("div", { class: "note small muted", style: { marginTop: "12px" } },
    Object.entries(meta.caveats).map(([k, v]) => h("p", { key: k }, `· ${v}`))));
  draw();
}
