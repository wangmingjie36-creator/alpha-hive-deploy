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
  ["positioning", "做市商与大额成交"], ["macro", "人气与板块"], ["levels", "参考价位"], ["swarm", "各蜂明细"]];

function pickers(d, ticker, onDate) {
  const tickers = d.tickers || [];
  return h("div", { class: "controls" },
    tickers.length ? h("select", { class: "sel", "aria-label": "标的", onchange: (e) => go(`#/deep/${encodeURIComponent(e.target.value)}`) },
      (tickers.includes(ticker) ? tickers : [ticker, ...tickers]).map((x) => h("option", { value: x, selected: x === ticker }, x))) : null,
    (d.dates || []).length ? h("select", { class: "sel", "aria-label": "扫描日期", onchange: (e) => onDate(e.target.value) },
      [...d.dates].reverse().map((x) => h("option", { value: x, selected: x === d.date }, x))) : null);
}

function summary(d) {
  const s = d.summary, v = s.votes, vc = v.counts, rs = s.resonance, rk = s.rank;
  const short = (n) => n.replace(/Bee.*/, "");
  const failed = d.agents.filter((a) => a.failed);
  const conflict = rs.conflict_level && rs.conflict_level !== "none"
    ? `各蜂之间有${zh(WORD, rs.conflict_level)}分歧` + (rs.arbitration_triggered ? (rs.arbitration_flipped ? "，仲裁后方向被翻转" : "，触发过仲裁、方向没变") : "")
    : "各蜂之间没有明显分歧";
  return sec("summary", "摘要",
    h("div", { class: "kpis" },
      kpi("方向", dirChip(s.direction), `${v.voters} 只蜂投票：${vc.bullish} 多 · ${vc.bearish} 空 · ${vc.neutral} 中性`),
      kpi("综合分", num(s.score, 2), s.confidence_band ? `区间 ${num(s.confidence_band[0], 1)}～${num(s.confidence_band[1], 1)}` : null),
      kpi("当天排名", isNum(rk.rank) ? `第 ${rk.rank}` : "—", isNum(rk.of) ? `名单共 ${rk.of} 只` : null),
      kpi("现价", num(s.spot, 2))),
    line(rs.detected ? `共振已触发：${num(rs.supporting_agents, 0)} 只蜂同向${zh(DIR, rs.direction)}` + (isNum(rs.boost_pct) ? `，综合分加成 ${num(rs.boost_pct, 0)}%` : "") + "。" : "共振没有触发。",
      ` ${conflict}。`,
      isNum(s.total_penalty) && s.total_penalty > 0 ? ` 综合分里扣了 ${num(s.total_penalty, 2)} 分。` : "",
      v.non_voting.length ? ` ${v.non_voting.map(short).join("、")} 不参与投票。` : "",
      v.failed.length ? ` ${v.failed.map(short).join("、")} 当天出错，不计票。` : ""),
    isNum(s.data_real_pct) && s.data_real_pct < 90 ? h("p", { class: "callout warn" }, `扫描给这一天的数据真实度打了 ${pctOf(s.data_real_pct)}，偏低，读数要打折看。`) : null,
    s.coverage_warning ? h("p", { class: "callout warn" }, s.coverage_warning) : null,
    failed.length ? h("p", { class: "callout warn" }, `这一天有 ${failed.length} 只蜂运行出错（${failed.map((a) => a.label.split(" · ")[0]).join("、")}），它们的分数和方向不是正常算出来的；报错原文在「各蜂明细」里。`) : null);
}

// 证伪条件（v0.45.777 起分三块）：扫描能判的机器条件 / Alpha Bot 自动核对的几条人工条件 / 其余需人工判断的（折叠）。
// 自动核对另走 `/api/thesis-auto`（要去 Yahoo 取季报，首次几秒），先把这一块画成「正在核对」，结果回来再整块重画。
const AUTO_CHIP = { fired: ["neg", "已触发"], partial: ["warn", "数字部分已满足"] };
const usd2 = (v) => (isNum(v) ? `$${num(v, 2)}` : "—");
const etTime = (s) => (s ? s.slice(0, 16).replace("T", " ") : "—");

