// 标的页：水平 / 希腊值 / 期限 / 卖权 / 价格。一次现算（服务端 60 秒缓存）喂全部子页。
import { api, h, num, money, pct, pctPts, usdShort, signedPct, distPct, signChip, seg, table, panel, isNum,
  downloadCSV, pref, setPref, timeAgo, SIGN_LABEL, CURVE_LABEL, ROUTE_LABEL, STRUCT_LABEL, TENOR_LABEL,
  EARN_LABEL, SOURCE_LABEL, fill, plain } from "../lib.js";
import { strikeBars, gammaCurve, lines, columns, payoff, C } from "../charts.js";
import { state, tickers, refreshMeta } from "../app.js";

const TABS = [["levels", "水平"], ["greeks", "希腊值"], ["term", "期限"], ["sell", "卖权"], ["price", "价格"]];
const VIEWS = [["next_expiry", "下一到期"], ["le_45dte", "≤45 天（路由用）"], ["full", "全部到期"]];

export async function renderTicker(root, t, tab, alive, force = false) {
  const d = await api(`/api/live/${encodeURIComponent(t)}${force ? "?force=1" : ""}`);
  if (!alive()) return;
  const main = h("div", {});
  fill(root, h("div", { class: "with-side" }, sidebar(t, `#/t/{T}/${tab}`), main));

  const favs = state.meta?.settings?.favorites || [];
  const inWatch = (state.meta?.watchlist?.watchlist || []).includes(t);
  const favBtn = inWatch ? null : h("button", { class: "btn", type: "button", onclick: async () => {
    const next = favs.includes(t) ? favs.filter((x) => x !== t) : [...favs, t];
    await api("/api/settings", { body: { favorites: next } });
    await refreshMeta(); renderTicker(root, t, tab, alive);
  } }, favs.includes(t) ? "移出自选" : "加入自选");
  const refresh = h("button", { class: "btn", type: "button", onclick: (e) => {
    e.target.disabled = true; e.target.textContent = "拉取中…"; renderTicker(root, t, tab, alive, true);
  } }, "重新拉取");

  if (!d.data_available) {
    fill(main, h("div", { class: "page-head" }, h("h1", {}, t), h("div", { class: "controls" }, favBtn, refresh)),
      panel(null, {}, h("div", { class: "empty" }, "这只标的现在拿不到期权链。"),
        h("p", { class: "callout warn" }, `原因：${d.reason || "未知"}`),
        h("p", { class: "small muted" }, "CBOE 延迟行情只覆盖挂牌期权的美股 / ETF；指数请用 CBOE 写法（如 _SPX）。")));
    return;
  }

  const zg = d.views?.le_45dte?.zero_gamma || {};
  const S = d.underlying_price;
  const head = h("div", { class: "page-head" },
    h("div", {},
      h("h1", {}, t, h("span", { class: "sub num" }, money(S))),
      h("div", { class: "fresh" },
        signChip(zg.sign_at_spot),
        h("span", {}, "报价 ", h("b", {}, SOURCE_LABEL[d.underlying_price_source] || d.underlying_price_source || "—")),
        h("span", {}, "最后成交 ", h("b", { class: "num" }, (d.payload_last_trade_time || "—").replace("T", " "))),
        h("span", {}, "IV30 ", h("b", { class: "num" }, isNum(d.iv30) ? `${num(d.iv30, 1)}%` : "—")),
        h("span", {}, "OI 为前一交易日"),
        h("span", {}, `取于 ${timeAgo(d.cache_age_sec)}`))),
    h("div", { class: "controls" }, favBtn, refresh));

  const tabs = h("nav", { class: "tabs", "aria-label": "标的子页" },
    TABS.map(([k, label]) => h("a", { href: `#/t/${t}/${k}`, "aria-current": k === tab ? "page" : null }, label)));
  const body = h("div", {});
  fill(main, head, tabs, body);
  const draw = { levels, greeks, term, sell, price }[tab] || levels;
  await draw(body, d, t, alive);
}

