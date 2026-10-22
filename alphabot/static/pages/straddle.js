// 跨式账本：财报跨式纸面账本（持仓 / 平仓 / 净值 / 逐仓风险）+ 盘中参考报价。全部只读，不写账本。
// 盲期：任何一笔的 GEX 都不和它之后的盈亏放在一起（服务端就不给）；GEX 影子记录只显示计数。
import { api, h, helpLink, num, money, pctPts, signedPct, panel, table, isNum, fill, signChip, plain } from "../lib.js";
import { lines, scatter, C } from "../charts.js";
import { state } from "../app.js";

const SIDE = { short: "卖跨式", long: "买跨式" };
const LABEL = { rich: "rich（贵）", cheap: "cheap（便宜）", fair: "fair（合理）", untradeable: "不可交易" };
const EXIT = { post_event: "财报后平仓", expiry_buffer: "到期前强平", written_off: "核销（无价）" };
const MARK = { cboe_mid: "CBOE 中间价", stale: "冻结 mark", intrinsic: "内在价值", written_off: "核销" };
const PX_SRC = { cboe_close: "收盘后快照", cboe_intraday: "盘中（15 分钟延迟）", cboe_stale_intraday: "收盘后仍是盘中文件", demo: "演示数据" };

const sMoney = (v, dp = 0) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}$${num(Math.abs(v), dp)}` : "—");
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const day = (s) => (s ? Date.parse(`${String(s).slice(0, 10)}T00:00:00Z`) / 864e5 : NaN);
const kpi = (k, v, cls = "") => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: `v ${cls}` }, v));

// 入场 → 财报 → 到期，「今天」处画到哪
function timeline(p, today) {
  const a = day(p.entry_date), b = day(p.expiry), e = day(p.earnings_date), t = day(today);
  if (!isNum(a) || !isNum(b) || b <= a) return null;
  const at = (x) => `${Math.max(0, Math.min(100, ((x - a) / (b - a)) * 100))}%`;
  return h("div", { class: "tl", role: "img", "aria-label": `入场 ${p.entry_date}，财报 ${p.earnings_date || "—"}，到期 ${p.expiry}` },
    h("div", { class: "rail" }), isNum(t) ? h("div", { class: "done", style: { width: at(t) } }) : null,
    isNum(e) ? h("div", { class: "mk earn", style: { left: at(e) } }) : null,
    h("span", { class: "lbl l", style: { left: "0%" } }, `入场 ${String(p.entry_date).slice(5)}`),
    isNum(e) ? h("span", { class: "lbl e", style: { left: at(e) } }, `财报 ${String(p.earnings_date).slice(5)}`) : null,
    h("span", { class: "lbl r", style: { left: "100%" } }, `到期 ${String(p.expiry).slice(5)}`));
}

// 到期盈亏平衡带 K ± 入场权利金 与现价的位置。卖跨式：带内赚（蓝）；买跨式：带外赚。
function bandBar(side, be, S) {
  if (!be?.available) return h("div", { class: "small muted" }, "盈亏平衡带不可得");
  const lo = be.lo, hi = be.hi, w = hi - lo;
  const a = Math.min(lo, isNum(S) ? S : lo) - w * 0.3, b = Math.max(hi, isNum(S) ? S : hi) + w * 0.3;
  const at = (x) => `${((x - a) / (b - a)) * 100}%`;
  return h("div", { class: `band ${side === "long" ? "long" : ""}`, role: "img",
    "aria-label": `到期盈亏平衡 ${num(lo)} 到 ${num(hi)}，现价 ${num(S)}` },
  h("div", { class: "track" }), h("div", { class: "zone", style: { left: at(lo), width: `${(w / (b - a)) * 100}%` } }),
  h("span", { class: "lbl", style: { left: at(lo) } }, num(lo)), h("span", { class: "lbl", style: { left: at(hi) } }, num(hi)),
  isNum(S) ? h("div", { class: "spot", style: { left: at(S) } }) : null,
  isNum(S) ? h("span", { class: "lbl s", style: { left: at(S) } }, `现价 ${num(S)}`) : null);
}

function bandText(side, band) {
  if (!band?.available) return "现价不可得，判断不了在带内还是带外。";
  const where = band.inside ? "在带内" : "在带外";
  const edge = `离最近一条边 ${pctPts(band.pct_to_nearest_edge, 2)}`;
  const verdict = band.at_expiry_in_profit ? "若到期停在这里：赚" : "若到期停在这里：亏";
  return `${where}，${edge}。${verdict}（${side === "short" ? "卖跨式要价格留在带内" : "买跨式要价格冲出带外"}）。`;
}

// 合计里只要有一格有数，就说明两条腿的数都被用上了（合计需要两条腿）；全是「—」时不许再声称「含某腿的模型值」
const GREEK_TOTALS = ["dollar_delta", "gamma_dollar_per_1pct", "theta_dollar_per_day", "vega_dollar_per_pt"];

function greeksGrid(g, warnAt) {
  if (!g) return null;
  const cell = (k, v) => h("div", {}, h("div", { class: "k" }, k), h("div", { class: "v" }, v));
  const shares = isNum(g.delta_shares) ? `${g.delta_shares > 0 ? "+" : g.delta_shares < 0 ? "−" : ""}${num(Math.abs(g.delta_shares), 0)} 股` : "—";
  return [
    h("div", { class: "sd-greeks" },
      cell("净 Δ（$）", h("span", {}, sMoney(g.dollar_delta), h("span", { class: "cell-sub" }, shares))),
      cell("Γ（1% 移动）", sMoney(g.gamma_dollar_per_1pct)),
      cell("θ / 天", sMoney(g.theta_dollar_per_day)),
      cell("vega / 点", sMoney(g.vega_dollar_per_pt))),
    g.directional ? h("p", { class: "callout warn" },
      `已明显带方向：每份跨式净 Δ ${num(g.net_delta_per_straddle, 2)}（入场时约为 0，提示阈值 ${num(warnAt, 2)}）。账本不做 delta 对冲，这一笔现在相当于持有 ${shares}。`) : null,
    !g.complete ? h("div", { class: "small muted" }, `Greeks 不全：${(g.missing || []).join("、")}`) : null,
    g.degraded_legs?.length && GREEK_TOTALS.some((k) => isNum(g[k])) ? h("div", { class: "small muted" },
      `Greeks 含 ${g.degraded_legs.join("、")} 腿（bid=0）的 CBOE 模型值，仅供参考；这条腿没有 mid，盯市价与压力表仍按严格报价口径。`) : null,
  ];
}

// 为什么 mid / 立即平仓价不可得：bid=0 的腿（卖跨式仍可按 ask 买回）与真缺报价要分开说
function quoteNote(r, side) {
  if (r.quote_ok) return null;
  const zero = r.bid_zero_legs || [];
  if (!zero.length) return "两腿报价不全，mid 与立即平仓价不可得。";
  const names = zero.join("、");
  if (isNum(r.exit_now_usd)) return `${names} 腿 bid=0：mid 不可得；立即平仓价按 ask 给出（卖跨式买回只看 ask）。`;
  return side === "long"
    ? `${names} 腿 bid=0（无买盘）：mid 不可得；买跨式按 bid 卖出，立即平仓价不可得。`
    : `${names} 腿 bid=0：mid 不可得；另一条腿没有可用的 ask，立即平仓价也不可得。`;
}

function levelsLine(lv, be) {
  if (!lv?.available) return h("div", { class: "small muted" }, `当天 GEX 水平不可得：${lv?.reason || "—"}`);
  const rel = (x) => (!isNum(x) || !be?.available ? "" : x < be.lo ? "（带下方）" : x > be.hi ? "（带上方）" : "（带内）");
  return h("div", { class: "sd-row" }, h("span", { class: "grade", title: "证据等级：Zero Gamma C、Major D" }, "C/D"), " 当天 GEX（≤45 天视图）：现价处 ", signChip(lv.sign_at_spot),
    ` · 最近零点 ${num(lv.zg_nearest)}${rel(lv.zg_nearest)} · 净 Major+ ${num(lv.net_major_pos, 0)}${rel(lv.net_major_pos)} · 净 Major− ${num(lv.net_major_neg, 0)}${rel(lv.net_major_neg)}`);
}

// ── 跨式 vs 宽跨 并排对比卡（v0.45.584）：只并排事实，不给「选哪个」（没有推荐 / 综合分字段，守卫钉住）
const CMP_NAME = { straddle: "跨式（ATM）", strangle: "宽跨（25Δ）" };

function compareCard(r, opts = {}) {
  if (!r.available) return h("p", { class: "small muted" }, `${r.ticker || ""} ${r.as_of || ""} 对比不可得：${r.reason_zh || r.reason || "—"}`);
  const A = r.structures.straddle, B = r.structures.strangle;
  const cell = (s, render) => (s.available ? h("td", {}, render(s)) : h("td", { class: "na" }, "—"));
  const sub = (t) => h("span", { class: "sub" }, t);
  const leg = (l, k) => (l ? `${k} bid ${num(l.bid)} / ask ${num(l.ask)} · Δ ${num(l.delta, 2)} · OI ${num(l.oi, 0)}` : `${k} —`);
  const rows = [
    ["行权价", (s) => (s.legs.put.strike === s.legs.call.strike ? `K ${num(s.legs.call.strike)}（put / call 同价）` : `put ${num(s.legs.put.strike)} · call ${num(s.legs.call.strike)}`),
      (s) => h("span", {}, sub(leg(s.legs.put, "put")), sub(leg(s.legs.call, "call")))],
    ["收到的权利金（按 bid）", (s) => num(s.premium), (s) => sub(`占现价 ${pctPts(s.premium_frac_of_spot * 100, 2)}`)],
    ["立刻进出一次要付掉权利金的", (s) => pctPts(s.round_trip_frac * 100, 0), (s) => sub(`买回按 ask：${num(s.cost_to_close)}`)],
    ["最大腿点差", (s) => pctPts(s.max_spread_frac * 100, 1), (s) => (s.flags || []).map((f) => h("span", { class: "chip warn", title: f.text }, `> ${pctPts(f.limit * 100, 0)} 闸门`))],
    ["到期盈亏平衡带", (s) => `${num(s.band.lo)} – ${num(s.band.hi)}`, (s) => sub(`相对现价 ${signedPct(s.band.lo_pct)} … ${signedPct(s.band.hi_pct)}`)],
    ["历史涨跌落在带外", (s) => (s.hist_outside.n ? `${s.hist_outside.k} / ${s.hist_outside.n}` : "—"),
      (s) => (s.hist_outside.ci95 ? sub(`95% 区间 ${pctPts(s.hist_outside.ci95[0] * 100, 0)}–${pctPts(s.hist_outside.ci95[1] * 100, 0)}（样本很小）`) : null)],
    [`账本定仓${isNum(r.nav) ? `（按${r.nav_basis?.label || " NAV"} ${money(r.nav, 0)}）` : ""}`, (s) => (s.sizing.contracts ? `${s.sizing.contracts} 张` : "—"),
      (s) => (s.sizing.contracts ? sub(`${num(s.sizing.shares, 0)} 股 · 名义 ${money(s.sizing.notional_usd, 0)}`) : sub(String(s.sizing.reason || "")))],
  ];
  const head = h("thead", {}, h("tr", {}, h("th", { scope: "col" }, ""), h("th", { scope: "col" }, CMP_NAME.straddle), h("th", { scope: "col" }, CMP_NAME.strangle)));
  const body = h("tbody", {}, rows.map(([label, main, extra], i) => h("tr", {}, h("th", { scope: "row" }, label),
    ...[A, B].map((s) => (!s.available && i === 0 ? h("td", { class: "na" }, s.reason_zh || "不可得")
      : cell(s, (x) => h("span", {}, main(x), extra ? extra(x) : null)))))));
  const sig = r.signal || {};
  const hist = r.hist;
  // 持仓卡里传入账本实际张数：对比卡按还原的 NAV 估算，两者不一致（取整误差）就直说，不让同一张卡上出现两个张数还不解释
  const real = opts.ledgerContracts, est = A.sizing?.contracts;
  const mismatch = isNum(real) && A.available && isNum(est) && real !== est;
  return h("div", { class: "cmp" },
    h("div", { class: "cmp-head" }, h("b", {}, r.ticker || "—"), ` · 快照 ${r.as_of}（${r.market_open === false ? "收盘后" : r.market_open ? "盘中" : "时段未知"}）· 标的 ${num(r.underlying_price)} · 到期 ${r.selected_expiry || "—"}${isNum(r.dte) ? `（${r.dte} 天）` : ""}${sig.earnings_date ? ` · 财报 ${sig.earnings_date}` : ""}`),
    h("div", { class: "cmp-scroll" }, h("table", { class: "cmp-grid" }, head, body)),
    isNum(r.premium_ratio) ? h("div", { class: "sd-row" }, `宽跨权利金是跨式的 ${pctPts(r.premium_ratio * 100, 0)}`,
      isNum(r.contracts_ratio) ? `；同一风险预算下张数是跨式的 ${num(r.contracts_ratio, 1)} 倍——名义风险不一定更小。` : "。") : null,
    hist ? h("div", { class: "sd-row" }, `这只票过去 ${hist.n} 次财报的 2 日涨跌（财报日早于 ${r.as_of}）：`,
      hist.moves.map((m) => signedPct(m.move_pct)).join(" · "),
      `；中位 |涨跌| ${pctPts(hist.median_abs_pct, 2)}、最大 ${pctPts(hist.max_abs_pct, 2)}`,
      isNum(sig.implied_event_move_pct) ? `；信号日的隐含事件波动 ${pctPts(sig.implied_event_move_pct, 2)}` : "")
      : h("div", { class: "small muted" }, `历史财报涨跌不可得：${r.hist_reason_zh || "—"}`),
    mismatch ? h("div", { class: "small muted" }, `账本实际开了 ${real} 张；本卡按上面的 NAV 与这份快照的报价估算为 ${est} 张。差异可能来自 NAV 还原的取整，或账本当时用的报价 / 风险比例与现在不同——以账本的张数为准。`) : null,
    h("ul", { class: "small muted cmp-notes" }, (r.caveats || []).map((c) => h("li", {}, c.text))));
}

// 同一个容器（「今日信号」共用的 cmpHost）可以被连点：每次请求领一个递增令牌（el.dataset.cmpSeq），回来时令牌已不是最新的
// （或容器已不在页面上）就丢弃——迟到的旧响应、失败的旧请求都不许盖掉新卡。返回结局：ok / unavailable / error / stale。
async function loadCompare(el, ticker, asOf, opts = {}) {
  const seq = String((Number(el.dataset.cmpSeq) || 0) + 1);
  el.dataset.cmpSeq = seq;
  const current = () => el.dataset.cmpSeq === seq && el.isConnected;
  fill(el, h("div", { class: "small muted" }, `正在读 ${ticker} ${asOf || ""} 的快照…`));
  try {
    const r = await api(`/api/straddle/compare/${encodeURIComponent(ticker)}${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ""}`);
    if (!current()) return "stale";
    fill(el, compareCard(r, opts));
    return r.available ? "ok" : "unavailable";
  } catch (e) {
    if (!current()) return "stale";
    fill(el, h("div", { class: "small err" }, `${ticker} 对比拉取失败：${e.message || e}　`,
      h("button", { class: "btn", type: "button", onclick: () => loadCompare(el, ticker, asOf, opts) }, "重试")));
    return "error";
  }
}

function renderLive(el, r, p, warnAt) {
  if (!r) { fill(el, h("div", { class: "small muted" }, "盘中参考：未拉取。")); return; }
  if (r.error) { fill(el, h("div", { class: "small err" }, `盘中参考拉取失败：${r.error}`)); return; }
  if (!r.available) {
    fill(el, h("div", { class: "small muted" }, `盘中参考不可得：${r.reason || "—"}（不拿账本 mark 顶替）`), levelsLine(r.levels, r.breakeven));
    return;
  }
  const when = (r.payload_last_trade_time || "").replace("T", " ").slice(0, 16);
  fill(el,
    h("div", { class: "sd-k" }, `盘中参考 · ${PX_SRC[r.underlying_price_source] || r.underlying_price_source || "—"} · 最后成交 ${when || "—"}${r.cache_age_sec ? ` · ${Math.round(r.cache_age_sec)} 秒前` : ""}${r.demo ? " · 演示" : ""}`),
    h("div", { class: "sd-row" }, `现价 `, h("b", {}, num(r.underlying_price)), ` · mark（mid）`, h("b", {}, num(r.mark_mid)),
      ` → `, h("b", { class: tone(r.unrealized_mid_usd) }, `${sMoney(r.unrealized_mid_usd)}（${signedPct(r.unrealized_mid_pct)}）`),
      ` · 立即平仓（${r.exit_now_basis === "ask_only" ? "按 ask" : "付点差"}）`, h("b", { class: tone(r.exit_now_usd) }, sMoney(r.exit_now_usd))),
    quoteNote(r, p.side) ? h("div", { class: "small muted" }, quoteNote(r, p.side)) : null,
    h("div", { class: "sd-row" }, bandText(p.side, r.band)),
    greeksGrid(r.greeks, warnAt),
    levelsLine(r.levels, r.breakeven),
    r.same_payload === false ? h("div", { class: "small muted" }, "注意：水平与报价来自不同时刻的两份 CBOE 文件。") : null);
}

// 账本 mark 冻结（某条腿报价不合账本口径）时，卡片上的「账本浮动盈亏」是旧价：标的在 mark 之后走了多远它都不知道。
// 这里并排给一个按当前报价算的估算（只读、不写账本、与账本数字分开标注）：优先 mid；mid 不可得（bid=0）时，
// 卖跨式按 ask 买回的价（立即平仓价，保守）。拿不到报价就如实说，不拿账本 mark 顶替。
function estimateOf(r, p) {
  if (isNum(r.unrealized_mid_usd)) return { usd: r.unrealized_mid_usd, basis: "mid" };
  if (isNum(r.exit_now_usd)) return { usd: r.exit_now_usd, basis: r.exit_now_basis === "ask_only" ? "按 ask 买回" : "付点差平仓" };
  return null;
}

function renderEst(el, r, p) {
  if (!r) { fill(el, `账本 mark 停在 ${p.last_mark_date || "—"}（冻结 ${p.stale_days ?? "?"} 天），浮动盈亏是旧价。当前报价估算：未拉取。`); return; }
  if (r.error) { fill(el, `账本 mark 已冻结，浮动盈亏是旧价。当前报价估算拉取失败：${r.error}`); return; }
  if (!r.available) { fill(el, `账本 mark 已冻结，浮动盈亏是旧价。当前报价估算不可得：${r.reason || "—"}。`); return; }
  const e = estimateOf(r, p);
  if (!e) { fill(el, "账本 mark 已冻结，浮动盈亏是旧价。当前报价估算不可得：两腿报价都不全。"); return; }
  const diff = isNum(p.unrealized_usd) ? e.usd - p.unrealized_usd : null;
  fill(el, "账本 mark 已冻结，上面的浮动盈亏是旧价。按当前报价估算 ", h("b", { class: tone(e.usd) }, sMoney(e.usd)),
    `（${e.basis}，非账本数字${isNum(diff) ? `；比账本低估 / 高估 ${sMoney(diff)}` : ""}）。`);
}

function positionCard(p, d, liveEls) {
  const warnAt = d.rules?.directional_delta_warn;
  const g = p.greeks, es = p.entry_signal || {};
  const liveEl = h("div", { class: "sd-live" });
  const stale = p.mark_source !== "cboe_mid";
  const estEl = stale ? h("div", { class: "callout warn sd-est" }) : null;
  liveEls[p.ticker] = { el: liveEl, p, warnAt, estEl };
  renderLive(liveEl, null, p, warnAt);
  if (estEl) renderEst(estEl, null, p);
  // 入场时的对比：展开才读（只读本机快照与缓存文件，不联网）。对比卡整张是卖方视角（收 bid、买回按 ask、按空头定仓规则），
  // 买跨式持仓不展示它（不编买方数）。懒加载标志 dataset.loaded 只在**拿到可用的卡**之后才置位：失败 / 不可得时
  // 再展开会重新请求（loadCompare 的失败文案里还有「重试」）；dataset.busy 防止加载中重复展开发出并发请求。
  const cmpBody = h("div", { class: "cmp" }, h("div", { class: "small muted" }, "展开时才读入场日的快照。"));
  const cmpDet = p.side === "long"
    ? h("p", { class: "small muted cmp-long-note" }, "买入跨式：对比卡只按卖方视角给出，此处不展示。")
    : h("details", { class: "sd-why", ontoggle: async () => {
      if (!cmpDet.open || cmpDet.dataset.loaded === "1" || cmpDet.dataset.busy === "1") return;
      cmpDet.dataset.busy = "1";
      const out = await loadCompare(cmpBody, p.ticker, p.entry_date, { ledgerContracts: p.contracts });
      delete cmpDet.dataset.busy;
      if (out === "ok") cmpDet.dataset.loaded = "1";
    } }, h("summary", {}, "入场时：跨式 vs 宽跨（只并排事实，卖方视角）"), cmpBody);
  return h("article", { class: "sd-card", "aria-label": `${p.ticker} ${SIDE[p.side] || p.side}` },
    h("div", { class: "sd-head" },
      h("span", { class: "tk" }, p.ticker), h("span", { class: "chip" }, SIDE[p.side] || p.side),
      h("span", { class: "chip" }, LABEL[p.label] || p.label || "—"),
      stale ? h("span", { class: "chip warn" }, `${MARK[p.mark_source] || p.mark_source} ${p.stale_days ?? "?"} 天`) : null,
      h("span", { class: "terms" }, `K ${num(p.strike)} · ${p.expiry} 到期 · ${p.contracts} 张 · 入场 ${p.entry_date}（标的 ${num(p.entry_underlying)}）`)),
    h("div", { class: "sd-pnl" },
      h("div", {}, h("div", { class: "sd-k" }, `账本浮动盈亏（mark ${p.last_mark_date || "—"}）`),
        h("div", { class: `big ${tone(p.unrealized_usd)}` }, sMoney(p.unrealized_usd)),
        h("div", { class: "small muted" }, `${signedPct(p.unrealized_pct)} · 权利金 ${num(p.entry_premium)} → ${num(p.last_mark)} · 名义 ${money(p.size_usd, 0)}`)),
      h("div", { class: "small muted", style: { textAlign: "right" } },
        isNum(p.days?.to_earnings) ? (p.days.to_earnings >= 0 ? `距财报 ${p.days.to_earnings} 天` : `财报已过 ${-p.days.to_earnings} 天`) : "财报日未知",
        h("br"), isNum(p.days?.to_expiry) ? `距到期 ${p.days.to_expiry} 天` : "")),
    estEl,
    timeline(p, d.today_et),
    h("div", {}, h("div", { class: "sd-k" }, "到期盈亏平衡带（K ± 入场权利金）"), bandBar(p.side, p.breakeven, p.underlying?.price),
      h("div", { class: "sd-row" }, bandText(p.side, p.band))),
    h("div", {}, h("div", { class: "sd-k" }, `风险（${p.underlying?.as_of || "—"} 收盘后，与账本 mark 同一份快照）`),
      d.greeks_file?.available ? greeksGrid(g, warnAt) : h("div", { class: "small muted" }, `Greeks 不可得：没有 ${d.greeks_file?.date || "—"} 的逐腿 Greeks 文件（${d.greeks_file?.error || "—"}）`)),
    h("details", { class: "sd-why" }, h("summary", {}, "入场理由"),
      h("p", {}, `隐含事件波动 ${pctPts(es.implied_event_move_pct, 2)} vs 历史中位 ${pctPts(es.hist_median_abs_move_pct, 2)}（n=${es.hist_n ?? "—"}），比值 ${num(es.ratio, 2)}；最大腿点差 ${isNum(es.max_leg_spread_pct) ? pctPts(es.max_leg_spread_pct * 100, 1) : "—"}；30 日实现波动 ${pctPts(es.rv_30d, 1)}；入场时两腿 Δ 之和 ${num(es.straddle_net_delta, 3)}。`),
      h("p", { class: "small muted" }, plain(p.rationale || ""))),
    cmpDet,
    liveEl);
}

function gate(label, have, want) {
  const ratio = isNum(have) && want ? Math.min(1, have / want) : 0;
  return h("div", { class: "gate" }, h("span", {}, label),
    h("div", { class: `bar ${ratio >= 1 ? "done" : ""}`, role: "progressbar", "aria-valuenow": String(have ?? 0), "aria-valuemax": String(want ?? 0), "aria-label": label },
      h("i", { style: { width: `${(ratio * 100).toFixed(1)}%` } })),
    h("span", { class: "num r small" }, `${have ?? 0} / ${want ?? "—"}`));
}

function shadowPanel(s, rules) {
  const pre = rules?.prereg || {};
  if (!s?.available) {
    return panel("GEX 影子记录", { sub: "攒样本中 · 冻结前只给计数" }, h("p", { class: "small muted" }, `进度不可得：${s?.reason || "—"}`));
  }
  const n = s.n_informative || {}, need = s.need || {};
  return panel("GEX 影子记录", { sub: s.frozen ? "检验已冻结" : (s.ready ? "已就绪，等待运行一次" : "攒样本中 · 冻结前只给计数") },
    h("p", { class: "small" }, `每条财报信号记下当天的 GEX 政体（全到期日视图），事后检验「正 gamma 下实际事件波动是否小于隐含」。已记录 ${s.n_events} 个财报事件，其中带可用 GEX 的 ${s.n_with_gex} 个（正 gamma ${s.n_with_gex_by_regime?.positive_gex ?? 0} · 负 gamma ${s.n_with_gex_by_regime?.negative_gex ?? 0}）。`),
    gate("有效单位（GEX 可用且已结算）", s.n_eligible_settled, need.min_units),
    gate("信息块内 · 正 gamma", n.positive_gex, need.min_per_group),
    gate("信息块内 · 负 gamma", n.negative_gex, need.min_per_group),
    gate("信息块（两种政体都有的财报周）", n.blocks, need.min_informative_blocks),
    h("p", { class: "callout lock" }, `盲期：检验冻结前，页面不把任何一笔的 GEX 和它之后的盈亏、实际波动放在一起；自己拼起来按政体比较就是偷看，会作废这次检验。协议 v${pre.version ?? "—"}：${pre.doc || "experiments/straddle_gex_prereg.md"}。`),
    h("div", { class: "small muted" }, `按状态：${Object.entries(s.by_status || {}).map(([k, v]) => `${k} ${v}`).join("、") || "—"}`));
}

// 影子宽跨记录（v0.45.586，协议 experiments/straddle_strangle_shadow_prereg.md）：**只显示计数**——任何一笔的影子盈亏、
// 与跨式盈亏的差都不在这一页（盲期：自己拼起来按结构比较就是偷看）。
// 单位状态 → 中文。键与 straddle_strangle_shadow.units() 能产出的状态一一对应；`exit_not_applicable` 在 units() 里不可达
// （入场不是 ok 的单位到不了平仓那一步）所以不登记。未登记的状态（以后新增的）原样显示代码，不留空（ssLabel）。
const SS_STATUS = { ok: "进检验", no_record: "没有影子记录", before_start: "登记前入场（排除）", long_side: "多头（只描述）",
  entry_unquotable: "入场时宽跨不可成交", entry_no_signal_record: "信号行没带记录", entry_no_size: "定仓给不出张数",
  exit_unpriced: "平仓日无价", exit_error: "平仓记录出错", exit_missing: "平仓记录缺影子平仓项",
  exit_before_event: "平仓日不晚于财报日（事件前就平了，排除）", unusable: "数据不全" };

const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);   // 状态码来自后端：'constructor' 之类不许查到原型上的东西

function ssLabel(k) {
  if (own(SS_STATUS, k)) return SS_STATUS[k];
  const m = /^schema:(.*)$/.exec(String(k));
  return m ? `影子记录版本 ${m[1]} 不符` : String(k);
}

// 持仓中的 open_by_status 键是**入场**状态（ok / unquotable / no_signal_record / no_size），与已平仓单位共用同一张表：
// ok = 入场时两腿能成交（平仓后才会进检验），其余补 entry_ 前缀查同一张表；查不到再原样显示。
function ssOpenLabel(k) {
  if (k === "ok") return "入场可成交";
  const key = `entry_${k}`;
  return own(SS_STATUS, key) ? SS_STATUS[key] : ssLabel(k);
}

// 检验**冻结之后**才有（服务端在未冻结 / 冻结文件坏了时根本不给 `revealed`）：冻结的结果 + 每个进检验的空头单位的配对。
// 冻结前这个函数不会被调用，也没有它的数据。d = (宽跨假想盈亏 − 跨式盈亏) ÷ 定仓 NAV（含定仓杠杆，协议 §3）。
const RV_DECISION = { reject_h0: "拒绝 H0：两种结构的盈亏有系统差异", fail_to_reject_h0: "没能拒绝 H0：这个样本量下没看出差异（不等于两者一样）" };
const RV_DIRECTION = { strangle_higher: "宽跨更高", straddle_higher: "跨式更高", none: "无方向" };

function revealedBlock(rv) {
  const r = rv?.result || {};
  const pairs = rv?.pairs || [];
  const pct = (v) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v) * 100, 2)}%` : "—");
  const head = r.testable === false
    ? h("p", { class: "small" }, `冻结时无法检验：${r.reason || "—"}。`)
    : h("p", { class: "small" }, `${RV_DECISION[r.decision] || r.decision || "—"}；p = ${isNum(r.p_value) ? num(r.p_value, 4) : "—"}（${r.mode === "enumerate" ? "枚举" : "随机置换"}），`,
      `方向：${RV_DIRECTION[r.direction] || r.direction || "—"}，D = 平均 d = ${pct(r.observed)}（占定仓 NAV）。${r.n_units ?? "—"} 个单位 / ${r.n_blocks ?? "—"} 个财报周，${r.ready_date || "—"} 冻结。`);
  const cols = [
    { label: "票", render: (p) => p.ticker },
    { label: "财报日", render: (p) => p.earnings_date || "—" },
    { label: "入场 → 平仓", render: (p) => `${p.entry_date || "—"} → ${p.exit_date || "—"}` },
    { label: "跨式", cls: "r", render: (p) => h("span", { class: tone(p.straddle_usd) }, `${p.straddle_contracts ?? "—"} 张 · ${sMoney(p.straddle_usd)}`) },
    { label: "宽跨（假想）", cls: "r", render: (p) => h("span", { class: tone(p.strangle_usd) }, `${p.strangle_contracts ?? "—"} 张 · ${sMoney(p.strangle_usd)}`) },
    { label: "d（占 NAV）", cls: "r", render: (p) => h("span", { class: tone(p.d) }, pct(p.d)) },
    { label: "冻结的检验", render: (p) => (p.in_frozen_test ? "在" : "之后平仓，只描述") },
  ];
  return h("div", {},
    h("h3", {}, "冻结结果与每笔配对"),
    head,
    pairs.length ? table(cols, pairs) : h("p", { class: "small muted" }, "没有进检验的单位。"),
    h("p", { class: "small muted" }, "盲期已结束（检验已冻结）。配对只是描述：判定只看冻结的那次检验；宽跨张数按账本的权利金定仓，是跨式的数倍，所以盈亏金额不能直接比大小。"));
}