// 「让 Claude 核对」（v0.45.794，只开 NVDA）：人点按钮、确认预估后才花钱；结果是核对那一刻的结论，只对最新一天显示。
// 有效结果 = 条件原文没改过、跑出了三种结论之一；出错的那条照旧算「需人工判断」。
const CL_CHIP = { fired: ["neg", "已触发"], not_fired: [null, "未触发"], unjudgeable: ["sub", "判不了"] };
function claudeValid(cl) {
  if (!cl || !cl.available || !cl.applicable || !cl.last) return [];
  return (cl.last.results || []).filter((r) => !r.stale && r.status);
}
function claudePart(cl, onStart) {
  if (!cl || !cl.supported) return null;
  if (!cl.available) return cl.reason === "demo" ? null : note(`Claude 核对：${cl.reason_zh || cl.reason || "不可用"}`);
  if (!cl.applicable) return note("Claude 联网核对的结果是核对那一刻的结论，只在最新一天显示。");
  const n = (cl.conditions || []).length;
  if (!n) return null;
  const last = cl.last, valid = claudeValid(cl), rules = cl.rules || {}, est = cl.estimate || {};
  const errors = last ? (last.results || []).filter((r) => !r.stale && !r.status) : [];
  const stale = last ? (last.results || []).filter((r) => r.stale) : [];
  const src = (r) => ((r.sources || []).length ? h("ul", { class: "small" }, r.sources.map((x) =>
    h("li", {}, h("a", { href: x.url, target: "_blank", rel: "noopener noreferrer" }, x.what || x.url), x.date ? `（${x.date}）` : ""))) : null);
  const conf = { high: "高", medium: "中", low: "低" };
  const cols = [
    { label: "级别", render: (r) => (r.level === 2 ? h("span", { class: "chip neg" }, "止损") : h("span", { class: "chip warn" }, "预警")) },
    { label: "条件", render: (r) => h("div", {}, h("b", {}, r.label || r.id), h("div", { class: "small muted" }, r.trigger || "")) },
    { label: "核对结果", render: (r) => h("div", { class: "small" }, r.finding || "—",
      r.ambiguity ? h("div", { class: "muted" }, `措辞歧义：${r.ambiguity}`) : null, src(r),
      h("div", { class: "muted" }, `依据截至 ${r.as_of || "—"}，把握${conf[r.confidence] || "未注明"}，这条花了 ${usd2(r.cost_usd)}`),
      r.from_previous ? h("div", { class: "muted" }, `这条是之前 ${etTime(r.checked_at)}（美东）那次核对的结果，最近一次没跑出结论`) : null) },
    { label: "状态", render: (r) => { const c = CL_CHIP[r.status];
      if (!c) return "—";
      return c[0] === "sub" ? h("span", { class: "sub" }, c[1]) : c[0] ? h("span", { class: `chip ${c[0]}` }, c[1]) : c[1]; } },
  ];
  const spent = isNum(cl.spent_today) ? `今天已花 ${usd2(cl.spent_today)} / 上限 ${usd2(cl.cap_usd)}（与对话抽屉共用）` : (cl.spend_error || "今天已花多少读不了");
  const running = cl.running;
  const btn = running ? h("p", { class: "callout" }, `正在核对第 ${running.done + 1} / ${running.total} 条（${running.current}）……每条约一到三分钟，可以先看别的，回来再看。`)
    : h("button", { class: "btn", type: "button", onclick: () => onStart && onStart(n, est, rules, spent) },
      `${last ? "重新" : ""}让 Claude 核对这 ${n} 条（约 ${usd2(est.low_usd)}–${usd2(est.high_usd)}）`);
  return h("div", {},
    h("h4", {}, `Claude 联网核对的 ${n} 条`, last ? h("span", { class: "sub" }, ` 上次核对 ${etTime(last.started_at)}（美东），共花 ${usd2(last.spent_usd)}`) : null),
    valid.length ? table(cols, valid) : last ? null : note("还没核对过。"),
    last && last.stopped_zh ? h("p", { class: "callout warn" }, last.stopped_zh) : null,
    last && last.state === "interrupted" ? h("p", { class: "callout warn" }, "上次核对没跑完（Alpha Bot 中途重启过），下面只有跑完的那几条。") : null,
    errors.length ? note(errors.map((r) => `${r.label || r.id}：${r.error_zh}`).join("；")) : null,
    stale.length ? note(`${stale.map((r) => r.label || r.id).join("、")}：条件原文在核对之后改过，那次结果作废、不显示。`) : null,
    cl.save_error ? h("p", { class: "callout warn" }, cl.save_error) : null,
    cl.load_error ? h("p", { class: "callout warn" }, cl.load_error) : null,
    btn,
    note(`每条一个请求（${rules.model || "Claude"}，最多联网搜 ${rules.web_search_max_uses ?? "—"} 次），点了按钮并确认预估才会花钱；${spent}。`
      + "结论来自联网搜索，来源未经本系统核实；是核对那一刻的结论，不进评分。"));
}

