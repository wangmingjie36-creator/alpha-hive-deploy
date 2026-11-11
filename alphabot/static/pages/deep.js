// 深度页：单票深度报告（v0.45.760）。把每日扫描对一只标的记下的数字与事实排出来，接替已退役的深度报告生成器。
// 内容全由服务端整理（alphabot/deep_report.py），前端只排版。版式跟着内容走：只有一个事实就写一句话，要比较才用表。
// 提醒只在页面顶部说一次；各板块只留它独有的那一条。取不到的一律显示「—」。
import { api, h, helpLink, num, signedPct, panel, table, isNum, fill } from "../lib.js";
import { go } from "../app.js";

const DIR = { bullish: "看多", bearish: "看空", neutral: "中性" };
const WORD = { risk_on: "偏进攻", risk_off: "偏防守", neutral: "中性", bull: "向上", bear: "向下",
  negative_gex: "负 gamma", positive_gex: "正 gamma", backwardation: "倒挂", contango: "正向",
  none: "无", moderate: "中度", heavy: "重度" };
const zh = (map, v) => map[v] || v || "—";
const dirChip = (d) => h("span", { class: `chip ${d === "bullish" ? "pos" : d === "bearish" ? "neg" : ""}` }, zh(DIR, d));
const tone = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const pctOf = (v, dp = 0) => (isNum(v) ? `${num(v, dp)}%` : "—");
// 金额写成人读的量级：约 3.07 亿美元 / 679 万美元（原始记录精确到个位，页面不需要）
const usd = (v) => (!isNum(v) ? "—" : Math.abs(v) >= 1e8 ? `${num(v / 1e8, 2)} 亿美元` : Math.abs(v) >= 1e4 ? `${num(v / 1e4, 0)} 万美元` : `${num(v, 0)} 美元`);
const qty = (v) => (!isNum(v) ? "—" : v >= 1e8 ? `${num(v / 1e8, 2)} 亿` : v >= 1e4 ? `${num(v / 1e4, 0)} 万` : num(v, 0));
// 判据里的数：整数不带小数（「看空信号 >= 7」），其余两位
const plain = (v) => (!isNum(v) ? "—" : Number.isInteger(v) ? num(v, 0) : num(v, 2));
const kpi = (k, v, sub, cls = "") => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
  sub ? h("div", { class: `small ${cls || "muted"}` }, sub) : null);
const empty = (text) => h("div", { class: "empty" }, text);
const line = (...kids) => h("p", {}, ...kids);
const note = (...kids) => h("p", { class: "note small muted" }, ...kids);
const sec = (id, title, ...body) => { const p = panel(title, {}, ...body); p.id = `deep-${id}`; return p; };

const SECTIONS = [["summary", "摘要"], ["risks", "风险"], ["catalysts", "催化剂"], ["options", "期权要点"],
  ["macro", "人气与板块"], ["levels", "参考价位"], ["swarm", "各蜂明细"]];

function pickers(d, ticker, onDate) {
  const tickers = d.tickers || [];
  return h("div", { class: "controls" },
    tickers.length ? h("select", { class: "sel", "aria-label": "标的", onchange: (e) => go(`#/deep/${encodeURIComponent(e.target.value)}`) },
      (tickers.includes(ticker) ? tickers : [ticker, ...tickers]).map((x) => h("option", { value: x, selected: x === ticker }, x))) : null,
    (d.dates || []).length ? h("select", { class: "sel", "aria-label": "扫描日期", onchange: (e) => onDate(e.target.value) },
      [...d.dates].reverse().map((x) => h("option", { value: x, selected: x === d.date }, x))) : null);
}