export function sidebar(current, pattern) {
  const wl = state.meta?.watchlist?.watchlist || [];
  const favs = state.meta?.watchlist?.favorites || [];
  const link = (x, cls) => h("a", { href: pattern.replace("{T}", x), class: cls || "", "aria-current": x === current ? "true" : null }, x);
  const extra = !wl.includes(current) && !favs.includes(current) ? [h("h3", {}, "当前"), link(current)] : [];
  return h("aside", { class: "side", "aria-label": "标的列表" },
    ...extra,
    favs.length ? [h("h3", {}, "自选"), ...favs.map((x) => link(x, "fav"))] : null,
    h("h3", {}, `观察列表 · ${wl.length}`), ...wl.map((x) => link(x)));
}

// ─────────────────────────────── 水平
function levels(body, d) {
  let view = pref("levelsView", "le_45dte");
  let mode = pref("levelsMode", "net");
  let basis = "oi";
  const S = d.underlying_price;
  const c = C();

  function draw() {
    const v = d.views?.[view] || {};
    const zg = v.zero_gamma || {};
    const mj = v.majors || {};
    const rows = basis === "oi" ? (v.strikes || []) : (v.volume_strikes || []);
    const lv = [];
    (zg.crossings || []).forEach((x, i) => lv.push({ value: x, label: `零点 ${num(x)}`, color: c.ink3, type: "dashed", pos: "insideStartTop", key: `zg${i}` }));
    if (isNum(mj.net_major_pos_strike)) lv.push({ value: mj.net_major_pos_strike, label: `Major+ ${num(mj.net_major_pos_strike, 0)}`, color: c.pos, type: "dotted" });
    if (isNum(mj.net_major_neg_strike)) lv.push({ value: mj.net_major_neg_strike, label: `Major− ${num(mj.net_major_neg_strike, 0)}`, color: c.neg, type: "dotted", pos: "insideEndBottom" });
    const series = mode === "net"
      ? [{ name: "净 GEX", value: (r) => r.net_gex_usd_per_1pct }]
      : [{ name: "call GEX", value: (r) => r.call_gex_usd_per_1pct, color: c.pos },
        { name: "put GEX", value: (r) => r.put_gex_usd_per_1pct, color: c.neg }];

    const ladderEl = h("div", { class: "chart tall", role: "img", "aria-label": "按行权价的 GEX 梯子" });
    const curveEl = h("div", { class: "chart short", role: "img", "aria-label": "总 GEX 随假想现价变化的曲线" });
    const controls = h("div", { class: "controls" },
      seg(VIEWS, view, (x) => { view = x; setPref("levelsView", x); draw(); }, "期限视图"),
      seg([["net", "净值"], ["split", "call / put"]], mode, (x) => { mode = x; setPref("levelsMode", x); draw(); }, "显示方式"),
      seg([["oi", "按 OI"], ["volume", "按成交量"]], basis, (x) => { basis = x; draw(); }, "口径"));
    const csvCols = [
      { label: "strike", csv: (r) => r.strike }, { label: "net_gex_usd_per_1pct", csv: (r) => r.net_gex_usd_per_1pct },
      { label: "call_gex_usd_per_1pct", csv: (r) => r.call_gex_usd_per_1pct }, { label: "put_gex_usd_per_1pct", csv: (r) => r.put_gex_usd_per_1pct },
      { label: "net_dex_usd", csv: (r) => r.net_dex_usd }, { label: "call_oi", csv: (r) => r.call_oi }, { label: "put_oi", csv: (r) => r.put_oi }];

    fill(body, 
      h("div", { class: "grid two" },
        panel("GEX 梯子", {
          sub: `美元 / 现价每变动 1% · ${basis === "oi" ? "按持仓 OI" : "按当日成交量（仅展示：成交量看不出买卖方向）"} · 现价 ±${Math.round((d.band_pct || 0.2) * 100)}%`,
          right: h("button", { class: "btn", type: "button", onclick: () => downloadCSV(`alphabot-${d.ticker}-gex-${view}.csv`, csvCols, v.strikes || []) }, "导出 CSV"),
        }, controls, h("div", { style: { height: "10px" } }), rows.length ? ladderEl : h("div", { class: "empty" }, "这个视图没有可用合约。"),
        h("p", { class: "note" }, "正值（蓝）= 做市商被假设为净多 gamma，价格波动时倾向反向对冲、压低波动；负值（橙）相反。符号用朴素 OI 口径：call 记正、put 记负——个股上 put 侧可能整个反了，所以只作环境参考。")),
        panel("水平", { sub: `${VIEWS.find((x) => x[0] === view)[1]} · ${v.n_contracts ?? "—"} 张合约` }, levelCards(d, v, S))),
      h("div", { style: { height: "20px" } }),
      panel("Gamma 曲线", { sub: "每张合约固定自身 IV，在现价 ±20% 的假想价格上重算总 GEX；过零点就是 Zero Gamma（路由读的正是 ≤45 天视图的这条线）" },
        (v.curve?.total || []).length ? curveEl : h("div", { class: "empty" }, "没有可用于扫描的合约（需要 IV 与 OI）。")));
    if (rows.length) strikeBars(ladderEl, { rows, series, spot: S, levels: lv });
    if ((v.curve?.total || []).length) gammaCurve(curveEl, v.curve, S);
  }
  draw();
}