function thesisBlock(d, auto, cl = null, onStart = null) {
  const r = d.risks, tb = r.thesis_breaks;
  const machine = tb.filter((x) => x.machine);
  // 看的不是最新一天 ⇒ 没核对：这几条照旧归「需人工判断」，只留一句为什么
  // 条件原文改过 / 配置里已没有的那几条也没核对 ⇒ 同样归人工，只在下面说一句
  const NOT_CHECKED = new Set(["trigger_changed", "not_in_config"]);
  const all = auto && auto.available && auto.applicable ? auto.checks || [] : [];
  const checks = all.filter((c) => !NOT_CHECKED.has(c.verdict?.reason));
  const changed = all.filter((c) => c.verdict?.reason === "trigger_changed");
  const autoLabels = new Set(checks.filter((c) => c.label).map((c) => c.label));
  const clDone = claudeValid(cl);
  const clLabels = new Set(clDone.map((r) => r.label));
  const manual = tb.filter((x) => !x.machine && !autoLabels.has(x.label) && !clLabels.has(x.label));
  const undecided = machine.filter((x) => x.fired === null).length;
  const autoFired = checks.filter((c) => c.verdict?.status === "fired").length;
  const autoPartial = checks.filter((c) => c.verdict?.status === "partial").length;
  const autoCant = checks.filter((c) => c.verdict?.status === "unjudgeable").length;
  const lvl = (x) => (x.level === 2 ? h("span", { class: "chip neg" }, "止损") : h("span", { class: "chip warn" }, "预警"));
  const mCols = [
    { label: "级别", render: lvl },
    { label: "条件", render: (x) => h("b", {}, x.label || "—") },
    { label: "判据", render: (x) => (x.field ? `${x.field} ${x.op || ""} ${plain(x.value)}` : "—") },
    { label: "当前", cls: "r", render: (x) => plain(x.actual) },
    { label: "状态", render: (x) => (x.fired === true ? h("span", { class: "chip neg" }, "已触发") : x.fired === false ? "未触发"
      : h("span", { class: "sub" }, "缺数据，判不了")) },
  ];
  const aCols = [
    { label: "级别", render: (c) => (c.level ? lvl(c) : "—") },
    { label: "条件", render: (c) => h("div", {}, h("b", {}, c.label || "—"), h("div", { class: "small muted" }, c.trigger || "")) },
    { label: "核对结果", render: (c) => h("div", { class: "small" }, c.verdict?.facts || "—",
      c.source ? h("div", { class: "muted" }, `来源：${c.source}${c.fetched_at ? `，取于 ${c.fetched_at.slice(0, 16).replace("T", " ")}（美东）` : ""}`) : null) },
    { label: "状态", render: (c) => { const v = c.verdict || {}; const chip = AUTO_CHIP[v.status];
      return chip ? h("span", { class: `chip ${chip[0]}` }, chip[1]) : v.status === "not_fired" ? "未触发"
        : h("span", { class: "sub" }, v.reason_zh || "判不了"); } },
  ];
  const clFired = clDone.filter((r) => r.status === "fired").length;
  const clCant = clDone.filter((r) => r.status === "unjudgeable").length;
  const fired = r.thesis_fired + autoFired + clFired;
  const sub = ` ${tb.length} 条里已触发 ${fired} 条` + (autoPartial ? `，${autoPartial} 条数字部分已满足` : "")
    + (undecided + autoCant + clCant ? `，${undecided + autoCant + clCant} 条判不了` : "") + (manual.length ? `，${manual.length} 条需人工判断` : "");
  let autoPart;
  if (auto === null) autoPart = note("正在自动核对能按数据判的几条（季报、股息、收益率）…");
  else if (!auto.available) autoPart = auto.reason === "demo" ? null : note(`自动核对没做成：${auto.reason_zh || auto.reason || "原因不明"}。这几条照旧算「需人工判断」。`);
  else if (checks.length) {
    autoPart = h("div", {}, h("h4", {}, `Alpha Bot 自动核对的 ${checks.length} 条`), table(aCols, checks),
      note("按此刻取到的数据核对，只对最新一天做。「数字部分已满足」= 条件里能用数据判的那半已满足，另一半（如「融资无望」）仍要人工判断，不算已触发。数据只到最近一期财报。"),
      changed.length ? note(`${changed.map((c) => c.label).join("、")}：条件原文与登记时不同（阈值可能改过），这次没自动核对，归在下面需人工判断的里。`) : null);
  } else if (!auto.applicable && (auto.checks || []).length) {
    autoPart = note(`有 ${auto.checks.length} 条平时会自动核对，但看的不是最新一天（${auto.latest_date || "—"}）：自动核对用的是此刻的数据，拿它判过去那天会用上后来的信息，所以这一天不做，照旧算「需人工判断」。`);
  } else autoPart = null;
  return h("div", {},
    h("h3", {}, "什么情况下这个判断不再成立", h("span", { class: "sub" }, auto === null ? ` ${tb.length} 条` : sub)),
    tb.length || checks.length ? null : empty("这只标的当天没有记录。"),
    machine.length ? table(mCols, machine) : null,
    autoPart,
    claudePart(cl, onStart),
    manual.length ? h("details", { class: "sd-why" },
      h("summary", {}, `另有 ${manual.length} 条需人工判断，未核对（点开看）`),
      table([{ label: "级别", render: lvl }, { label: "条件", render: (x) => x.label || "—" }], manual),
      note(cl && cl.supported && cl.available ? "这些条件没有可靠的免费结构化数据，扫描与自动核对都不判；上面「让 Claude 核对」可以付费联网核对。「未核对」不等于「未触发」。"
        : "这些条件（分部收入、用户数、监管与管理层事件等）没有可靠的免费结构化数据，扫描和 Alpha Bot 都不核对。「未核对」不等于「未触发」。")) : null);
}