function summary(d) {
  const s = d.summary, v = s.votes, rs = s.resonance, rk = s.rank;
  const failed = d.agents.filter((a) => a.failed);
  const conflict = rs.conflict_level && rs.conflict_level !== "none"
    ? `各蜂之间有${zh(WORD, rs.conflict_level)}分歧` + (rs.arbitration_triggered ? (rs.arbitration_flipped ? "，仲裁后方向被翻转" : "，触发过仲裁、方向没变") : "")
    : "各蜂之间没有明显分歧";
  return sec("summary", "摘要",
    h("div", { class: "kpis" },
      kpi("方向", dirChip(s.direction), `${num(v.bullish, 0)} 只看多 · ${num(v.bearish, 0)} 只看空 · ${num(v.neutral, 0)} 只中性`),
      kpi("综合分", num(s.score, 2), s.confidence_band ? `区间 ${num(s.confidence_band[0], 1)}～${num(s.confidence_band[1], 1)}` : null),
      kpi("当天排名", isNum(rk.rank) ? `第 ${rk.rank}` : "—", isNum(rk.of) ? `名单共 ${rk.of} 只` : null),
      kpi("现价", num(s.spot, 2))),
    line(rs.detected ? `共振已触发：${num(rs.supporting_agents, 0)} 只蜂同向${zh(DIR, rs.direction)}。` : "共振没有触发。",
      ` ${conflict}。`,
      isNum(s.total_penalty) && s.total_penalty > 0 ? ` 综合分里扣了 ${num(s.total_penalty, 2)} 分。` : ""),
    isNum(s.data_real_pct) && s.data_real_pct < 90 ? h("p", { class: "callout warn" }, `扫描给这一天的数据真实度打了 ${pctOf(s.data_real_pct)}，偏低，读数要打折看。`) : null,
    s.coverage_warning ? h("p", { class: "callout warn" }, s.coverage_warning) : null,
    failed.length ? h("p", { class: "callout warn" }, `这一天有 ${failed.length} 只蜂运行出错（${failed.map((a) => a.label.split(" · ")[0]).join("、")}），它们的分数和方向不是正常算出来的；报错原文在「各蜂明细」里。`) : null);
}

function risks(d) {
  const r = d.risks, ins = r.insider, tb = r.thesis_breaks;
  // 没核对有两种：本来就要人判断的（machine = false），和有判据但当天缺数判不了的——分开数、分开写
  const manual = tb.filter((x) => !x.machine).length;
  const undecided = tb.filter((x) => x.machine && x.fired === null).length;
  const tbCols = [
    { label: "级别", render: (x) => (x.level === 2 ? h("span", { class: "chip neg" }, "止损") : h("span", { class: "chip warn" }, "预警")) },
    { label: "条件", render: (x) => h("b", {}, x.label || "—") },
    { label: "判据", render: (x) => (x.machine && x.field ? `${x.field} ${x.op || ""} ${plain(x.value)}` : "需人工判断") },
    { label: "当前", cls: "r", render: (x) => plain(x.actual) },
    { label: "状态", render: (x) => (x.fired === true ? h("span", { class: "chip neg" }, "已触发") : x.fired === false ? "未触发"
      : h("span", { class: "sub" }, x.machine ? "缺数据，判不了" : "未核对")) },
  ];
  const tradeCols = [{ label: "内部人", render: (x) => x.insider || "—" }, { label: "动作", render: (x) => x.action || "—" },
    { label: "日期", render: (x) => x.date || "—" }, { label: "股数", cls: "r", render: (x) => qty(x.shares) },
    { label: "金额", cls: "r", render: (x) => usd(x.value_usd) },
    { label: "占其持股", cls: "r", render: (x) => pctOf(x.pct_of_holding, 1) }];
  const facts = [
    isNum(r.short_pct_float) ? `做空占流通股 ${pctOf(r.short_pct_float, 1)}` + (isNum(r.days_to_cover) ? `，回补约需 ${num(r.days_to_cover, 1)} 天` : "") : null,
    isNum(r.crowding_score) ? `拥挤度 ${num(r.crowding_score, 0)} / 100` : null,
    ins && isNum(ins.sold_usd) ? `已申报的内部人交易：卖出 ${usd(ins.sold_usd)}、买入 ${usd(ins.bought_usd)}` : null,
  ].filter(Boolean);
  return sec("risks", "风险",
    h("h3", {}, `看空一方的理由（强度 ${num(r.bear_score, 1)} / 10）`),
    r.bearish_signals.length ? h("ul", {}, r.bearish_signals.map((x) => h("li", {}, x))) : empty("扫描当天没有记到看空信号。"),
    r.enough_bear_points ? null : h("p", { class: "callout warn" }, `看空理由不到 ${d.rules.min_bear_points} 条，反对意见偏少，下结论前请自己再找反例。`),
    facts.length ? line(facts.join("；"), "。") : null,
    h("h3", {}, "什么情况下这个判断不再成立",
      h("span", { class: "sub" }, ` ${tb.length} 条里已触发 ${r.thesis_fired} 条` + (manual ? `，${manual} 条需人工判断` : "") + (undecided ? `，${undecided} 条当天缺数据判不了` : ""))),
    tb.length ? table(tbCols, tb) : empty("这只标的当天没有记录。"),
    manual ? note("需人工判断的条件（如财报数据、管理层变动）扫描不会去核对。「未核对」不等于「未触发」。") : null,
    ins && ins.trades.length ? h("div", {}, h("h3", {}, "内部人交易"), table(tradeCols, ins.trades)) : null);
}

