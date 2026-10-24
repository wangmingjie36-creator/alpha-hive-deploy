// 对冲账本：组合 Greeks 的纸面 SPY 覆盖（持仓 / 现金 / 成交 / 净值 / 每日 β·Delta 带位）。全部只读，不写账本。
// 历史只显示能自证定价场次的记录（服务端经 portfolio_greeks.load_history 放行）；被拦下的只给条数与日期范围。
import { api, h, helpLink, num, money, pctPts, panel, table, isNum, fill } from "../lib.js";
import { lines, C } from "../charts.js";
import { renderTailHedge } from "./hedge_tail.js";

const sMoney = (v, dp = 0) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}$${num(Math.abs(v), dp)}` : "—");
const sPct = (v, dp = 2) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), dp)}%` : "—");
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const kpi = (k, v, cls = "", sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: `v ${cls}` }, v),
  sub ? h("div", { class: "small muted" }, sub) : null);
const BAND = { inside: "带内", above: "出带（偏多）", below: "出带（偏空）", unknown: "判断不了（数据不全）", empty: "没有任何行" };
const ACTION = { hold: "不动", sell_spy: "卖出 SPY", buy_spy: "买入 SPY" };
const SIDE_SHORT = { sell_spy: "卖出", buy_spy: "买入" };
// 来源键取自 portfolio_greeks._default_mark：cboe_close / cboe_intraday / twelve_data_bar / <src>_bar（v0.45.459 二次检查：初版写成 twelve_data_close，从不匹配）
const PX_SRC = { cboe_close: "CBOE 收盘", cboe_intraday: "CBOE 盘中", twelve_data_bar: "Twelve Data 日线收盘", finnhub_close: "Finnhub 收盘（CBOE 取不到时的第三来源）", demo: "演示数据" };
const pxSrc = (s) => PX_SRC[s] || (String(s || "").endsWith("_bar") ? `${String(s).slice(0, -4)} 日线收盘` : (s || "—"));
const REASON = {
  no_provenance: "记录不自证定价场次（v0.45.423 之前的格式）", not_its_session_close: "证明说价不是那一场的收盘",
  unpriced: "净值行没有净值", date_mismatch: "审计文件日期对不上", unreadable: "坏行（读不了）", not_an_object: "不是对象",
  no_date: "没有日期", intraday_run: "盘中跑的记录（价是实时价、不是收盘）",
};
const KIND = { equity: "净值", trades: "成交", audits: "审计" };
// 逐票 Delta 对冲（v0.45.526）：原因码取自 delta_hedge.REASONS（tests/test_delta_hedge.py 双向核对），状态取自 decide()
const D_STATUS = { hedge: "要对冲", inside: "带内，不动", exit: "跨式已平，清对冲", flat: "—", unknown: "决定做不出来" };
const D_REASON = {
  nav_unavailable: "NAV 不可得", option_delta_unknown: "跨式腿有一行 Delta 算不出（缺报价 / 缺价）",
  price_missing: "取不到这只的价", price_stale: "价不是当天那一场的", held_shares_unknown: "对冲持仓账里股数读不出来",
};
const D_MODE = { shadow: "只算不成交（shadow）", paper: "纸面成交", off: "关闭" };
const shares = (v) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), 0)} 股` : "—");

function latestPanel(L, rules) {
  if (!L) return panel("最近一天", {}, h("div", { class: "empty" }, "还没有放行的审计记录。"));
  const a = L.aggregate || {}, band = a.band || {}, pn = a.pct_nav || {}, cov = a.coverage || {}, rec = L.recommendation || {};
  const bw = isNum(band.band_pct) ? band.band_pct : rules.beta_delta_band_pct;
  const head = h("p", { class: "lede" },
    "合并组合 β·Delta ", h("b", { class: tone(L.beta_dd_pct) }, sPct(L.beta_dd_pct)), ` NAV（带 ±${num(bw, 0)}%）→ `, h("b", {}, BAND[a.band_status] || a.band_status || "—"), "。",
    L.executed
      ? h("span", {}, ` 已按收盘成交 ${shares(L.executed_shares)} SPY @ ${money(L.spy_price)}，交易后 `, h("b", {}, sPct(L.beta_dd_pct_after)), `（${BAND[L.band_status_after] || "—"}）。`)
      : h("span", {}, ` ${ACTION[rec.action] || rec.action || "—"}${L.execution_blocked ? `（拒绝成交：${L.execution_blocked}）` : ""}。`));
  const cell = (k, v) => h("div", {}, h("div", { class: "k" }, k), h("div", { class: "v" }, v));
  return panel(`最近一天（${L.date}）`, { sub: `SPY ${money(L.spy_price)}（${pxSrc(L.spy_mark?.source)} @ ${L.spy_mark?.session || "—"}）· 合并 NAV ${money(a.nav, 0)}` },
    head,
    h("div", { class: "sd-greeks" },
      cell("$Delta", sPct(pn.dollar_delta)), cell("β·$Delta", sPct(pn.beta_dollar_delta)),
      cell("Γ（1% 移动）", sPct(pn.gamma_dollar_per_1pct, 3)), cell("Vega / 波动率点", sPct(pn.vega_dollar_per_pt, 3)),
      cell("Theta / 日", sPct(pn.theta_dollar_per_day, 3))),
    h("p", { class: "small muted" }, `以上都是占合并 NAV 的百分比。覆盖 ${cov.n_rows ?? "—"} 行：缺价 ${cov.n_price_missing ?? "—"} · 缺报价 ${cov.n_quote_missing ?? "—"} · 缺 β ${cov.n_beta_missing ?? "—"}`
      + `${a.partial ? " · 数据不全（不全时一律不对冲）" : ""}${a.vega_alert ? " · vega 超告警线" : ""}${a.gamma_alert ? " · gamma 超告警线" : ""}`),
    rec.reason ? h("p", { class: "small" }, h("span", { class: "muted" }, "规则给出的理由："), rec.reason) : null);
}

// 成本估算（v0.45.638）：固定 bps 的主观假设，不是点差 / 冲击模型；不进决策、不扣现金。数字全由服务端算好（前端只排版）。
// shadow 没有持仓账：逐日「若每天都拉回 0」的隐含换手才是日常口径；决策表里那一天的合计是「若从空仓开始」的上界。
function costBlock(D, mode) {
  const T = D.turnover || {};
  const bps = isNum(D.cost_bps) ? D.cost_bps : T.cost_bps;
  const bits = [];
  if (isNum(D.trade_notional_usd)) {
    bits.push(h("p", { class: "small" }, `${D.latest_date} 的决策：${D.n_trades ?? 0} 笔，成交名义 ${money(D.trade_notional_usd, 0)}`
      + (isNum(D.trade_notional_pct_nav) ? `（${num(D.trade_notional_pct_nav, 1)}% NAV）` : "")
      + `，估算成本 ${money(D.est_cost_usd, 2)}`
      + (mode === "shadow" ? "。shadow 没有持仓账，这是「若今天从空仓开始对冲」的上界，不是日常换手。" : "。")
      + (isNum(D.executed_notional_usd) ? ` 其中真成交名义 ${money(D.executed_notional_usd, 0)}、估算成本 ${money(D.executed_est_cost_usd, 2)}。` : "")));
  }
  const rows = (T.rows || []);
  if (rows.length) {
    const cols = [
      { label: "日期", render: (r) => r.date },
      { label: "口径", render: (r) => (r.kind === "build" ? "建仓（不进日均）" : "相邻两日目标变化") },
      { label: "只数", cls: "r", render: (r) => r.n_names + (r.n_skipped ? `（另 ${r.n_skipped} 只判不了）` : "") },
      { label: "成交名义", cls: "r", render: (r) => money(r.notional_usd, 0) },
      { label: "% NAV", cls: "r", render: (r) => (isNum(r.notional_pct_nav) ? `${num(r.notional_pct_nav, 1)}%` : "—") },
      { label: "估算成本", cls: "r", render: (r) => money(r.est_cost_usd, 2) },
      { label: "bps NAV", cls: "r", render: (r) => (isNum(r.est_cost_bps_nav) ? num(r.est_cost_bps_nav, 2) : "—") }];
    bits.push(h("h3", {}, "估算成本与隐含换手"),
      h("p", { class: "small muted" }, `按每笔成交 ${isNum(bps) ? num(bps, 1) : "—"} bps 估算（主观假设，没有校准）。「隐含换手」= 若每天都把每只标的拉回目标股数，相邻两个扫描日之间要成交的名义；`
        + `日均 ${isNum(T.avg_notional_pct_nav) ? `${num(T.avg_notional_pct_nav, 1)}% NAV` : "—"}、约 ${isNum(T.avg_est_cost_bps_nav) ? `${num(T.avg_est_cost_bps_nav, 2)} bps NAV` : "—"}/天（${T.n_rebalance_days ?? 0} 个换日）。`),
      table(cols, rows));
  }
  return bits;
}

function deltaPanel(D, rules) {
  if (!D) return null;
  const mode = D.mode || rules.delta_hedge_mode;
  const sub = `${D_MODE[mode] || mode || "—"} · 每只标的 |跨式腿净 $Delta + 对冲市值| 超过 NAV 的 ${num(D.band_pct, 1)}% 才动手，${D.rebalance_to === "edge" ? "拉回带边" : "拉回 0"} · 用该标的自己的股票，不经 β`;
  if (!D.available) {
    return panel("逐票 Delta 对冲", { sub }, h("div", { class: "empty" },
      "放行的审计里还没有这一节（这个功能上线前的审计没有，或跨式账本里没有持仓）。"));
  }
  const nameCols = [
    { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: "跨式腿 $Delta", cls: "r", render: (r) => sMoney(r.option_dd) },
    { label: "% NAV", cls: "r", render: (r) => sPct(r.option_pct_nav, 1) },
    { label: "目标对冲股数", cls: "r", render: (r) => shares(r.target_shares) },
    { label: "今日成交", cls: "r", render: (r) => (r.trade_shares ? shares(r.trade_shares) : "—") },
    { label: "状态", render: (r) => (r.status === "unknown"
      ? h("span", { class: "chip warn", title: r.reason || "" }, `未决：${D_REASON[r.reason] || r.reason || "—"}`)
      : (D_STATUS[r.status] || r.status)) },
  ];
  const names = (D.names || []).filter((r) => r.status !== "flat");
  const posCols = [
    { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: "持股", cls: "r", render: (r) => shares(r.shares) },
    { label: "均价", cls: "r", render: (r) => money(r.avg_price) },
    { label: "标记价", cls: "r", render: (r) => money(r.last_price) },
    { label: "标记日", render: (r) => r.last_mark_date || "—" },
  ];
  const tradeCols = [
    { label: "日期", render: (r) => r.date }, { label: "标的", render: (r) => h("b", {}, r.ticker) },
    { label: "成交", cls: "r", render: (r) => shares(r.shares) }, { label: "价", cls: "r", render: (r) => money(r.price) },
    { label: "原因", render: (r) => D_STATUS[r.reason] || r.reason || "—" },
    { label: "成交后持股", cls: "r", render: (r) => shares(r.shares_after) },
  ];
  const lastBits = [];
  lastBits.push(h("p", { class: "small muted" }, `${D.latest_date} 的决策 · 合并 NAV ${money(D.nav, 0)} · 要对冲 ${D.n_hedge ?? 0} 只 / 带内 ${D.n_inside ?? 0} 只 / 未决 ${D.n_undecided ?? 0} 只。`
    + (mode === "shadow" ? " shadow 没有对冲持仓账：「今日成交」是「若今天从空仓开始对冲」要成交的量，「目标对冲股数」才是把跨式 Delta 全抵掉需要持有的股数。" : "")));
  if (D.nav_basis && D.nav_basis !== "full") {
    lastBits.push(h("p", { class: "callout warn" }, `带宽用的 NAV 不含逐票对冲账里 ${(D.nav_unmarked || []).join("、")} 的盈亏（这几只持仓标的当天没有可用的价）：`
      + "它只是对冲仓的盈亏、相对合并 NAV 是个零头；但合并 NAV 本身这一天算不出来，所以上面 SPY 带的判断是「判断不了」。"));
  }
  if ((D.undecided || []).length) {
    lastBits.push(h("p", { class: "callout warn" }, `${D.undecided.length} 只决定做不出来（只影响这几只，别的照常）：`
      + D.undecided.map((u) => `${u.ticker}（${D_REASON[u.reason] || u.reason}）`).join("；")));
  }
  for (const [tk, why] of Object.entries(D.blocked || {})) lastBits.push(h("p", { class: "callout warn" }, `${tk} 未成交：${why}`));
  for (const [k, v] of Object.entries(D.errors || {})) lastBits.push(h("p", { class: "callout warn" }, `${k} 读不了：${v}`));
  const account = D.opened
    ? [h("h3", {}, "逐票对冲账"), h("p", { class: "small muted" }, `起始现金 0，开账 ${D.starting_date || "—"} · 现金 ${money(D.cash, 0)}`),
      (D.positions || []).length ? table(posCols, D.positions) : h("div", { class: "empty" }, "空仓。"),
      h("h3", {}, "成交（放行的）"),
      (D.trades || []).length ? table(tradeCols, D.trades) : h("div", { class: "empty" }, "放行的记录里还没有成交。")]
    : [];
  return panel("逐票 Delta 对冲", { sub }, ...lastBits,
    names.length ? table(nameCols, names) : h("div", { class: "empty" }, "跨式账本里没有持仓。"), ...costBlock(D, mode), ...account,
    h("p", { class: "note small muted" }, "没有点差 / 冲击成本模型（单只股票比 SPY 贵；上面的成本只是固定 bps 的估算）；只对冲 Delta，Gamma / Vega 不对冲；带与拉回方式是 v1 主观常数，不是校准结果，也没有证据说明逐票对冲后更好。"));
}

function gatePanel(g) {
  const tot = g.excluded_total || {};
  const n = Object.values(tot).reduce((s, v) => s + (v || 0), 0);
  const rows = [];
  for (const [kind, by] of Object.entries(g.excluded || {})) {
    for (const [reason, cnt] of Object.entries(by || {})) {
      const rg = (g.excluded_range || {})[kind];
      rows.push([KIND[kind] || kind, REASON[reason] || reason, cnt, rg ? `${rg[0]} ~ ${rg[1]}` : "—"]);
    }
  }
  const fv = g.first_verified || {};
  return panel("历史闸", { sub: `只显示能自证定价场次的记录 · 测量起点 ${g.measurement_start || "—"}` },
    h("p", { class: "small" }, `放行的最早日期：净值 ${fv.equity || "—"} · 成交 ${fv.trades || "—"} · 审计 ${fv.audits || "—"}。`,
      n ? `被拦下 ${n} 条：不显示，也不进上面的图表和表格。注意账户卡片例外：当前持股、均价、现金是开账以来全部成交累积出来的（含被拦那段），所以「累计盈亏」覆盖整个开账期，而图表只从放行起点画起、起点不是 0。` : "没有被拦下的记录。"),
    rows.length ? table([{ label: "种类", render: (r) => r[0] }, { label: "原因", render: (r) => r[1] },
      { label: "条数", cls: "r", render: (r) => r[2] }, { label: "日期范围", render: (r) => r[3] }], rows) : null,
    rows.length ? h("p", { class: "note" }, "这些旧记录的标的价 / SPY 成交价比当天晚一个交易日、且记录本身不说明是哪一场的价，按既定决定原样保留、不重算；放在一起会把脏数与干净数静默混算，所以这里只给计数。") : null);
}

export async function renderHedge(root, alive) {
  const d = await api("/api/hedge");
  if (!alive()) return;
  const rules = d.rules || {};
  const head = h("div", { class: "page-head" }, h("div", {},
    h("h1", {}, "对冲账本", h("span", { class: "sub" }, d.source === "demo" ? "演示数据" : (d.as_of ? `账本 ${d.as_of}` : ""))),
    h("p", { class: "lede" }, `组合 Greeks 的纸面 ${rules.hedge_instrument || "SPY"} 覆盖（模拟成交，不连券商）：每个扫描日收盘后，把股票纸面组合和跨式腿合起来算 β·Delta 占合并 NAV 的百分比，出 ±${num(rules.beta_delta_band_pct, 0)}% 带就用 ${rules.hedge_instrument || "SPY"} 股票拉回带边，带内不动。 `, helpLink("hedge"))));
  if (!d.data_available) {
    fill(root, head, panel(null, {}, h("div", { class: "empty" }, `没找到对冲账本目录：${d.state_dir?.path || "—"}`),
      Object.entries(d.errors || {}).map(([k, v]) => h("p", { class: "small muted" }, `${k}：${v}`)),
      h("p", { class: "small muted" }, "桌面程序用 --reset 重选数据根；终端运行要设 ALPHA_HIVE_HOME。")));
    return;
  }
  const a = d.account || {}, g = d.gate || {}, L = d.latest;
  const warns = [];
  if (d.freshness?.stale) warns.push(`账本停在 ${d.as_of}；按交易日历，${d.freshness.expected_as_of} 收盘后的扫描应已更新（扫描没跑、失败，或还在跑）。`);
  if (a.identity_ok === false) warns.push(`账目对不上：现金 + 持仓市值与 ${a.identity_checked_against} 的净值行差 ${sMoney(a.identity_gap_usd, 2)}。`);
  if (!d.opened && !Object.keys(d.errors || {}).length) warns.push("对冲账本还没有任何记录（没有 meta.json）：组合 Greeks 还没在这个数据根上执行过对冲记账，或数据根选错了。下面全是空的。");
  if (L && d.latest_is_as_of === false) warns.push(`最近一份放行的审计是 ${L.date}，不是账本最后一天 ${d.as_of}——那天这一轮没跑完（审计最后写）、审计文件缺了，或被历史闸拦下了（看下面「历史闸」）。`);
  if (a.mark_stale) warns.push(`持仓标记停在 ${a.mark_date}，账本已到 ${d.as_of}：之后那几天 SPY 价取不到，市值、净值、浮动都按 ${a.mark_date} 的价算。`);
  for (const [k, v] of Object.entries(d.errors || {})) warns.push(`${{ positions: "持仓文件", meta: "meta.json", history: "历史" }[k] || k} 读不了：${v}`);
  for (const [kind, day] of g.regressions || []) warns.push(`${KIND[kind] || kind} 在已经有放行记录之后又出现了无定价证明的记录（${day}）——写入方退化了。`);
  const pc = L?.price_check || {};
  if (pc.hedge_undecided) {
    // 点名缺价的标的（v0.45.526）：此前只说「1 price missing」，不说是哪一只
    const who = [...(pc.stale || []).map((x) => `${x.ticker}（价属于 ${x.session || "?"}，来源 ${pxSrc(x.source)}，看到 ${money(x.seen_price)}，不是当天收盘）`),
      ...(pc.unpriced || []).map((x) => `${x.ticker}（取不到价）`)];
    warns.push(`${L.date} 数据不全，对冲决定做不出来：${pc.hedge_undecided}${who.length ? `——缺价的是：${who.join("；")}` : ""}`);
  }
  if ((pc.gaps || []).length) warns.push(`${L.date} 另缺：${pc.gaps.join("；")}`);
  // 买价为 0 的腿不是「缺」：Greeks 取 CBOE 模型值（没有 mid）、已计入带位——说出来，别让它悄悄进决定
  if (pc.n_bid_zero) warns.push(`${L.date} 带位含 ${pc.n_bid_zero} 张买价为 0 的腿（${(pc.bid_zero || []).join("、")}）：用的是 CBOE 模型 Greeks，没有中间价；这些腿没有进压力表。`);
  // 被排除的标的不是「没有敞口」：点名 + 估算额，让它变大时看得见（v0.45.545）
  for (const x of L?.excluded_from_hedge || []) {
    warns.push(`${L.date} ${x.ticker} 不进对冲计算（配置里排除）：股票 ${x.n_stock_positions} 笔` +
      (x.stock_usd_est != null ? `，按建仓价估 ${sMoney(x.stock_usd_est, 0)}（${sPct(x.stock_pct_nav_est)} NAV）` : "") +
      (x.n_straddles ? `，跨式 ${x.n_straddles} 笔` : "") + "；敞口仍在，SPY 覆盖不管它。");
  }

  const kpis = h("div", { class: "kpis" },
    kpi(`持有 ${a.ticker || rules.hedge_instrument || "SPY"}`, shares(a.shares), "", a.entry_date ? `自 ${a.entry_date}` : null),
    kpi("均价", money(a.avg_price)),
    kpi("标记价", money(a.mark), a.mark_stale ? "down" : "", a.mark_date ? `标记于 ${a.mark_date}` : null),
    kpi("市值", money(a.market_value, 0)), kpi("现金", money(a.cash, 0)),
    kpi("累计盈亏（= 净值）", sMoney(a.nav), tone(a.nav)),
    kpi("浮动", sMoney(a.unrealized_usd), tone(a.unrealized_usd)), kpi("已实现", sMoney(a.realized_usd), tone(a.realized_usd)));

  const tailHost = h("div");     // 尾部对冲候选（v0.45.643）：另一个接口、要现拉 SPY 期权链，不阻塞本页其余部分
  const bandEl = h("div", { class: "chart short" });
  const eqEl = h("div", { class: "chart short" });
  const days = d.days || [];
  const dayCols = [
    { label: "日期", render: (r) => r.date },
    { label: "β·Δ 交易前", cls: "r", render: (r) => h("span", { class: tone(r.beta_dd_pct) }, sPct(r.beta_dd_pct)) },
    { label: "带位", render: (r) => BAND[r.aggregate?.band_status] || r.aggregate?.band_status || "—" },
    { label: "动作", render: (r) => (r.executed ? `${ACTION[r.recommendation?.action] || r.recommendation?.action} ${shares(r.executed_shares)}` : (ACTION[r.recommendation?.action] || r.recommendation?.action || "—")) },
    { label: "β·Δ 交易后", cls: "r", render: (r) => (r.executed ? sPct(r.beta_dd_pct_after) : "—") },
    { label: "SPY", cls: "r", render: (r) => money(r.spy_price) },
  ];
  // 理由那一句（规则的原文）只是把这几列连起来说一遍：放进行的悬停提示，不占一列（v0.45.459 实测：占一列时日期 / 金额被挤到折行）
  const tradeCols = [
    { label: "日期", render: (r) => h("span", { title: r.reason || "" }, r.date) },
    { label: "方向", render: (r) => SIDE_SHORT[r.action] || r.action || "—" },
    { label: "股数", cls: "r", render: (r) => shares(r.shares) },
    { label: "价格", cls: "r", render: (r) => h("span", {}, money(r.price), h("span", { class: "sub" }, `${pxSrc(r.price_source)} @ ${r.price_session || "—"}`)) },
    { label: "交易前 β·Δ", cls: "r", render: (r) => sPct(r.beta_dd_pct_before) },
    { label: "拉回到", cls: "r", render: (r) => (isNum(r.target_usd) && isNum(r.nav) && r.nav ? sPct((r.target_usd / r.nav) * 100, 1) : "—") },
    { label: "交易后持股", cls: "r", render: (r) => shares(r.shares_after) },
  ];

  fill(root, head,
    warns.map((w) => h("p", { class: "callout warn" }, w)),
    panel("账户", { sub: `起始现金 0，所以净值就是开账（${a.starting_date || "—"}）以来的累计盈亏 · 账目核对 ${a.identity_ok === true ? `✓（与 ${a.identity_checked_against} 净值行一致）` : a.identity_ok === false ? "✗" : `—（${a.identity_reason || "核对不了"}）`}` }, kpis),
    h("div", { style: { height: "16px" } }),
    latestPanel(L, rules),
    h("div", { style: { height: "16px" } }),
    h("div", { class: "grid halves" },
      panel("β·Delta 与对冲带", { sub: `占合并 NAV 的 %；灰带 = ±${num(rules.beta_delta_band_pct, 0)}%；实线 = 交易前、虚线 = 交易后` },
        days.length ? bandEl : h("div", { class: "empty" }, "还没有放行的审计记录。")),
      panel("对冲腿累计盈亏", { sub: "每个扫描日收盘后的净值（= 现金 + SPY 市值）" },
        (d.equity || []).length ? eqEl : h("div", { class: "empty" }, "还没有放行的净值记录。"))),
    h("div", { style: { height: "16px" } }),
    deltaPanel(d.delta, rules),
    h("div", { style: { height: "16px" } }),
    tailHost,
    h("div", { style: { height: "16px" } }),
    panel("成交", { sub: `${(d.trades || []).length} 笔（放行的）· 按收盘价成交，SPY 点差约 1bp 未建模 · 「拉回到」是占合并 NAV 的带边 · 鼠标停在日期上看规则原文` },
      (d.trades || []).length ? table(tradeCols, d.trades) : h("div", { class: "empty" }, "放行的记录里还没有成交。")),
    h("div", { style: { height: "16px" } }),
    panel("每日带位", { sub: `${days.length} 天（放行的），新的在上` }, days.length ? table(dayCols, [...days].reverse()) : h("div", { class: "empty" }, "—")),
    h("div", { style: { height: "16px" } }),
    gatePanel(g),
    h("p", { class: "note small muted", style: { marginTop: "14px" } }, `对冲只看 β·Delta：vega 超 ${num(rules.vega_alert_pct, 1)}% / gamma 超 ${num(rules.gamma_alert_pct, 1)}% NAV 只告警、不对冲；缺价 / 缺 β / 缺报价时一律不对冲。观察项：不进评分、不进股票纸面组合、不构成投资建议。`));

  renderTailHedge(tailHost, alive);
  const c = C();
  if (days.length) {
    const bw = rules.beta_delta_band_pct;
    // 纵轴把 ±带边与 0 线都包进来：自动缩放时数据都在带的一侧，另一条带边与 0 线会落在画布外（v0.45.459 二次检查）
    const vals = days.flatMap((r) => [r.beta_dd_pct, r.beta_dd_pct_after]).filter(isNum);
    const span = Math.ceil((Math.max(...vals.map(Math.abs), isNum(bw) ? bw : 0, 1) * 1.1) / 5) * 5;   // 取整到 5%
    lines(bandEl, { x: days.map((r) => r.date.slice(5)), yFmt: (v) => sPct(v, 1), yMin: -span, yMax: span,
      series: [{ name: "交易前", data: days.map((r) => r.beta_dd_pct), color: c.ink, width: 2, symbol: true },
        { name: "交易后", data: days.map((r) => (r.executed ? r.beta_dd_pct_after : r.beta_dd_pct)), color: c.ink3, dash: true }],
      bands: isNum(bw) ? [{ lo: -bw, hi: bw, label: "带内" }] : [],
      hmarks: [{ value: 0, label: "", color: c.lineStrong, type: "solid" }] });
  }
  if ((d.equity || []).length) {
    lines(eqEl, { x: d.equity.map((e) => e.date.slice(5)), yFmt: (v) => sMoney(v),
      series: [{ name: "累计盈亏", data: d.equity.map((e) => e.nav), color: c.ink, width: 2, symbol: d.equity.length < 20 }],
      hmarks: [{ value: 0, label: "", color: c.lineStrong, type: "solid" }] });
  }
}