function risks(d) {
  const r = d.risks, ins = r.insider;
  const tradeCols = [{ label: "内部人", render: (x) => x.insider || "—" }, { label: "动作", render: (x) => x.action || "—" },
    { label: "日期", render: (x) => x.date || "—" }, { label: "股数", cls: "r", render: (x) => qty(x.shares) },
    { label: "金额", cls: "r", render: (x) => usd(x.value_usd) },
    { label: "占其持股", cls: "r", render: (x) => pctOf(x.pct_of_holding, 1) }];
  const facts = [
    isNum(r.short_pct_float) ? `做空占流通股 ${pctOf(r.short_pct_float, 1)}` + (isNum(r.days_to_cover) ? `，回补约需 ${num(r.days_to_cover, 1)} 天` : "") : null,
    isNum(r.crowding_score) ? `拥挤度 ${num(r.crowding_score, 0)} / 100` : null,
    ins && isNum(ins.sold_usd) ? `已申报的内部人交易：卖出 ${usd(ins.sold_usd)}、买入 ${usd(ins.bought_usd)}` : null,
  ].filter(Boolean);
  const tbBox = h("div", {}, thesisBlock(d, null));
  // 两份结果各自回来就整块重画；Claude 核对在跑时每 5 秒问一次本机进度（只问本机、不花钱），页面离开就停
  // err：点按钮没开始的原因（下次点再清）；pollErr：读进度失败（下一次读成功就清，v0.45.794 二次检查）
  const st = { auto: null, cl: null, err: null, pollErr: null };
  const clUrl = `/api/thesis-claude/${encodeURIComponent(d.ticker)}?date=${encodeURIComponent(d.date)}`;
  const redraw = () => {
    if (!tbBox.isConnected) return;
    const warn = [st.err, st.pollErr].filter(Boolean).join("；");
    fill(tbBox, thesisBlock(d, st.auto, st.cl, start), warn ? h("p", { class: "callout warn" }, warn) : null);
  };
  const pollCl = () => api(clUrl).then((c) => {
    st.cl = c; st.pollErr = null; redraw();
    if (c.running && tbBox.isConnected) setTimeout(pollCl, 5000);
  }).catch((e) => { st.pollErr = `读 Claude 核对状态失败（${e.message || e}）`; redraw(); });
  async function start(n, est, rules, spent) {
    const ok = confirm(`让 Claude（${rules.model || "Opus 5.5"}）联网核对 ${d.ticker} 的 ${n} 条人工证伪条件？\n\n`
      + `· 每条一个请求，最多联网搜 ${rules.web_search_max_uses ?? "—"} 次，按实际用量收 API 费用\n`
      + `· 预计共约 $${num(est.low_usd, 2)}–$${num(est.high_usd, 2)}（11-20 试跑实测单条 $0.36–0.46；搜到的网页计入输入，可能更贵）\n`
      + `· ${spent}；每条发出前按最坏情况预检，超上限就停\n· 任何一条出错（断线、可能已计费等）后面的就不发`);
    if (!ok) return;
    st.err = null;
    try { await api(`/api/thesis-claude/${encodeURIComponent(d.ticker)}`, { method: "POST", body: { confirm: true } }); }
    catch (e) { st.err = `没开始：${e.message || e}`; }
    pollCl();
  }
  api(`/api/thesis-auto/${encodeURIComponent(d.ticker)}?date=${encodeURIComponent(d.date)}`)
    .catch((e) => ({ available: false, reason: "exception", reason_zh: `请求失败（${e.message || e}）` }))
    .then((a) => { st.auto = a; redraw(); });
  pollCl();
  return sec("risks", "风险",
    h("h3", {}, `看空一方的理由（强度 ${num(r.bear_score, 1)} / 10）`),
    r.bearish_signals.length ? h("ul", {}, r.bearish_signals.map((x) => h("li", {}, x))) : empty("扫描当天没有记到看空信号。"),
    r.enough_bear_points ? null : h("p", { class: "callout warn" }, `看空理由不到 ${d.rules.min_bear_points} 条，反对意见偏少，下结论前请自己再找反例。`),
    facts.length ? line(facts.join("；"), "。") : null,
    tbBox,
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
    flowLayers(r.flow_expiries),
    note("异动成交来自延迟快照，分不出是买还是卖。和前一天比变了什么，看 ",
      h("a", { href: `#/compare/${encodeURIComponent(d.ticker)}` }, "对比页"), "；实时水平看 ",
      h("a", { href: `#/t/${encodeURIComponent(d.ticker)}/levels` }, "标的页"), "。"));
}