function levelCards(d, v, S) {
  const zg = v.zero_gamma || {};
  const mj = v.majors || {};
  const card = (k, val, detail, grade) => h("div", { class: "level" },
    h("div", { class: "k" }, k, grade ? [" ", h("span", { class: "grade", title: gradeTitle(grade) }, `证据 ${grade}`)] : null),
    h("div", { class: "v" }, val), detail ? h("div", { class: "d" }, detail) : null);
  const dist = (x) => (isNum(x) ? signedPct(distPct(x, S)) + " 距现价" : "");
  const r = d.tenors || {};
  const routeTxt = (tn) => { const rt = r[tn]?.route || {}; return `put ${ROUTE_LABEL[rt.put] || "—"} · call ${ROUTE_LABEL[rt.call] || "—"}`; };
  return h("div", { class: "levels" },
    card("现价处净 gamma", SIGN_LABEL[zg.sign_at_spot] || "不可得", `${CURVE_LABEL[zg.curve_state] || "—"} · 现价处总量 ${usdShort(zg.total_at_spot)} / 1%`),
    card("Zero Gamma（最近）", num(zg.nearest), dist(zg.nearest), "C"),
    card("下方零点 / 上方零点", `${num(zg.nearest_below)} / ${num(zg.nearest_above)}`,
      `${isNum(zg.zg_below_pct) ? "下方 " + pctPts(zg.zg_below_pct) : "下方无"} · ${isNum(zg.zg_above_pct) ? "上方 " + pctPts(zg.zg_above_pct) : "上方无"}`),
    card("净 Major+", num(mj.net_major_pos_strike), `${dist(mj.net_major_pos_strike)} · ${usdShort(mj.net_major_pos_gex)}`, "D"),
    card("净 Major−", num(mj.net_major_neg_strike), `${dist(mj.net_major_neg_strike)} · ${usdShort(mj.net_major_neg_gex)}`, "D"),
    card("call / put 单边极值", `${num(mj.call_side_extreme_strike)} / ${num(mj.put_side_extreme_strike)}`, "旧「call wall / put wall」口径，只展示", "D"),
    card("总净 GEX", usdShort(v.totals?.net_gex_usd_per_1pct), `按成交量：${usdShort(v.volume_totals?.net_gex_usd_per_1pct)}`),
    card("路由 · 月度", "", routeTxt("monthly")),
    card("路由 · 周度", "", routeTxt("weekly")));
}
function gradeTitle(g) {
  return { A: "定价数学", B: "有学术证据，幅度中等", C: "依赖模型、口径不一、无独立检验", D: "无同行评审，只展示" }[g] || "";
}

