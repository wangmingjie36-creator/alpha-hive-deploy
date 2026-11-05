// 接货 · 筛选：30 只标的里「卖 put 好不好做」的客观事实（我们手工做过的那张表）。只读；只并排事实，**不排名、不推荐、不给综合分**。
// 三层数据：离线层（账本梯子，秒开）/ 行情层（回撤、波动、21 日最差、点击取）/ 取链层（按你的接货价选合约，逐只点）。
// 数字全由服务端算好，前端只排版与排序；名单与接货价存在 Alpha Bot 设置里，**只供显示、不改账本**。
import { api, h, helpLink, num, money, panel, table, isNum, fill } from "../lib.js";

const sPct = (v, dp = 1) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), dp)}%` : "—");
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const WAIT = "等一下再点（Yahoo 限流冷却中）";

// 排序键：只取表里已有的数，缺值一律排最后
const SORTS = {
  ticker: { label: "代码", get: (r) => r.ticker, asc: true },
  dd52: { label: "距 52 周高（跌得多的在前）", get: (r) => r.stats?.dd52_pct, asc: true },
  vol: { label: "近 1 年波动（低的在前）", get: (r) => r.stats?.vol1y_pct, asc: true },
  y20: { label: "20Δ 年化（高的在前）", get: (r) => r.ladder?.rungs?.["0.20"]?.annual_yield_pct, asc: false },
  cap20: { label: "20Δ 单张占资金（小的在前）", get: (r) => r.ladder?.rungs?.["0.20"]?.capital_usd, asc: true },
  p10: { label: "21 日跌超 10% 的频率（低的在前）", get: (r) => r.stats?.p21_le_m10_pct, asc: true },
};

function sortRows(rows, key) {
  const s = SORTS[key] || SORTS.ticker;
  const val = (r) => { const v = s.get(r); return v === undefined || v === null ? null : v; };
  return [...rows].sort((a, b) => {
    const x = val(a), y = val(b);
    if (x === null && y === null) return a.ticker.localeCompare(b.ticker);
    if (x === null) return 1;
    if (y === null) return -1;
    if (typeof x === "string") return a.ticker.localeCompare(b.ticker);
    return s.asc ? x - y : y - x;
  });
}

function rungCell(r, k) {
  const x = r.ladder?.rungs?.[k];
  if (!x) return h("span", { class: "muted" }, "—");
  const flags = [];
  if (!x.spread_ok) flags.push(h("span", { class: "chip warn", title: "点差超过上限" }, "点差"));
  if (!x.oi_ok) flags.push(h("span", { class: "chip warn", title: "未平仓量低于下限" }, "OI"));
  return h("span", {}, `${num(x.strike, 2)}（${sPct(x.dist_pct)}） `,
    h("span", { class: "sub" }, `点差 ${isNum(x.spread_pct) ? num(x.spread_pct, 0) + "%" : "—"} · OI ${num(x.oi, 0)} · 年化 ${isNum(x.annual_yield_pct) ? num(x.annual_yield_pct, 1) + "%" : "—"} · 占 ${money(x.capital_usd, 0)}`),
    ...flags);
}

function chainDetail(c) {
  if (!c) return null;
  if (!c.ok) return h("div", { class: "small muted" }, `取链 / 选合约：${c.reason}${c.detail ? `（${c.detail}）` : ""}`
    + (c.accept_price ? `；接货价 ${num(c.accept_price, 2)}（${c.accept_price_source === "user" ? "你给的" : "占位"}）` : ""));
  const rp = c.replay;
  return h("div", { class: "sd-live" },
    h("div", { class: "sd-k" }, `按接货价 ${num(c.accept_price, 2)}（${c.accept_price_source === "user" ? "你给的" : "占位：现价打折"}，离现价 ${sPct(c.distance_pct)}）选出：`),
    h("div", { class: "sd-row" }, `${c.symbol} · 行权价 ${num(c.strike, 2)}（${sPct(c.strike_dist_pct)}）· 到期 ${c.expiry}（${c.dte} 天）· bid ${num(c.bid, 2)} · 点差 ${num(c.spread_pct, 1)}% · OI ${num(c.oi, 0)} · Δ ${isNum(c.delta) ? num(c.delta, 2) : "—"}`),
    h("div", { class: "sd-row" }, `年化 ${num(c.annual_yield_pct, 1)}% · 被行权净成本 ${num(c.net_cost, 2)}（比现价 ${sPct(c.net_discount_pct)}）· 一张占资金 ${money(c.capital_usd, 0)} · `
      + (c.earnings_in_window === true ? "财报在到期前" : c.earnings_in_window === false ? "财报不在到期前" : "财报日待验证")),
    rp?.ok ? h("div", { class: "small muted" }, `把这张的 bid 放回过去的真实行情（重叠窗口 ${rp.n_windows} 个、互不重叠约 ${rp.independent_windows} 个）：平均净收益 ${sPct(rp.mean_net_pct, 2)}（占行权价）· 净赚 ${num(rp.win_pct, 0)}% · 5% 分位 ${sPct(rp.p05_net_pct, 1)} · 最差 ${sPct(rp.worst_net_pct, 1)}。偏乐观、样本很小，不是预测。`)
      : h("div", { class: "small muted" }, c.replay_needs_prices ? "权利金重放要日线：先点「取行情统计」。" : `权利金重放不给：${rp?.reason || "—"}`));
}

export async function renderWheelScreen(root, alive) {
  const state = { scope: "watch", sort: "ticker", filters: { spread: false, oi: false, noEarn: false, hasAccept: false }, data: null,
    chain: {}, busy: false, msg: "" };
  const holder = h("div", {});
  const head = h("div", { class: "page-head" }, h("div", {},
    h("h1", {}, "接货型卖 put · 筛选", h("span", { class: "sub" }, "只并排事实")),
    h("p", { class: "lede" }, "30 只标的里「卖 put 好不好做」的客观事实：账本里的 put 梯子（点差 / OI / 年化 / 占资金 / 财报）、日线统计（回撤 / 波动 / 21 日最差），"
      + "以及按你的接货价选出的合约。不排名、不推荐、不给综合分；「愿不愿意持有」是你的判断，这里只存你给的名单和接货价、只供显示。")),
    helpLink("wheel"));
  const warn = h("p", { class: "callout warn" }, "「权利金重放」把今天的权利金放回过去两年的行情：多数标的这两年在涨、30 天窗口互不重叠的样本只有二十来个，偏乐观，不是预测。"
    + "梯子是账本里约 30 天的一档，取链层才是接货账本用的 55~90 天窗口。点差是收盘后记的，盘中可能更窄。");

  async function load() {
    state.data = await api(`/api/wheel/screen?scope=${state.scope}`);
    if (!alive()) return;
    paint();
  }

  async function savePatch(patch) {
    try { await api("/api/settings", { body: patch }); state.msg = ""; } catch (e) { state.msg = `保存失败：${e.message}`; }
    await load();
  }

  async function pullPrices() {
    if (state.busy) return;
    state.busy = true; state.msg = "正在取日线…"; paint();
    try {
      const d = await api(`/api/wheel/screen/prices?tickers=${encodeURIComponent((state.data.tickers || []).join(","))}`);
      if (d.retry_after_sec) state.msg = `被 Yahoo 限流，约 ${d.retry_after_sec} 秒后再点；已取到的照常显示。`;
      else state.msg = "行情统计已更新。";
    } catch (e) { state.msg = `取行情失败：${e.message}`; }
    state.busy = false;
    await load();
  }

  async function pullChain(t) {
    state.chain[t] = { ok: false, reason: "loading" }; paint();
    try { state.chain[t] = await api(`/api/wheel/screen/chain/${encodeURIComponent(t)}`); }
    catch (e) { state.chain[t] = { ok: false, reason: "request_failed", detail: e.message }; }
    paint();
  }

  function paint() {
    const d = state.data;
    if (!d) return;
    if (!d.available) { fill(holder, panel(null, {}, h("div", { class: "empty" }, d.state_reason === "demo" ? "演示模式不展示这张表。" : `不可用：${d.state_reason || "—"}`))); return; }
    let rows = d.rows.filter((r) => {
      const x = r.ladder?.rungs?.["0.20"];
      if (state.filters.spread && !(x && x.spread_ok)) return false;
      if (state.filters.oi && !(x && x.oi_ok)) return false;
      if (state.filters.noEarn && r.earnings_in_ladder_window !== false) return false;
      if (state.filters.hasAccept && !r.accept_price) return false;
      return true;
    });
    rows = sortRows(rows, state.sort);
    const scopeSel = h("select", { onchange: (e) => { state.scope = e.target.value; load(); } },
      h("option", { value: "watch", selected: state.scope === "watch" }, `我的名单（${(d.watch || []).length} 只）`),
      h("option", { value: "all", selected: state.scope === "all" }, "全部 30 只"));
    const sortSel = h("select", { onchange: (e) => { state.sort = e.target.value; paint(); } },
      Object.entries(SORTS).map(([k, v]) => h("option", { value: k, selected: state.sort === k }, `排序：${v.label}`)));
    const watchIn = h("input", { type: "text", value: (d.watch || []).join(", "), size: 28, "aria-label": "名单（逗号分隔）" });
    const box = (key, label) => h("label", { class: "small" }, h("input", { type: "checkbox", checked: state.filters[key],
      onchange: (e) => { state.filters[key] = e.target.checked; paint(); } }), ` ${label} `);
    const controls = h("div", { class: "toolbar" }, scopeSel, sortSel, h("button", { class: "btn primary", type: "button", disabled: state.busy, onclick: pullPrices },
      "取行情统计（回撤 / 波动 / 21 日最差）"), h("span", { class: "small muted" }, state.msg || `行情已缓存 ${d.stats_cached.length} 只`));
    const filters = h("div", { class: "toolbar" }, h("span", { class: "small muted" }, "只看："),
      box("spread", "20Δ 点差合格"), box("oi", "20Δ OI 合格"), box("noEarn", "财报不在梯子到期前"), box("hasAccept", "已给接货价"));
    const nameEdit = h("div", { class: "toolbar" }, h("span", { class: "small muted" }, "我的名单："), watchIn,
      h("button", { class: "btn", type: "button", onclick: () => savePatch({ wheel_watch: watchIn.value.split(/[,\s]+/).filter(Boolean) }) }, "保存名单"),
      h("span", { class: "small muted" }, `留空 = 接货账本现行名单（${(d.wheel_universe || []).join(" / ")}）`));
    const cols = [
      { label: "标的", render: (r) => h("span", {}, h("b", {}, r.ticker), r.in_wheel_universe ? h("span", { class: "chip", title: "在接货账本的名单里" }, "账本") : null) },
      { label: "距 52 周高 / 3 月高", cls: "r", render: (r) => (r.stats?.ok ? `${sPct(r.stats.dd52_pct)} / ${sPct(r.stats.dd3m_pct)}` : h("span", { class: "muted" }, r.stats ? "不够" : "—")) },
      { label: "近 1 年波动", cls: "r", render: (r) => (r.stats?.ok ? `${num(r.stats.vol1y_pct, 0)}%` : "—") },
      { label: "21 日最差 / 跌≥10% 频率", cls: "r", render: (r) => (r.stats?.ok ? `${sPct(r.stats.worst21_pct)} / ${num(r.stats.p21_le_m10_pct, 0)}%` : "—") },
      { label: "20Δ put（账本梯子）", render: (r) => rungCell(r, "0.20") },
      { label: "30Δ put", render: (r) => rungCell(r, "0.30") },
      { label: "财报在梯子到期前", render: (r) => (r.earnings_in_ladder_window === true ? "是" : r.earnings_in_ladder_window === false ? "否" : h("span", { class: "muted" }, "待验证")) },
      { label: "你的接货价", render: (r) => {
        const inp = h("input", { type: "number", step: "0.01", min: "0", size: 7, value: r.accept_price ?? "", placeholder: "占位",
          "aria-label": `${r.ticker} 接货价`, onchange: (e) => savePatch({ wheel_accept: { [r.ticker]: e.target.value === "" ? null : Number(e.target.value) } }) });
        return h("span", {}, inp, r.accept_distance_pct !== null && r.accept_distance_pct !== undefined ? h("span", { class: "sub" }, ` 离现价 ${sPct(r.accept_distance_pct)}`) : null);
      } },
      { label: "", render: (r) => h("button", { class: "btn", type: "button", onclick: () => pullChain(r.ticker) }, "按接货价取链") },
    ];
    const detail = Object.entries(state.chain).map(([t, c]) => h("div", { class: "cmp" }, h("b", {}, t), chainDetail(c)));
    fill(holder, controls, filters, nameEdit,
      panel(`${rows.length} / ${d.rows.length} 只`, { sub: `账本 ${d.as_of || "—"}（${d.scope === "all" ? "全部" : "我的名单"}）` }, table(cols, rows)),
      detail.length ? panel("按接货价取链的结果", { sub: "接货账本同一条选合约规则（期限窗口 + 不高于接货价的最高合格档）" }, ...detail) : null);
  }

  fill(root, head, warn, holder);
  await load();
}