// 异动成交按到期日分层（v0.45.769）：每层两侧笔数与金额、哪一侧的钱多、金额最大的几笔，以及服务端算好的几条说明。
// 说明只有算得出的量（倍数、占比、行权价范围、与上一层的差）；「Call 多」不写成看涨——记录分不出买卖方向。
function flowLayers(fx) {
  if (!fx || !fx.layers || !fx.layers.length) return null;
  const item = (x) => `${x.type_zh} ${num(x.strike, 0)}（${isNum(x.volume) ? `${num(x.volume, 0)} 手` : "— 手"}，${usd(x.premium_usd)}`
    + `${isNum(x.otm_pct) ? `，${x.otm_pct > 0 ? `虚值 ${pctOf(x.otm_pct, 0)}` : x.otm_pct < 0 ? `实值 ${pctOf(-x.otm_pct, 0)}` : "平值"}` : ""}）`;
  const lead = `共 ${num(fx.count, 0)} 个合约有异动：Call ${num(fx.n_call, 0)} 个、${usd(fx.call_premium)}；Put ${num(fx.n_put, 0)} 个、${usd(fx.put_premium)}。`
    + `${isNum(fx.times) ? `按权利金算${fx.lean_zh}（${fx.call_premium >= fx.put_premium ? "Call 是 Put" : "Put 是 Call"} 的 ${num(fx.times, 1)} 倍）。` : fx.lean_zh !== "—" ? `${fx.lean_zh}。` : ""}`
    + `${isNum(fx.nearest_resistance) ? `下文的阻力位指现价上方最近的 Call 侧未平仓集中位 ${num(fx.nearest_resistance, 0)}。` : ""}`;
  return h("div", {},
    h("h3", {}, "异动成交按到期日", h("span", { class: "sub" }, ` ${fx.layers.length} 个到期日`)),
    line(lead),
    fx.layers.map((L) => h("div", { class: "flow-layer" },
      h("h3", {}, `${L.expiry}${isNum(L.days_to_exp) ? `（${num(L.days_to_exp, 0)} 天后到期）` : ""} `,
        h("span", { class: `chip ${L.lean.startsWith("call") ? "pos" : L.lean.startsWith("put") ? "neg" : ""}` }, L.lean_zh),
        h("span", { class: "sub" }, ` Call ${num(L.n_call, 0)} 个 ${usd(L.call_premium)} · Put ${num(L.n_put, 0)} 个 ${usd(L.put_premium)}`)),
      h("p", { class: "small" }, L.top.map((x, i) => h("span", { class: x.side === "call" ? "up" : "down" }, `${i ? "；" : ""}${item(x)}`)),
        L.n_more ? h("span", { class: "muted" }, `；另有 ${num(L.n_more, 0)} 个`) : null),
      L.facts.length ? h("ul", { class: "small" }, L.facts.map((f) => h("li", {}, f))) : null)),
    fx.unclassified || fx.missing_premium ? note(`${fx.unclassified ? `${fx.unclassified} 条分不出 Call / Put 或缺到期日，没计入。` : ""}`
      + `${fx.missing_premium ? `${fx.missing_premium} 个没有权利金金额，只计个数、不计金额。` : ""}`) : null,
    note("每一条是一个合约（某到期日、某行权价的 Call 或 Put）当天的成交合计，不是一笔订单。颜色只区分 Call（绿）与 Put（红），不表示看涨看跌：记录里没有买卖方向，买 Call 与卖 Call 在这里长得一样。倍数与占比都按权利金金额算。"));
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
    note(`当天所有标的共用的大盘读数：${market.join("，")}。`),
    vixTerm(m));
}