function strangleShadowPanel(s, rules) {
  const pre = rules?.strangle_prereg || {};
  if (!s?.available) {
    return panel("影子宽跨记录", { sub: "攒样本中 · 冻结前只给计数" }, h("p", { class: "small muted" }, `进度不可得：${s?.reason || "—"}`));
  }
  const need = s.need || {};
  const byStatus = Object.entries(s.by_status || {}).map(([k, v]) => `${ssLabel(k)} ${v}`).join("、") || "—";
  const openBy = Object.entries(s.open_by_status || {}).map(([k, v]) => `${ssOpenLabel(k)} ${v}`).join("、") || "—";
  // n_closed 是**全部**已平仓单位（含登记前入场、状态 before_start 的），不是「自 start_date 起入场的」——拆开说，只给个数
  const nClosed = isNum(s.n_closed) ? s.n_closed : 0;
  const nBefore = Number(s.by_status?.before_start) || 0;
  const closedText = `已平仓共 ${nClosed} 笔，其中登记后入场 ${nClosed - nBefore} 笔${nBefore ? `（其余 ${nBefore} 笔是登记前入场、不进检验）` : ""}，进检验 ${s.n_units} 笔（只检验空头）。`;
  return panel("影子宽跨记录", { sub: s.frozen ? "检验已冻结" : (s.ready ? "已就绪，等待运行一次" : "攒样本中 · 冻结前只给计数") },
    h("p", { class: "small" }, `每笔被账本开仓的跨式，同一天、同一份快照里再记一份假想的 25Δ 宽跨（只记录、不开仓）。持仓中自 ${s.start_date} 起入场且带记录的 ${s.n_open_recorded} 笔（${openBy}）；${closedText}`),
    gate("进检验的空头单位", s.n_units, need.min_units),
    gate("财报周（块）", s.n_blocks, need.min_blocks),
    s.revealed ? null
      : h("p", { class: "callout lock" }, `盲期：检验冻结前，页面不显示任何一笔的影子盈亏，也不把它和跨式盈亏放在一起；自己拼起来比较就是偷看，会作废这次检验。检验冻结后这里自动解锁每笔配对。协议 v${pre.version ?? "—"}：${pre.doc || "experiments/straddle_strangle_shadow_prereg.md"}。`),
    h("div", { class: "small muted" }, `已平仓按状态：${byStatus}`),
    s.revealed ? revealedBlock(s.revealed) : null);
}