function catalysts(d) {
  const c = d.catalysts, t = c.analyst_targets;
  const cols = [
    { label: "事件", render: (r) => h("b", {}, r.event || "—") }, { label: "日期", render: (r) => r.date || "—" },
    { label: "还有", cls: "r", render: (r) => (isNum(r.days_until) ? `${num(r.days_until, 0)} 天` : "—") },
    { label: "重要度", render: (r) => (r.severity === "critical" ? h("span", { class: "chip warn" }, "关键") : (r.severity || "—")) },
  ];
  const eps = isNum(c.forward_eps) && isNum(c.trailing_eps)
    ? `每股收益过去 12 个月 ${num(c.trailing_eps, 2)}，卖方预期未来 12 个月 ${num(c.forward_eps, 2)}。` : "";
  return sec("catalysts", "催化剂",
    c.events.length ? table(cols, c.events) : line("扫描当天没有记到未来的事件。"),
    t ? line(`${isNum(c.analysts) ? `${num(c.analysts, 0)} 位分析师的` : "分析师"}目标价均值 ${num(t.mean, 0)}`,
      isNum(c.target_upside_pct) ? `，比现价${c.target_upside_pct >= 0 ? "高" : "低"} ${pctOf(Math.abs(c.target_upside_pct))}` : "",
      isNum(t.low) && isNum(t.high) ? `；最低 ${num(t.low, 0)}，最高 ${num(t.high, 0)}` : "", "。 ", eps) : (eps ? line(eps) : null),
    t ? note("目标价是卖方的公开预期，常滞后于股价。") : null);
}