// 做市商与大额成交（v0.45.771）。两半都是推算：做市商一半建立在「净多 Call、净空 Put」的假设上；大额成交一半只按规模定义、
// 看不到身份与买卖方向。前提各说一次（服务端给的原文），说明句也由服务端算好，页面只排版。
function positioning(d) {
  const p = d.positioning;
  if (!p) return null;
  const dl = p.dealer, bg = p.big;
  const sideChip = (x) => h("span", { class: `chip ${x.side === "call" ? "pos" : "neg"}` }, x.side === "call" ? "CALL" : "PUT");
  const contract = [{ label: "侧", render: sideChip }, { label: "行权价", cls: "r", render: (x) => num(x.strike, 2) }, { label: "到期", render: (x) => x.expiry || "—" }];
  const expCols = [{ label: "到期日", render: (x) => h("span", {}, x.expiry || "—", isNum(x.days_to) ? h("span", { class: "sub" }, ` ${num(x.days_to, 0)} 天后`) : null) },
    { label: "占所列到期日 gamma", cls: "r", render: (x) => pctOf(x.share_pct, 0) },
    { label: "Call 未平仓", cls: "r", render: (x) => qty(x.call_oi) }, { label: "Put 未平仓", cls: "r", render: (x) => qty(x.put_oi) }];
  const topCols = [...contract, { label: "成交量", cls: "r", render: (x) => num(x.volume, 0) },
    { label: "当时未平仓", cls: "r", render: (x) => num(x.oi, 0) },
    { label: "量 ÷ 未平仓", cls: "r", render: (x) => (isNum(x.vol_oi) ? `${num(x.vol_oi, 2)}×` : "—") },
    { label: "成交权利金", cls: "r", render: (x) => usd(x.premium_usd) }];
  const followCols = [...contract, { label: "上次成交量", cls: "r", render: (x) => num(x.volume, 0) },
    { label: "未平仓：上次 → 这次", cls: "r", render: (x) => (isNum(x.oi_before) && isNum(x.oi_after) ? `${num(x.oi_before, 0)} → ${num(x.oi_after, 0)}` : "—") },
    { label: "增减 ÷ 成交量", cls: "r", render: (x) => (isNum(x.kept_ratio) ? h("span", { class: tone(x.kept_ratio) }, signedPct(x.kept_ratio * 100, 0)) : "—") },
    { label: "结论", render: (x) => x.verdict_zh }];
  const moverCols = [...contract, { label: "未平仓：上次 → 这次", cls: "r", render: (x) => `${num(x.oi_before, 0)} → ${num(x.oi_after, 0)}` },
    { label: "增减", cls: "r", render: (x) => h("span", { class: tone(x.oi_change) }, `${x.oi_change > 0 ? "+" : "−"}${num(Math.abs(x.oi_change), 0)}`) }];
  const facts = (xs) => (xs && xs.length ? h("ul", {}, xs.map((f) => h("li", {}, f))) : null);
  const dealer = dl.available
    ? [h("div", { class: "kpis" },
      kpi("净 gamma", zh(WORD, dl.regime), isNum(dl.total_gex_musd) ? `${num(dl.total_gex_musd, 0)} 百万美元 / 每 1 美元` : null),
      kpi("翻转位", num(dl.flip, 0), isNum(dl.flip_dist_pct) ? `距现价 ${signedPct(dl.flip_dist_pct, 1)}` : null),
      kpi("Call 侧集中位", num(dl.call_wall, 0), isNum(dl.call_wall_dist_pct) ? `距现价 ${signedPct(dl.call_wall_dist_pct, 1)}` : null),
      kpi("Put 侧集中位", num(dl.put_wall, 0), isNum(dl.put_wall_dist_pct) ? `距现价 ${signedPct(dl.put_wall_dist_pct, 1)}` : null)),
    facts(dl.facts),
    (dl.expiries || []).length ? table(expCols, dl.expiries) : null,
    note("这里的净 gamma 是每日扫描收盘后按全部到期日算的；", h("a", { href: `#/t/${encodeURIComponent(d.ticker)}/levels` }, "标的页"),
      "的净 GEX 是盘中现算、默认看 45 天内到期，单位是每 1%。两处口径不同，数值乃至正负不一样都是正常的（现价贴近翻转位时尤其如此）。")]
    : [line(`这一天扫描没有可用的 gamma 读数（${dl.reason_zh || dl.reason || "原因不明"}），做市商一侧不做推算。`)];
  const fo = bg.follow || {};
  // 未平仓隔夜才更新：某次扫描记的是它前一个交易日收盘时的持仓 ⇒ 两次扫描之差 = 上一次扫描那天（含）到这一次前一天的开平仓
  const when = !fo.prev_date ? "" : (fo.sessions_covered === 1 ? `这些增减发生在 ${fo.prev_date} 那个交易日。`
    : `这些增减发生在 ${fo.prev_date}（含）到 ${d.date}（不含）之间的 ${isNum(fo.sessions_covered) ? `${num(fo.sessions_covered, 0)} 个工作日` : "交易日"}里。`)
    + `${d.date} 当天的开平仓要到下一次扫描才看得到。`;
  return sec("positioning", "做市商与大额成交",
    h("h3", {}, "做市商对冲"), note(dl.assumption), ...dealer,
    h("h3", {}, "大额成交", h("span", { class: "sub" }, ` 当天成交权利金 ≥ ${usd(bg.threshold_usd)} 的合约`)), note(bg.caveat),
    facts(bg.facts),
    bg.top.length ? h("div", {}, h("h3", {}, "今天金额最大的几个合约", bg.n_more ? h("span", { class: "sub" }, ` 另有 ${num(bg.n_more, 0)} 个`) : null), table(topCols, bg.top)) : null,
    fo.available && fo.rows.length ? h("div", {}, h("h3", {}, `上一次（${fo.prev_date || "—"}）的大额成交，这一次还在不在`,
      fo.n_more ? h("span", { class: "sub" }, ` 按金额列前 ${fo.rows.length} 个，另有 ${num(fo.n_more, 0)} 个`) : null), table(followCols, fo.rows),
      note(when, "未平仓的增减是这个合约全部参与者合计的结果，不只是那天成交的人；所以「留下」说的是这个合约上的净持仓增加了，不是某一方没走。")) : null,
    (bg.oi_movers || []).length ? h("div", {}, h("h3", {}, "未平仓增减最多的合约", h("span", { class: "sub" }, ` 全部到期日，和 ${fo.prev_date || "上一次"} 比`)),
      table(moverCols, bg.oi_movers),
      note(when, "增加 = 新开的仓多于平掉的；减少 = 平掉的多。同一到期日里一档减少、相邻一档增加，常见于移仓，但数据里对不出是不是同一批人。")) : null);
}