// ─────────────────────────────── 希腊值
function greeks(body, d) {
  let metric = pref("greekMetric", "dex");
  let view = pref("greekView", "le_45dte");
  let tenor = "monthly";
  const S = d.underlying_price;
  const c = C();
  const METRICS = {
    dex: { label: "DEX", key: "net_dex_usd", sub: "美元 · 持有者口径（call Δ>0、put Δ<0），与 GEX 的做市商符号不同，不能相加" },
    vanna: { label: "Vanna", key: "net_vanna_usd_per_volpt", sub: "美元 delta / 每 1 个波动率点（做市商符号）" },
    charm: { label: "Charm", key: "net_charm_usd_per_day", sub: "美元 delta / 每个日历日（做市商符号）" },
    oi: { label: "OI", key: null, sub: "持仓张数：call 在右、put 在左" },
  };
  function draw() {
    const v = d.views?.[view] || {};
    const rows = v.strikes || [];
    const m = METRICS[metric];
    const em = d.expected_move?.[tenor] || {};
    const lv = [];
    if (isNum(em.lo)) lv.push({ value: em.lo, label: `−1σ ${num(em.lo)}`, color: c.ink3, type: "dotted" });
    if (isNum(em.hi)) lv.push({ value: em.hi, label: `+1σ ${num(em.hi)}`, color: c.ink3, type: "dotted" });
    const series = metric === "oi"
      ? [{ name: "call OI", value: (r) => r.call_oi, color: c.pos }, { name: "put OI", value: (r) => -r.put_oi, color: c.neg }]
      : [{ name: m.label, value: (r) => r[m.key] }];
    const el = h("div", { class: "chart tall" });
    const smileEl = h("div", { class: "chart short" });
    const sm = d.smile?.[tenor] || [];
    fill(body, 
      panel(`${m.label} 剖面`, { sub: m.sub },
        h("div", { class: "controls" },
          seg(Object.entries(METRICS).map(([k, x]) => [k, x.label]), metric, (x) => { metric = x; setPref("greekMetric", x); draw(); }, "指标"),
          seg(VIEWS, view, (x) => { view = x; setPref("greekView", x); draw(); }, "期限视图"),
          seg([["monthly", "月度 ±1σ"], ["weekly", "周度 ±1σ"]], tenor, (x) => { tenor = x; draw(); }, "期望波动")),
        h("div", { style: { height: "10px" } }), rows.length ? el : h("div", { class: "empty" }, "这个视图没有可用合约。"),
        h("p", { class: "note" }, `±1σ 期望波动 = S·σ·√T（${em.expiry || "—"}，ATM IV ${pct(em.atm_iv)}，${em.dte ?? "—"} 天）：${money(em.move_1sigma)}。`)),
      h("div", { style: { height: "20px" } }),
      panel("IV 微笑", { sub: `${TENOR_LABEL[tenor]} 选中到期日 ${em.expiry || "—"} 的逐行权价隐含波动率` },
        sm.length ? smileEl : h("div", { class: "empty" }, "没有这个到期日的 IV。")));
    if (rows.length) strikeBars(el, { rows, series, spot: S, levels: lv, fmt: metric === "oi" ? (x) => num(Math.abs(x), 0) : usdShort });
    if (sm.length) {
      lines(smileEl, { xType: "value", x: null, yFmt: (x) => pct(x, 0),
        series: [
          { name: "call IV", data: sm.filter((r) => isNum(r.call_iv)).map((r) => [r.strike, r.call_iv]), color: c.pos, symbol: true },
          { name: "put IV", data: sm.filter((r) => isNum(r.put_iv)).map((r) => [r.strike, r.put_iv]), color: c.neg, symbol: true }],
        vmarks: isNum(S) ? [{ value: S, label: `现价 ${num(S)}` }] : [] });
    }
  }
  draw();
}