export async function renderStraddle(root, alive) {
  const d = await api("/api/straddle");
  if (!alive()) return;
  const head = h("div", { class: "page-head" }, h("div", {},
    h("h1", {}, "财报跨式账本", h("span", { class: "sub" }, d.source === "demo" ? "演示数据" : (d.as_of ? `账本 ${d.as_of}` : ""))),
    h("p", { class: "lede" }, "纸面账本（模拟成交，不连券商）：财报前比较「期权隐含的事件波动」和「这只票过去 8 次财报的实际波动」——贵就卖跨式、便宜就买跨式，财报后平仓。每个扫描日收盘后更新一次；盘中可以另拉参考报价。 ", helpLink("straddle"))));
  if (!d.data_available) {
    fill(root, head, panel(null, {}, h("div", { class: "empty" }, `没找到跨式账本目录：${d.state_dir?.path || "—"}`),
      h("p", { class: "small muted" }, "桌面程序用 --reset 重选数据根；终端运行要设 ALPHA_HIVE_HOME。")));
    return;
  }
  const a = d.account || {}, k = d.kpis || {}, rules = d.rules || {};
  const warns = [];
  if (d.freshness?.stale) warns.push(`账本停在 ${d.as_of}；按交易日历，${d.freshness.expected_as_of} 收盘后的扫描应已更新（扫描没跑、失败，或还在跑）。`);
  if (a.identity_ok === false) warns.push(`账目对不上：NAV 与「起始资金 + 已实现 + 浮动」差 ${sMoney(a.identity_gap_usd, 2)}。`);
  for (const [f, n] of Object.entries(d.bad_lines || {})) warns.push(`${f} 有 ${n} 行解析失败，没计入。`);
  if (d.meta_error) warns.push(`meta.json 读不了：${d.meta_error}`);

  const es = k.event_split;
  const sideSrc = es ? (es.saw_event.by_side || {}) : (k.by_side || {});
  const nExcl = es ? (es.event_missed.n || 0) + (es.event_unknown.n || 0) : 0;
  const sideSub = es && nExcl ? `已平仓 · 只含测到事件的 ${es.saw_event.n} 笔（不含事件前被平 ${es.event_missed.n} 笔${es.event_unknown.n ? `、日期缺失 ${es.event_unknown.n} 笔` : ""}）` : "已平仓";
  const kpis = h("div", { class: "kpis" },
    kpi("NAV", money(a.nav, 0)), kpi("累计", `${sMoney(isNum(a.nav) && isNum(a.starting_capital) ? a.nav - a.starting_capital : null)}（${signedPct(a.return_pct, 2)}）`, tone(a.return_pct)),
    kpi("已实现", sMoney(a.realized_usd), tone(a.realized_usd)), kpi("浮动", sMoney(a.unrealized_usd), tone(a.unrealized_usd)),
    kpi("在险权利金", money(a.open_premium_at_risk, 0)), kpi("持仓", `${a.positions ?? 0} / ${a.max_open ?? "—"}`),
    kpi("已平仓", `${k.n ?? 0} 笔${isNum(k.win_rate) ? ` · 胜率 ${pctPts(k.win_rate, 0)}` : ""}`),
    // v0.45.606：测到事件 / 事件前被平拆开单列（平仓日 ≤ 财报日 = 没测到事件；读者侧判定，账本不存）。三块 n 之和 = 已平仓。
    ...(((k.event_split?.event_missed?.n || 0) + (k.event_split?.event_unknown?.n || 0)) > 0 ? [
      kpi("其中测到事件", `${k.event_split.saw_event.n} 笔${isNum(k.event_split.saw_event.win_rate) ? ` · 胜率 ${pctPts(k.event_split.saw_event.win_rate, 0)}` : ""} · ${sMoney(k.event_split.saw_event.total_pnl_usd)}`, tone(k.event_split.saw_event.total_pnl_usd)),
      kpi("事件前被平", `${k.event_split.event_missed.n} 笔 · ${sMoney(k.event_split.event_missed.total_pnl_usd)}${k.event_split.event_unknown.n ? ` · 另有 ${k.event_split.event_unknown.n} 笔日期缺失` : ""}`, tone(k.event_split.event_missed.total_pnl_usd)),
    ] : []));

  // 盘中参考报价（第二期）：按持仓逐只拉（服务端串行取数，每只约 4–7 秒，60 秒内走缓存）
  const liveEls = {};
  const live = {};
  const status = h("span", { class: "small muted" }, "盘中参考报价：未拉取");
  let auto = false, busy = false;
  const sessionLive = () => !!state.meta?.session?.live;
  // 拉一只、写回它的卡（盘中参考块 + 冻结 mark 的估算行）；返回这只有没有拿到可用报价
  async function pullOne(t, force) {
    try { live[t] = await api(`/api/straddle/live/${encodeURIComponent(t)}${force ? "?force=1" : ""}`); }
    catch (e) { live[t] = { error: e.message || String(e) }; }
    if (!alive()) return false;
    const x = liveEls[t];
    renderLive(x.el, live[t], x.p, x.warnAt);
    if (x.estEl) renderEst(x.estEl, live[t], x.p);
    return !!live[t].available;
  }
  async function pullAll(force) {
    if (busy) return;
    busy = true; pullBtn.disabled = true;
    let ok = 0;
    const tickers = Object.keys(liveEls);
    for (const [i, t] of tickers.entries()) {
      if (!alive()) return;
      fill(status, `正在拉 ${t}（${i + 1}/${tickers.length}）…`);
      if (await pullOne(t, force)) ok += 1;
      if (!alive()) return;
    }
    const now = new Date().toLocaleTimeString("zh-CN", { timeZone: "America/New_York", hour12: false });
    fill(status, `上次拉取 ${now} ET · ${ok}/${tickers.length} 只可得${sessionLive() ? "" : " · 现在不在交易时段，报价是最近一次收盘后的"}`);
    busy = false; pullBtn.disabled = false;
  }
  const pullBtn = h("button", { class: "btn primary", type: "button", onclick: () => pullAll(true) }, "拉一次盘中报价");
  // 自动刷新必须 force：服务端两层缓存都是 60 秒，不 force 的话每隔一拍都只拿到缓存（v0.45.428 二次检查）。
  // 间隔 2 分钟：6 只 × 每只约 1.5MB、串行 4–7 秒，每分钟强刷会把 CBOE 取数占掉一半时间、挡住标的页现拉。
  const AUTO_MS = 120000;
  const autoBtn = h("button", { class: "btn", type: "button", "aria-pressed": "false",
    onclick: () => { auto = !auto; autoBtn.setAttribute("aria-pressed", String(auto)); fill(autoBtn, auto ? "自动刷新：开（盘中约每 2 分钟）" : "自动刷新：关"); if (auto) pullAll(false); } }, "自动刷新：关");
  const timer = setInterval(() => { if (!alive()) { clearInterval(timer); return; } if (auto && sessionLive()) pullAll(true); }, AUTO_MS);

  const cards = h("div", { class: "sd-cards" }, (d.positions || []).map((p) => positionCard(p, d, liveEls)));
  // 账本 mark 冻结的那几只：进页面就拉一次（只读，串行；其余仍等你点「拉一次盘中报价」），否则页上只有旧价
  (async () => {
    for (const t of Object.keys(liveEls).filter((k) => liveEls[k].estEl)) {
      if (!alive() || busy) return;
      await pullOne(t, false);
    }
  })();
  const eqEl = h("div", { class: "chart short" });
  const calEl = h("div", { class: "chart short" });
  const sig = d.signals_today || {};
  const closedCols = [
    { label: "代码", render: (r) => h("b", {}, r.ticker) },
    { label: "方向", render: (r) => h("span", {}, SIDE[r.side] || r.side, h("span", { class: "sub" }, LABEL[r.label] || "")) },
    { label: "开仓 → 平仓", render: (r) => h("span", {}, `${r.entry_date} → ${r.exit_date}`, h("span", { class: "sub" }, `持有 ${r.holding_days ?? "—"} 天 · ${EXIT[r.exit_reason] || r.exit_reason || "—"}`),
      r.event_missed === true ? h("span", { class: "chip warn", title: "平仓日不晚于财报日：财报行情发生之前就平了，这笔测到的是时间损耗，不是事件" }, "事件前被平") : null) },
    { label: "张数", cls: "r", render: (r) => r.contracts },
    { label: "权利金 入场 → 出场", cls: "r", render: (r) => `${num(r.entry_premium)} → ${num(r.exit_premium)}` },
    { label: "盈亏", cls: "r", render: (r) => h("span", { class: tone(r.pnl_usd) }, sMoney(r.pnl_usd), h("span", { class: "sub" }, signedPct(r.pnl_pct))) },
    { label: "事件波动 隐含 vs 实际", cls: "r", render: (r) => h("span", {}, `${pctPts(r.implied_event_move_pct, 2)} vs ${pctPts(r.realized_abs_move_pct, 2)}`, h("span", { class: "sub" }, isNum(r.realized_vs_implied) ? `实际 / 隐含 = ${num(r.realized_vs_implied, 2)}` : "未结算")) },
    { label: "出场价来源", render: (r) => h("span", { class: "small muted" }, MARK[r.mark_source] || r.mark_source || "—") },
  ];
  const sigCols = [
    { label: "代码", render: (r) => h("b", {}, r.ticker) },
    { label: "标签", render: (r) => LABEL[r.label] || r.label || "—" },
    // v0.45.454：事件波动触底（扣完扩散 ≤ 0）= 算不出，不是「0」。新行存的是 null；已落盘的旧行存的是 0.0，
    // 所以这里认 event_move_floor_hit，不靠值本身是不是 0。
    { label: "比值", cls: "r", render: (r) => (r.event_move_floor_hit ? "—" : num(r.ratio, 2)) },
    { label: "隐含 vs 历史中位", cls: "r", render: (r) => (r.event_move_floor_hit
      ? h("span", {}, `— vs ${pctPts(r.hist_median_abs_move_pct, 2)}（${r.hist_n ?? "—"}）`, h("span", { class: "sub" }, "扣掉扩散后 ≤ 0，算不出"))
      : `${pctPts(r.implied_event_move_pct, 2)} vs ${pctPts(r.hist_median_abs_move_pct, 2)}（${r.hist_n ?? "—"}）`) },
    { label: "最大腿点差", cls: "r", render: (r) => (isNum(r.max_leg_spread_pct) ? pctPts(r.max_leg_spread_pct * 100, 1) : "—") },
    { label: "财报", render: (r) => r.earnings_date || "—" },
    { label: "状态", render: (r) => h("span", { class: "small" }, r.status, r.untradeable_reason ? h("span", { class: "sub" }, r.untradeable_reason) : null) },
    { label: "跨式 vs 宽跨", render: (r) => h("button", { class: "btn", type: "button", onclick: () => {
      loadCompare(cmpHost, r.ticker, sig.as_of);
      cmpPanel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } }, `看 ${r.ticker} 对比`) },
  ];
  const cmpHost = h("div", { class: "cmp" }, h("div", { class: "small muted" }, "点上面「今日信号」某一行的按钮，这里并排显示这只票的 ATM 跨式与 25Δ 宽跨；只读本机快照与缓存，不联网。"));
  const cmpPanel = panel("跨式 vs 宽跨", { sub: "卖方视角，只并排事实，不给「选哪个」：系统里已结算事件还很少，任何选择规则目前都是在拟合" }, cmpHost);

  fill(root, head,
    warns.map((w) => h("p", { class: "callout warn" }, w)),
    panel("账户", { sub: `起始 ${money(a.starting_capital, 0)} · 每笔权利金上限 NAV 的 ${num(a.risk_per_trade_pct, 0)}% · 版本 ${d.freshness?.ledger_version || "—"}` }, kpis),
    h("div", { style: { height: "16px" } }),
    panel("持仓", { sub: "账本 mark 是每个扫描日收盘后的 CBOE 中间价；盘中参考另拉、不写账本", right: h("div", { class: "controls" }, status, autoBtn, pullBtn) },
      (d.positions || []).length ? cards : h("div", { class: "empty" }, "当前没有持仓。")),
    h("div", { style: { height: "16px" } }),
    h("div", { class: "grid halves" },
      panel("累计盈亏", { sub: "实线 = 累计（已实现 + 浮动），虚线 = 其中已实现；同一根 $ 轴" }, (d.equity || []).length ? eqEl : h("div", { class: "empty" }, "还没有净值记录。")),
      // v0.45.606：只看测到事件的那批（事件前被平的是时间损耗，混进来会拉低「买跨式」胜率）；剔了几笔在副标题里说清
      panel("按方向", { sub: sideSub }, table([
        { label: "方向", render: (r) => SIDE[r[0]] || r[0] }, { label: "笔数", cls: "r", render: (r) => r[1].n },
        { label: "胜率", cls: "r", render: (r) => pctPts(r[1].win_rate, 0) }, { label: "平均", cls: "r", render: (r) => signedPct(r[1].avg_pnl_pct) },
        { label: "合计", cls: "r", render: (r) => h("span", { class: tone(r[1].total_pnl_usd) }, sMoney(r[1].total_pnl_usd)) }],
      Object.entries(sideSrc)),
      h("p", { class: "note" }, `内在价值平仓 ${k.intrinsic_exits ?? 0} 笔 · 核销 ${k.written_off_exits ?? 0} 笔。样本太小时胜率没有意义，看「隐含 vs 实际」更有用。`))),
    h("div", { style: { height: "16px" } }),
    panel("已平仓", { sub: `${(d.closed || []).length} 笔` }, (d.closed || []).length ? table(closedCols, d.closed) : h("div", { class: "empty" }, "还没有平仓。")),
    h("div", { style: { height: "16px" } }),
    h("div", { class: "grid halves" },
      panel("信号校准", { sub: `每个已结算的财报事件一个点（${(d.calibration || []).length} 个）· 实心 = 开过仓` },
        (d.calibration || []).length ? calEl : h("div", { class: "empty" }, "还没有已结算的财报事件。"),
        h("p", { class: "note" }, `横轴：信号日的「隐含 ÷ 历史中位」（≥ ${num(rules.rich_ratio, 2)} 卖、≤ ${num(rules.cheap_ratio, 2)} 买）；纵轴：财报后「实际 ÷ 隐含」，< 1 说明期权给贵了（利好卖跨式）。点少时只能看个大概，不足以下结论。`)),
      shadowPanel(d.shadow, rules)),
    h("div", { style: { height: "16px" } }),
    strangleShadowPanel(d.strangle_shadow, rules),
    h("div", { style: { height: "16px" } }),
    panel(`今日信号（${sig.as_of || "—"}）`, { sub: `${sig.n_rows ?? 0} 只标的，合格 ${(sig.eligible || []).length} 只` },
      (sig.eligible || []).length ? table(sigCols, sig.eligible) : h("div", { class: "empty" }, "今天没有合格信号。"),
      (sig.ineligible_reasons || []).length ? h("p", { class: "note" }, `不合格原因：${sig.ineligible_reasons.map(([r, n]) => `${r} ${n}`).join("；")}`) : null),
    h("div", { style: { height: "16px" } }),
    cmpPanel,
    h("p", { class: "note small muted", style: { marginTop: "14px" } }, "成交约定：买入按 ask、卖出按 bid（付点差），盯市用 CBOE 中间价。没有 delta 对冲；卖跨式风险无上界，权利金上限只为让两侧规模可比，不是仓位规则。观察项，不进评分、不构成投资建议。"));

  const c = C();
  if ((d.equity || []).length) {
    const start = a.starting_capital || 0;
    lines(eqEl, { x: d.equity.map((e) => e.date.slice(5)), yFmt: (v) => sMoney(v),
      series: [{ name: "累计盈亏", data: d.equity.map((e) => (isNum(e.nav) ? e.nav - start : null)), color: c.ink, width: 2 },
        { name: "其中已实现", data: d.equity.map((e) => e.realized_cum), color: c.ink3, dash: true, step: "end" }],
      hmarks: [{ value: 0, label: "", color: c.lineStrong, type: "solid" }] });
  }
  if ((d.calibration || []).length) {
    scatter(calEl, { points: d.calibration.map((p) => ({ ...p, x: p.ratio, y: p.realized_vs_implied, label: p.ticker, filled: p.traded, signal_label: p.label })),
      xName: "隐含 ÷ 历史中位", yName: "实际 ÷ 隐含",
      vlines: [{ value: rules.cheap_ratio, label: "cheap" }, { value: rules.rich_ratio, label: "rich" }].filter((m) => isNum(m.value)),
      hlines: [{ value: 1, label: "实际 = 隐含" }],
      tip: (p) => `<b>${p.ticker}</b> 财报 ${p.earnings_date}<br>信号 ${p.first_as_of}：比值 ${num(p.ratio, 2)}（${p.signal_label || "—"}）<br>隐含 ${pctPts(p.implied_event_move_pct, 2)} · 实际 ${pctPts(p.realized_abs_move_pct, 2)}` });
  }
}