// VIX 期限结构（v0.45.769）。扫描只记了形态标签，没记两个月份的点位 ⇒ 不写点位与价差；没取到就写没取到，不给结论
function vixTerm(m) {
  const vix = isNum(m.vix) ? `当天 VIX ${num(m.vix, 1)}${m.vix_stale ? "（数据陈旧）" : ""}，` : "";
  if (!m.vix_term || m.vix_term === "unknown") return note(`${vix}VIX 期限结构这一天没取到。`);
  if (!m.vix_term_zh) return note(`${vix}VIX 期限结构记的是「${m.vix_term}」，这个标签页面还不认识，不配说明。`);
  return h("p", { class: "note small" }, h("b", {}, "VIX 期限结构："), `${vix}形态为`, h("b", {}, m.vix_term_zh), `。${m.vix_term_read}`,
    m.vix_term_source && m.vix_term_source !== "live" ? `（来源：${m.vix_term_source}，不是当天现取的）` : "",
    " 扫描只记了形态，没记近月与远月各自的点位。");
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
  const routed = d.dimensions.some((x) => isNum(x.base_weight) && isNum(x.weight) && Math.abs(x.base_weight - x.weight) >= 0.005);
  const dimCols = [
    { label: "维度", render: (r) => h("b", {}, r.label) },
    { label: "分数", cls: "r", render: (r) => num(r.score, 2) },
    ...(routed ? [{ label: "基础权重", cls: "r", render: (r) => (isNum(r.base_weight) ? pctOf(r.base_weight * 100) : "—") }] : []),
    { label: routed ? "当天权重" : "权重", cls: "r", render: (r) => (r.in_score ? pctOf(r.weight * 100)
      : h("span", { class: "sub", title: r.missing_reason || "" }, r.live ? "0，不计分" : "当天缺这一维")) },
    { label: "数据质量", cls: "r", render: (r) => pctOf(r.data_quality) },
  ];
  const yesNo = (ok) => (ok ? "是" : h("span", { class: "sub" }, "否"));
  const agCols = [
    { label: "蜂", render: (r) => h("span", {}, h("b", {}, r.label), r.failed ? h("span", { class: "chip neg" }, "出错") : null) },
    { label: "方向", render: (r) => dirChip(r.direction) },
    { label: "分数", cls: "r", render: (r) => num(r.score, 2) },
    { label: "投票", render: (r) => yesNo(r.votes) },
    { label: "进综合分", render: (r) => (r.in_score ? `是，${pctOf(r.weight * 100)}` : yesNo(false)) },
  ];
  const noteCols = [{ label: "蜂", render: (r) => h("b", {}, r.label) },
    { label: "扫描时记下的要点", render: (r) => (r.findings.length ? r.findings.join(" · ") : "—") }];
  // 综合分各步：只显示第一步、最后一步，和中间真变了的步
  const steps = d.pipeline.filter((x, i) => i === 0 || i === d.pipeline.length - 1 || x.change);
  const stepCols = [{ label: "步骤", render: (r) => h("b", {}, r.label) }, { label: "分数", cls: "r", render: (r) => num(r.score, 2) },
    { label: "变化", cls: "r", render: (r) => (isNum(r.change) && r.change !== 0 ? h("span", { class: tone(r.change) }, `${r.change > 0 ? "+" : "−"}${num(Math.abs(r.change), 2)}`) : "") }];
  const off = d.side_effects.filter((x) => !x.applied && (x.key === "ml_auxiliary" || x.key === "gex_direct")).map((x) => x.label);
  const on = d.side_effects.filter((x) => x.applied).map((x) => x.label.split("（")[0]);
  const inScore = d.agents.filter((a) => a.in_score).map((a) => a.label.split(" · ")[0]);
  return sec("swarm", "各蜂明细",
    d.agents.length ? table(agCols, d.agents) : line("这一天没有各蜂的记录。"),
    d.agents.length ? note(inScore.length ? `当天进综合分的只有 ${inScore.join("、")}；其余的蜂有分数，但权重为 0、不在五个维度里，或当天出错。` : "当天没有任何一只蜂的权重大于 0。",
      on.length ? ` 另外动了综合分的：${on.join("、")}。` : "",
      off.length ? ` 算了但没用进综合分的：${off.join("、")}。` : "") : null,
    h("h3", {}, "五个维度"), table(dimCols, d.dimensions),
    steps.length >= 2 ? h("div", {}, h("h3", {}, "综合分是怎么来的"), table(stepCols, steps)) : null,
    d.agents.length ? h("details", { class: "sd-why" }, h("summary", {}, `展开 ${d.agents.length} 只蜂各自记下的要点`), table(noteCols, d.agents)) : null);
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
    fill(root, head, toc, summary(d), risks(d), catalysts(d), options(d), positioning(d), macro(d), levels(d), swarm(d));
  }
  await draw();
}