// ─────────────────────────────── 期限
function term(body, d) {
  const ex = (d.expiries || []).filter((e) => isNum(e.dte));
  const gexEl = h("div", { class: "chart short" });
  const ivEl = h("div", { class: "chart short" });
  const vw = d.views || {};
  const signs = VIEWS.map(([k, label]) => ({ k, label, sign: vw[k]?.zero_gamma?.sign_at_spot, tot: vw[k]?.totals?.net_gex_usd_per_1pct, n: vw[k]?.n_contracts }));
  const flips = new Set(signs.map((s) => s.sign).filter(Boolean)).size > 1;
  const cols = [
    { label: "到期日", render: (e) => e.expiry, csv: (e) => e.expiry },
    { label: "天数", cls: "r", render: (e) => e.dte, csv: (e) => e.dte },
    { label: "净 GEX / 1%", cls: "r", render: (e) => usdShort(e.net_gex_usd_per_1pct), csv: (e) => e.net_gex_usd_per_1pct },
    { label: "call OI", cls: "r", render: (e) => num(e.call_oi, 0), csv: (e) => e.call_oi },
    { label: "put OI", cls: "r", render: (e) => num(e.put_oi, 0), csv: (e) => e.put_oi },
    { label: "P/C（OI）", cls: "r", render: (e) => (e.call_oi > 0 ? num(e.put_oi / e.call_oi, 2) : "—"), csv: (e) => (e.call_oi > 0 ? e.put_oi / e.call_oi : "") },
    { label: "成交量", cls: "r", render: (e) => num(e.volume, 0), csv: (e) => e.volume },
    { label: "ATM IV", cls: "r", render: (e) => pct(e.atm_iv), csv: (e) => e.atm_iv },
  ];
  fill(body, 
    panel("三个期限视图", { sub: "GEX 是带符号求和，截断到期日集合可能翻号——三个视图并列给出，路由固定读 ≤45 天" },
      flips ? h("p", { class: "callout warn" }, "三个视图在现价处的 gamma 符号不一致：环境判断对到期日范围敏感，读路由时要打折。") : null,
      table([{ label: "视图", render: (s) => s.label }, { label: "现价处", render: (s) => signChip(s.sign) },
        { label: "总净 GEX / 1%", cls: "r", render: (s) => usdShort(s.tot) }, { label: "合约数", cls: "r", render: (s) => num(s.n, 0) }], signs)),
    h("div", { style: { height: "20px" } }),
    h("div", { class: "grid halves" },
      panel("逐到期日净 GEX", { sub: "美元 / 现价每变动 1%" }, ex.length ? gexEl : h("div", { class: "empty" }, "—")),
      panel("ATM 隐含波动率期限结构", { sub: `IV30 ${isNum(d.iv30) ? num(d.iv30, 1) + "%" : "—"}` }, ex.length ? ivEl : h("div", { class: "empty" }, "—"))),
    h("div", { style: { height: "20px" } }),
    panel("到期日明细", { right: h("button", { class: "btn", type: "button", onclick: () => downloadCSV(`alphabot-${d.ticker}-expiries.csv`, cols, ex) }, "导出 CSV") },
      table(cols, ex)));
  if (ex.length) {
    columns(gexEl, { x: ex.map((e) => `${e.expiry.slice(5)}`), values: ex.map((e) => e.net_gex_usd_per_1pct) });
    lines(ivEl, { x: ex.map((e) => e.expiry.slice(5)), yFmt: (x) => pct(x, 0),
      series: [{ name: "ATM IV", data: ex.map((e) => e.atm_iv), color: C().ink2, symbol: true }] });
  }
}