function options(d) {
  const r = d.options.read, g = d.options.gex, fs = r.flow_summary;
  const flowCols = [
    { label: "侧", render: (x) => h("span", { class: `chip ${x.side === "call" ? "pos" : "neg"}` }, x.side === "call" ? "CALL" : "PUT") },
    { label: "行权价", cls: "r", render: (x) => num(x.strike, 2) }, { label: "到期", render: (x) => x.expiry || "—" },
    { label: "成交量", cls: "r", render: (x) => num(x.volume, 0) },
    { label: "权利金金额", cls: "r", render: (x) => usd(x.premium_usd) },
  ];
  if (!r.options_available) return sec("options", "期权要点", line("这一天这只标的没有期权分析记录。"));
  return sec("options", "期权要点",
    h("div", { class: "kpis" },
      kpi("隐含 / 实际波动率", `${isNum(r.iv_current) ? num(r.iv_current, 0) : "—"} / ${isNum(r.rv_30d) ? num(r.rv_30d, 0) : "—"}`,
        isNum(r.iv_rank) ? h("span", {}, `IV Rank ${num(r.iv_rank, 0)}`, r.iv_rank_source === "hv_proxy" ? "（用历史波动率顶替算的）" : "") : null),
      kpi("P/C（全链）", num(r.full_pc_ratio, 2), isNum(r.full_oi) ? `未平仓 ${qty(r.full_oi)}张` : null),
      kpi("Skew（虚值 put ÷ call）", num(r.skew, 2)),
      kpi("Gamma 环境", g.available ? zh(WORD, g.regime) : "不可得", g.available && isNum(g.flip) ? `翻转位 ${num(g.flip, 0)}` : null)),
    r.flows.length ? h("div", {}, h("h3", {}, "金额最大的异动成交", h("span", { class: "sub" }, ` 共 ${fs.count} 条，call 占成交量的 ${pctOf(fs.call_pct)}`)),
      table(flowCols, r.flows)) : null,
    note("异动成交来自延迟快照，分不出是买还是卖。和前一天比变了什么，看 ",
      h("a", { href: `#/compare/${encodeURIComponent(d.ticker)}` }, "对比页"), "；实时水平看 ",
      h("a", { href: `#/t/${encodeURIComponent(d.ticker)}/levels` }, "标的页"), "。"));
}

function macro(d) {
  const m = d.macro, sd = m.sentiment_deltas, sc = m.sector;
  const layerCols = [{ label: "", render: (r) => h("b", {}, r.label) }, { label: "趋势", render: (r) => zh(WORD, r.regime) },
    { label: "依据", render: (r) => r.detail || "—" }];
  const peerCols = [{ label: "同业 / 上下游", render: (r) => h("span", {}, h("b", {}, r.symbol || "—"), r.name ? h("span", { class: "sub" }, ` ${r.name}`) : null) },
    { label: "5 日涨跌", cls: "r", render: (r) => h("span", { class: tone(r.return_5d_pct) }, signedPct(r.return_5d_pct, 1)) },
    { label: "比本标的", cls: "r", render: (r) => h("span", { class: tone(r.vs_target_pct) }, signedPct(r.vs_target_pct, 1)) }];
  const market = [`大盘环境${zh(WORD, m.regime)}`, isNum(m.vix) ? `VIX ${num(m.vix, 1)}` + (m.vix_stale ? "（数据陈旧）" : "") : null,
    isNum(m.fear_greed) ? `恐惧与贪婪指数 ${num(m.fear_greed, 0)}` + (m.fear_greed_real === false ? "（非真实数据）" : "") : null,
    ...m.calendar].filter(Boolean);
  return sec("macro", "人气与板块",
    h("div", { class: "kpis" },
      kpi("5 日涨跌", h("span", { class: tone(m.momentum_5d) }, signedPct(m.momentum_5d, 1)), isNum(m.volume_ratio) ? `成交量是平时的 ${num(m.volume_ratio, 1)} 倍` : null),
      sc ? kpi(`比${sc.name || "板块"}（${sc.etf || "—"}）`, h("span", { class: tone(sc.relative) }, isNum(sc.relative) ? `${sc.relative > 0 ? "+" : "−"}${num(Math.abs(sc.relative), 1)} 个点` : "—"),
        `20 日：个股 ${signedPct(sc.stock_ret_20d, 1)}，板块 ${signedPct(sc.sector_ret_20d, 1)}`) : null,
      kpi("情绪", pctOf(m.sentiment_pct), isNum(sd.delta_7d) ? `比一周前 ${sd.delta_7d > 0 ? "+" : sd.delta_7d < 0 ? "−" : ""}${num(Math.abs(sd.delta_7d), 0)}` : null),
      kpi("Reddit 热度", isNum(m.reddit_rank) ? `第 ${num(m.reddit_rank, 0)}` : "—", isNum(m.reddit_mentions) ? `${num(m.reddit_mentions, 0)} 次提及` : null)),
    m.layers.length ? table(layerCols, m.layers) : null,
    m.peers.length ? table(peerCols, m.peers) : null,
    note(`当天所有标的共用的大盘读数：${market.join("，")}。`));
}