// ─────────────────────────────── 卖权工作台
function sell(body, d) {
  let tenor = pref("sellTenor", "monthly");
  let pick = "short_put";
  const S = d.underlying_price;
  const c = C();
  function draw() {
    const row = d.tenors?.[tenor] || {};
    const rt = row.route || {};
    const a = d.assess?.[tenor] || {};
    const control = seg([["monthly", "月度 21–45 天"], ["weekly", "周度 7–20 天"]], tenor, (x) => { tenor = x; setPref("sellTenor", x); draw(); }, "期限");
    if (row.status !== "recorded") {
      fill(body, h("div", { class: "controls" }, control),
        panel(null, {}, h("div", { class: "empty" }, `这个期限没有候选：${row.unavailable_reason || "—"}`)));
      return;
    }
    const routeRung = { put: rt.put === "far" ? "0.10" : rt.put === "base" ? "0.20" : null,
      call: rt.call === "far" ? "0.10" : rt.call === "base" ? "0.20" : null };
    const earnWarn = row.earnings_status === "before_expiry" || row.earnings_status === "unknown";
    const top = h("div", { class: "page-head" },
      h("div", {}, h("h2", {}, `到期 ${row.expiry} · ${row.dte} 天`),
        h("div", { class: "fresh" },
          h("span", {}, "路由 ", h("b", {}, `put ${ROUTE_LABEL[rt.put] || "—"}`), " · ", h("b", {}, `call ${ROUTE_LABEL[rt.call] || "—"}`)),
          h("span", {}, `理由：${(rt.reasons || []).join("；") || "—"}`),
          h("span", {}, "财报 ", earnWarn ? h("span", { class: "chip warn" }, EARN_LABEL[row.earnings_status]) : h("b", {}, EARN_LABEL[row.earnings_status] || "—")))),
      control);

    // 梯子
    const L = row.ladder || {};
    const rungs = ["0.10", "0.16", "0.20", "0.25", "0.30"];
    const legOf = (side, k) => (L[side] || {})[k] || {};
    const cellsFor = (side, k) => {
      const slot = legOf(side, k); const leg = slot.short;
      const hl = routeRung[side] === k;
      const td = (text, cls = "r") => h("td", { class: `${cls}${hl ? " route-cell" : ""}` }, text);
      if (!leg) return [h("td", { class: "muted small", colspan: "6" }, `无（${(slot.reasons || []).join("，") || "—"}）`)];
      const q = (row.ladder_quotes?.[side] || {})[k] || {};
      const cells = [
        td(h("b", {}, num(leg.strike))), td(num(Math.abs(leg.delta ?? NaN), 3)), td(pct(leg.itm_prob)),
        td(num(leg.sigma_distance, 2)), td(h("span", {}, money(leg.bid), h("span", { class: "sub" }, `点差 ${pct(leg.spread_pct, 0)}`))),
        td(q.quotable ? h("span", {}, pct(q.yield_raw, 2), h("span", { class: "sub" }, `年化 ${pct(q.yield_annualized, 0)}`))
          : h("span", { class: "small muted" }, `不可报价：${(q.reason || "—").split(":")[0]}`)),
      ];
      return side === "put" ? cells.reverse() : cells;
    };
    const ladderTbl = h("div", { class: "table-wrap" }, h("table", { class: "ladder" },
      h("thead", {}, h("tr", {},
        ["单笔回报", "bid", "σ 距离", "P(ITM)", "|Δ|", "put K"].map((x) => h("th", { class: "r" }, x)),
        h("th", { class: "mid" }, "档位"),
        ["call K", "|Δ|", "P(ITM)", "σ 距离", "bid", "单笔回报"].map((x) => h("th", { class: "r" }, x)))),
      h("tbody", {}, rungs.map((k) => h("tr", {}, cellsFor("put", k), h("td", { class: "mid" }, k), cellsFor("call", k))))));

    // 结构
    const st = row.structures || {};
    const structRows = Object.keys(STRUCT_LABEL).map((k) => ({ k, route: st.route?.[k] || {}, base: st.base?.[k] || {} }));
    const qCells = (q) => q.quotable
      ? [h("td", { class: "r" }, money(q.credit)), h("td", { class: "r" }, q.max_loss === null ? "无上限" : money(q.max_loss)),
        h("td", { class: "r" }, h("span", {}, h("b", {}, pct(q.yield_raw, 2)), h("span", { class: "sub" }, `年化 ${pct(q.yield_annualized, 0)}`))),
        h("td", { class: "r" }, (q.breakevens || []).map((b) => num(b)).join(" / ") || "—")]
      : [h("td", { class: "muted small", colspan: "4" }, `不可报价：${q.reason || "—"}`)];
    const structTbl = h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {},
        h("tr", {}, h("th", {}, ""), h("th", { class: "c", colspan: "4" }, "路由档"), h("th", { class: "c", colspan: "4" }, "基线档 0.20Δ")),
        h("tr", {}, h("th", {}, "结构"), ...["权利金", "最大亏损", "单笔回报", "盈亏平衡"].map((x) => h("th", { class: "r" }, x)),
          ...["权利金", "最大亏损", "单笔回报", "盈亏平衡"].map((x) => h("th", { class: "r" }, x)))),
      h("tbody", {}, structRows.map((r) => {
        const tr = h("tr", { class: `link${r.k === pick ? " sel" : ""}`, tabindex: "0", title: "点击查看到期盈亏",
          onclick: () => { pick = r.k; draw(); }, onkeydown: (e) => { if (e.key === "Enter") { pick = r.k; draw(); } } },
        h("td", {}, h("b", {}, STRUCT_LABEL[r.k])), ...qCells(r.route), ...qCells(r.base));
        return tr;
      }))));

    const pf = d.payoff?.[tenor] || {};
    const em = d.expected_move?.[tenor] || {};
    const series = [];
    if (pf.route?.[pick]?.quotable) series.push({ name: `路由档 · ${STRUCT_LABEL[pick]}`, pnl: pf.route[pick].pnl, color: c.ink });
    if (pf.base?.[pick]?.quotable) series.push({ name: `基线档 · ${STRUCT_LABEL[pick]}`, pnl: pf.base[pick].pnl, color: c.ink3, dash: true });
    const pfEl = h("div", { class: "chart short" });

    fill(body, top,
      h("p", { class: `callout ${a.status === "ready" ? "" : "warn"}` }, plain(a.summary) || "就绪度判定不可得"),
      panel("Delta 梯子", { sub: "put 在左、call 在右；框出的格子是路由选中的档。P(ITM) = N(d2) 风险中性概率，不是 |Δ|；单笔回报 = bid ÷ 占用资金" }, ladderTbl),
      h("div", { style: { height: "20px" } }),
      h("div", { class: "grid" },
        panel("六种结构", { sub: "卖按 bid、买按 ask，每股口径（×100 为每张）" }, structTbl,
          h("p", { class: "note" }, plain(d.yield_note))),
        panel(`到期盈亏 · ${STRUCT_LABEL[pick]}`, { sub: `只按到期收盘算，不含存续期内触及；虚线 = ±1σ 期望波动（${money(em.move_1sigma)}）` },
          series.length ? pfEl : h("div", { class: "empty" }, "这个结构两档都不可报价。"))),
      h("div", { style: { height: "20px" } }),
      panel("局限", {}, h("ul", { class: "small muted" }, (d.caveats || []).map((x) => h("li", {}, plain(x))))));
    if (series.length) payoff(pfEl, { grid: pf.grid, series, spot: S, band: { lo: em.lo, hi: em.hi } });
  }
  draw();
}

// ─────────────────────────────── 价格
async function price(body, d, t, alive) {
  fill(body, h("div", { class: "empty" }, "加载日线…"));
  const b = await api(`/api/bars/${encodeURIComponent(t)}`);
  if (!alive()) return;
  if (!b.available) {
    fill(body, panel(null, {}, h("div", { class: "empty" }, "日线不可得。"),
      h("p", { class: "callout warn" }, `原因：${b.reason || "—"}（价格页只走 Twelve Data 共享入口，没配 key 就不画，不换源）`)));
    return;
  }
  const c = C();
  const v = d.views?.le_45dte || {};
  const zg = v.zero_gamma || {}; const mj = v.majors || {};
  const em = d.expected_move || {};
  const bars = b.bars || [];
  const hmarks = [];
  if (isNum(zg.nearest)) hmarks.push({ value: zg.nearest, label: `ZG ${num(zg.nearest)}`, color: c.ink3 });
  if (isNum(mj.net_major_pos_strike)) hmarks.push({ value: mj.net_major_pos_strike, label: `Major+ ${num(mj.net_major_pos_strike, 0)}`, color: c.pos, type: "dotted" });
  if (isNum(mj.net_major_neg_strike)) hmarks.push({ value: mj.net_major_neg_strike, label: `Major− ${num(mj.net_major_neg_strike, 0)}`, color: c.neg, type: "dotted" });
  const bands = [];
  if (isNum(em.monthly?.lo)) bands.push({ lo: em.monthly.lo, hi: em.monthly.hi, label: `月度 ±1σ（到 ${em.monthly.expiry}）`, color: c.sunk });
  const el = h("div", { class: "chart tall" });
  fill(body, panel("收盘价与今日水平", {
    sub: `最近 ${bars.length} 个交易日收盘（${b.source || ""}）；水平与 ±1σ 带只是**今天**的，历史水平叠其后价格的图在检验冻结前不提供（见口径页）` }, el));
  lines(el, { x: bars.map((r) => r.date.slice(5)), series: [{ name: "收盘", data: bars.map((r) => r.close), color: c.ink }],
    hmarks, bands, yFmt: (x) => num(x, 0) });
}