function levels(d) {
  const s = d.levels, im = s.implied;
  const cols = [{ label: "价位", render: (r) => h("b", {}, r.label) }, { label: "价格", cls: "r", render: (r) => num(r.price, 2) },
    { label: "距现价", cls: "r", render: (r) => h("span", { class: tone(r.dist_pct) }, signedPct(r.dist_pct, 1)) }];
  return sec("levels", "参考价位",
    im ? line(`期权市场给到 ${im.expiry || "—"}（${num(im.dte, 0)} 天后）的定价是上下 ${pctOf(im.pct, 1)}，即 ${num(im.down, 0)}～${num(im.up, 0)}。`,
      "这是平值跨式的价格除以现价，只说明幅度。") : line("这一天没有可用的平值跨式报价，算不出隐含幅度。"),
    s.rows.length ? table(cols, s.rows) : null);
}

function swarm(d) {
  const dimCols = [
    { label: "维度", render: (r) => h("b", {}, r.label) },
    { label: "分数", cls: "r", render: (r) => num(r.score, 2) },
    { label: "权重", cls: "r", render: (r) => (r.in_score ? pctOf(r.weight * 100) : h("span", { class: "sub" }, "不计分")) },
    { label: "数据质量", cls: "r", render: (r) => pctOf(r.data_quality) },
  ];
  const agCols = [
    { label: "蜂", render: (r) => h("span", {}, h("b", {}, r.label), r.votes ? null : h("span", { class: "sub" }, " 不投票"),
      r.failed ? h("span", { class: "chip neg" }, "出错") : null) },
    { label: "方向", render: (r) => dirChip(r.direction) },
    { label: "分数", cls: "r", render: (r) => num(r.score, 2) },
    { label: "扫描时记下的要点", render: (r) => (r.findings.length ? r.findings.join(" · ") : "—") },
  ];
  const out = d.dimensions.filter((x) => !x.in_score).map((x) => x.label);
  return sec("swarm", "各蜂明细",
    table(dimCols, d.dimensions),
    out.length ? note(`${out.join("、")}当天权重为 0，分数只供参考，不进综合分。`) : null,
    d.agents.length ? h("details", { class: "sd-why" }, h("summary", {}, `展开 ${d.agents.length} 只蜂各自的记录`), table(agCols, d.agents))
      : line("这一天没有各蜂的明细记录。"));
}

export async function renderDeep(root, ticker, alive) {
  let date = null;
  async function draw() {
    const d = await api(`/api/deep/${encodeURIComponent(ticker)}${date ? `?date=${encodeURIComponent(date)}` : ""}`);
    if (!alive()) return;
    date = d.date || date;
    const head = h("div", { class: "page-head" },
      h("div", {},
        h("h1", {}, `深度 · ${d.ticker}`, h("span", { class: "sub" }, d.date ? `扫描 ${d.date}` : "")),
        h("p", { class: "lede" }, "每日扫描收盘后对这只标的记下的数字与事实。综合分和方向没有被证实能预测涨跌，这页用来看清依据，不构成投资建议。 ", helpLink("deep"))),
      pickers(d, d.ticker, (v) => { date = v; draw(); }));
    if (!d.available) { fill(root, head, empty(d.reason_zh || "没有可显示的数据。")); return; }
    const toc = h("p", { class: "small" }, SECTIONS.map(([id, name], i) => h("span", {}, i ? " · " : "",
      h("a", { href: "javascript:void 0", onclick: () => document.getElementById(`deep-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" }) }, name))));
    fill(root, head, toc, summary(d), risks(d), catalysts(d), options(d), macro(d), levels(d), swarm(d));
  }
  await draw();
}
