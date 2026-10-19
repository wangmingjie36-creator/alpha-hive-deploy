// 标的 · 溢价（v0.45.492）：单票全链按 IV ÷ 期限匹配 RV 排序，找卖方溢价高的合约。
// 每个数都由 /api/premium 算好（口径见帮助页 #/help/premium）；筛选、排序、只看一个到期日也都在服务端做
// （先筛、再排、最后截断——排序放在截断之后的前端，排的就只是前 N 个）。这里只排版。
// 筛选「先验后存」：服务端接受了才记进本机；被拒就保留上一次的筛选并把服务端的原话显示出来。
import { api, h, num, pct, money, isNum, panel, seg, table, downloadCSV, pref, setPref, fill, loading,
  timeAgo, plain } from "../lib.js";
import { lines, C } from "../charts.js";
import { state } from "../app.js";

const PREF_KEY = "premium.params";
// 盘中自动刷新间隔：同跨式页（每次约 1.5MB 全链、经 CBOE 闸串行；再密会挡住别的页现拉）
const AUTO_MS = 120000;
// 开着自动刷新的票（只在内存里：顶部「重新拉取」会重建本子页，不能因此把开关悄悄关掉；刷新整页 / 重开程序则回到关）
const AUTO_ON = new Set();

export async function premium(body, d, t, routeAlive, force = false) {
  // 顶部「重新拉取」在同一路由里重建本子页、沿用同一个 routeAlive ⇒ 被换掉的旧实例仍算「活着」，它的定时器
  // 会在后台接着拉 CBOE（与新实例叠成两份）。本实例只在自己的容器还挂在页面上时才算活（v0.45.539）。
  const alive = () => routeAlive() && body.isConnected;
  let r = null;            // 最近一次被服务端接受的结果
  let pick = null;         // 只看哪个到期日（不记在本机：到期日随链变）
  let notice = null;       // {text, retry?}
  let seq = 0;             // 请求序号：只认最后发出的那一个（连点时慢的旧响应不许盖掉新的、更不许存进本机）
  let wanted = null;       // 最近一次发出、还没有结论的筛选：连着改两处时第二处要叠在第一处上，不能只叠在「已接受」上
  let auto = AUTO_ON.has(t);  // 盘中自动刷新（不记在本机：打开页面就开始定时拉 CBOE 不该是缺省）
  let pendingDraw = false;   // 自动刷新的结果到了、但用户正在输入框里打字：等离开输入框再重画
  let lastPull = null;     // 上一次自动刷新成功的美东时刻

  const reasonZh = (code) => {
    if (code === null || code === undefined || code === "") return "—";
    const s = String(code);
    const i = s.indexOf(":");
    const [k, detail] = i < 0 ? [s, ""] : [s.slice(0, i), s.slice(i + 1)];
    const label = r?.rules?.reasons?.[k];
    return label ? label.replace("{}", detail) : `${s}（未登记）`;
  };
  const stageZh = (k) => r?.rules?.stages?.[k] || k;

  const url = (prefs, refresh) => {
    const q = Object.entries({ ...prefs, ...(pick ? { expiry: pick } : {}) })
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`);
    if (refresh) q.push("refresh=1");      // 服务端先重拉一份新链再算（自动刷新不经过顶部「重新拉取」）
    else if (force) q.push("force=1");
    return `/api/premium/${encodeURIComponent(t)}?${q.join("&")}`;
  };

  // prefs = 想用的筛选；initial = 页面首次打开（用的是本机记住的那份）
  async function apply(prefs, { initial = false, note = null, refresh = false } = {}) {
    if (!r) fill(body, loading("计算溢价…"));
    const mine = ++seq;
    wanted = prefs;
    let res;
    try {
      res = await api(url(prefs, refresh));
    } catch (e) {
      if (!alive() || mine !== seq) return;
      wanted = null;                     // 被拒 / 出错：下一次改动从「已接受」的那份重新叠
      if (e.status === 400 && e.param === "expiry" && pick) {
        pick = null;
        return apply(prefs, { initial, note: `${e.message}，已取消「只看该到期日」` });
      }
      if (e.status === 400 && initial && e.param && e.param in prefs) {
        // 本机记住的旧值被拒（比如规则改过）：只删被拒的那一个，其余照旧，并明说
        const rest = { ...prefs };
        delete rest[e.param];
        setPref(PREF_KEY, rest);
        return apply(rest, { initial, note: `已删掉本机记住的 ${e.param}：${e.message}` });
      }
      notice = e.status === 400
        ? { text: `筛选没生效：${e.message}（保留上一次的筛选）` }
        : { text: `取数出错：${e.message}${r ? "（下面仍是上一次的结果）" : ""}`, retry: () => apply(prefs, { initial }) };
      show(refresh);
      return;
    }
    if (!alive() || mine !== seq) return;
    wanted = null;
    if (refresh && !res.data_available && r?.data_available) {
      // 自动刷新这一拍没拉到（开盘后 CBOE 文件常还停在上一交易日）：不拿「不可得」换掉屏幕上那份好结果，
      // 照实说出来、注明下面是几点的报价，下一拍再试
      const at = quoteAt(r.chain?.payload_last_trade_time);
      notice = { text: `自动刷新这一拍没拉到新链（${reasonZh(res.reason)}）——下面仍是${at ? `报价 ${at} ET ` : "上一次"}的结果，约 2 分钟后再试。` };
      show(true);
      return;
    }
    r = res;
    force = false;
    if (refresh) lastPull = new Date().toLocaleTimeString("zh-CN", { timeZone: "America/New_York", hour12: false });
    setPref(PREF_KEY, prefs);          // 先验后存：到这里服务端已经接受
    notice = note ? { text: note } : null;
    show(refresh);
  }

  // 自动刷新整页重画会冲掉输入框里打了一半、还没回车 / 离开的数字（onchange 要到那时才触发）
  const typing = () => { const a = document.activeElement; return !!a && a.tagName === "INPUT" && body.contains(a); };
  function show(fromAuto) {
    if (fromAuto && typing()) { pendingDraw = true; return; }
    pendingDraw = false;
    draw();
  }
  body.addEventListener("focusout", () => setTimeout(() => {
    if (alive() && pendingDraw && !typing()) show(false);
  }, 0));

  const setParam = (k, v) => apply({ ...(wanted || pref(PREF_KEY, {})), [k]: v });

  // 交易时段以服务端日历为准（/api/meta 每分钟刷新，同跨式页）；收盘后、节假日不拉
  const sessionLive = () => !!state.meta?.session?.live;
  let busy = false;
  async function autoTick() {
    if (busy || !auto || !sessionLive() || typing()) return;     // 正在打字：这一拍跳过
    busy = true;
    try { await apply(wanted || pref(PREF_KEY, {}), { refresh: true }); } finally { busy = false; }
  }
  let drawnLive = null;    // 按钮文案画出时的交易时段状态：开 / 收盘翻转而这一拍不拉时也要重画，不然文案停在旧状态
  const timer = setInterval(() => {
    if (!alive()) { clearInterval(timer); return; }
    if (pendingDraw && !typing()) show(false);         // 兜底：离开输入框的 focusout 没收到（窗口不在前台等）也要补画
    if (auto && r && !typing() && sessionLive() !== drawnLive) draw();
    autoTick();
  }, AUTO_MS);
  function autoBtn() {
    drawnLive = sessionLive();
    const label = !auto ? "自动刷新：关"
      : sessionLive() ? `自动刷新：开（盘中约每 2 分钟${lastPull ? `；上次 ${lastPull} ET` : ""}）`
        : "自动刷新：开（现在不在交易时段，暂停）";
    return h("button", { class: "btn", type: "button", "aria-pressed": String(auto),
      onclick: () => { auto = !auto; if (auto) AUTO_ON.add(t); else AUTO_ON.delete(t); draw(); if (auto) autoTick(); } }, label);
  }
  // CBOE 文件末笔成交时刻（美东、15 分钟延迟），"2026-10-16T16:00:00" → "10-16 16:00"
  const quoteAt = (s) => (typeof s === "string" && s.length >= 16 ? `${s.slice(5, 10)} ${s.slice(11, 16)}` : null);

  function noticeBox() {
    if (!notice) return null;
    return h("p", { class: "callout warn" }, notice.text,
      notice.retry ? h("button", { class: "btn", type: "button", style: { marginLeft: "10px" }, onclick: notice.retry }, "重试") : null);
  }

  function numInput(k, step, width = "72px") {
    return h("input", { class: "sel", type: "number", step, value: r.params?.[k], "aria-label": k, style: { width },
      onchange: (e) => { const v = e.target.value.trim(); if (v !== "") setParam(k, v); } });
  }

  function draw() {
    if (!r) {
      fill(body, panel(null, {}, h("div", { class: "empty" }, "溢价页没打开。"), noticeBox()));
      return;
    }
    if (!r.data_available) {
      // 自动刷新开着时整页不可得也要能关掉（下一拍可能就好了，所以不自动关）
      fill(body, noticeBox(), panel(null, {}, h("div", { class: "empty" }, "溢价不可得。"), auto ? autoBtn() : null,
        h("p", { class: "callout warn" }, `原因：${reasonZh(r.reason)}`),
        h("p", { class: "small muted" }, r.reason === "stale_vintage"
          ? "CBOE 的延迟文件还停在上一交易日（开盘后一段时间常见）——不拿昨天的链冒充今天，稍后点「重新拉取」。"
          : "期权链拿不到时整页不算，不给半张表。")));
      return;
    }
    const rules = r.rules || {};
    const hl = r.headline || {};
    const bars = r.bars || {};
    const p = r.params || {};
    const def = rules.defaults || {};
    const ch = r.chain || {};

    // ── 页头：这只票的波动率
    const rvCell = (x) => (isNum(x?.value) ? `${pct(x.value)}（${x.start?.slice(5)}–${x.end?.slice(5)}）` : `不可得（${reasonZh(x?.reason)}）`);
    const ivRank = (b) => (b?.state === "ready" ? `${num(b.rank, 0)}（百分位 ${num(b.percentile, 0)}；${b.current_date}）`
      : b?.state === "insufficient" ? `样本不足 ${b.n}/${b.min_days}` : `不可得（${reasonZh(b?.reason)}）`);
    const vrp = (b) => (b?.state === "ready" ? `IV−RV ${num(b.vrp_pts, 1)} 点，在自身历史第 ${pct(b.ts_pct, 0)} 分位（${b.label}；${b.date}）`
      : b?.state === "insufficient" ? `样本不足 ${isNum(b.n_obs) ? b.n_obs : "—"}/${b.min_obs}（${b.date} 那行 IV−RV ${num(b.vrp_pts, 1)} 点）`
        : b?.state === "missing_inputs" ? `${b.date} 那行缺数（${reasonZh(b.reason)}）` : `不可得（${reasonZh(b?.reason)}）`);
    const scanNote = (e) => `取自 ${e.scan_date} 的扫描${e.lag_sessions ? `（落后 ${e.lag_sessions} 个交易日）` : ""}${e.source ? `，${e.source}` : ""}`;
    const earn = (e) => (e?.status === "known" ? `${e.date}（预估、未确认；${scanNote(e)}）`
      : e?.status === "none" ? (e.date ? `上次财报 ${e.date} 已过（${scanNote(e)}）` : `扫描视野内没有财报（${scanNote(e)}）`)
        : `未知（${reasonZh(e?.reason)}）`);
    const kv = [
      ["RV20 / RV30", h("span", {}, rvCell(hl.rv20), " · ", rvCell(hl.rv30))],
      ["IV30 / RV30", isNum(hl.iv30_over_rv30) ? `${num(hl.iv30_over_rv30, 2)}×（IV30 ${pct(hl.iv30)}）` : `—（IV30 ${pct(hl.iv30)}）`],
      ["IV Rank", ivRank(r.history?.iv_rank)],
      ["IV−RV 自身分位", vrp(r.history?.vrp)],
      ["财报日", earn(r.earnings)],
    ];
    const head = panel("这只票的波动率", {
      sub: `报价 ${quoteAt(ch.payload_last_trade_time) ? `${quoteAt(ch.payload_last_trade_time)} ET` : ch.as_of}（${ch.session_live ? "盘中，15 分钟延迟" : "收盘后快照"}），现价 ${money(ch.underlying_price)}；日线 ${bars.source || "—"} 截至 ${bars.last_date || "—"}；取于 ${timeAgo(r.cache_age_sec)}` },
      ch.no_bids ? h("p", { class: "callout warn" }, `${reasonZh(ch.no_bids)}。到期日表的 IV / RV 照常，合约表与首选要等`,
        ch.no_bids === "off_hours_no_bids" ? "开盘（美东 9:30）后点右上角「重新拉取」，或打开下面的「自动刷新」。" : "下一份 CBOE 文件，稍后再刷新。") : null,
      !bars.available ? h("p", { class: "callout warn" }, `日线不可得（${reasonZh(bars.reason)}）：所有 RV 与 IV/RV 显示为「—」，表仍列出、不按 0 排。`) : null,
      bars.available && (bars.gaps_in_windows || []).length ? h("p", { class: "callout warn" }, `日线缺 ${bars.gaps_in_windows.join("、")}：跨过它的 RV 窗口不可得（到期日表里写着是哪几个）。`) : null,
      bars.available && bars.lag_sessions > 0 ? h("p", { class: "callout warn" }, `日线截至 ${bars.last_date}，落后 ${bars.lag_sessions} 个交易日：RV 照算，但用的是到那天为止的数据。`) : null,
      r.demo ? h("p", { class: "callout" }, "演示模式：合成链与合成日线，不读卖权账本的财报日 / IV 历史 / VRP 账本。") : null,
      table([{ label: "项", render: (x) => x[0] }, { label: "值", render: (x) => x[1] }], kv));

    // ── 控件
    const controls = panel("筛选", { sub: `价差闸与有买价是固定的；其余服务端接受后记在本机。缺省 DTE ${def.dte_min}–${def.dte_max}、|Δ| ${def.delta_min}–${def.delta_max}、OI ≥ ${def.oi_min}` },
      h("div", { class: "controls" },
        seg([["put", "put"], ["call", "call"], ["both", "两边"]], p.side, (v) => setParam("side", v), "方向"),
        h("span", { class: "small" }, "天数"), numInput("dte_min", "1", "64px"), "–", numInput("dte_max", "1", "64px"),
        h("span", { class: "small" }, "|Δ|"), numInput("delta_min", "0.01"), "–", numInput("delta_max", "0.01"),
        h("span", { class: "small" }, "OI ≥"), numInput("oi_min", "50", "84px"),
        h("button", { class: "btn", type: "button", onclick: () => { pick = null; apply({}); } }, "恢复缺省"),
        autoBtn()));

    // ── 逐到期日
    const ex = (r.expiries || []).filter((e) => e.in_window);
    const triChip = (v, yes, unk) => (v === true ? h("span", { class: "chip warn" }, yes) : v === null || v === undefined ? h("span", { class: "chip" }, unk) : "—");
    const kinkCell = (e) => {
      const k = e.kink || {};
      if (k.suspect === true) return h("span", { class: "chip warn" }, `疑似 ±${pct(k.implied_event_move, 1)}`);
      if (k.status !== "evaluated" || k.suspect === null || k.suspect === undefined) {
        return h("span", { class: "small muted", title: reasonZh(k.reason) }, k.status === "evaluated" ? "判不了" : "未参与");
      }
      return "—";
    };
    const expCols = [
      { label: "到期日", render: (e) => e.expiry, csv: (e) => e.expiry },
      { label: "天数", cls: "r", render: (e) => num(e.dte, 0), csv: (e) => e.dte },
      { label: "RV 窗口", cls: "r", render: (e) => h("span", { style: { whiteSpace: "nowrap" }, title: e.rv_start ? `${e.rv_start} → ${e.rv_end}` : "" },
        `${e.rv_n} 日`, e.rv_start ? h("span", { class: "small muted" }, ` · ${e.rv_start.slice(5)} 起`) : null), csv: (e) => `${e.rv_n} ${e.rv_start || ""}~${e.rv_end || ""}` },
      { label: "RV", cls: "r", render: (e) => (isNum(e.rv) ? pct(e.rv) : h("span", { class: "small muted", title: reasonZh(e.rv_reason) }, "不可得")), csv: (e) => e.rv ?? "" },
      { label: "ATM IV", cls: "r", render: (e) => pct(e.atm_iv), csv: (e) => e.atm_iv ?? "" },
      { label: "ATM IV/RV", cls: "r", render: (e) => (isNum(e.atm_iv_over_rv) ? `${num(e.atm_iv_over_rv, 2)}×` : "—"), csv: (e) => e.atm_iv_over_rv ?? "" },
      { label: "前向波动", cls: "r", render: (e) => pct(e.kink?.fwd_vol), csv: (e) => e.kink?.fwd_vol ?? "" },
      { label: "这段疑似事件", render: kinkCell, csv: (e) => e.kink?.suspect ?? "" },
      { label: "含事件", render: (e) => triChip(e.event_inside, `含 ±${pct(e.event_move, 1)}`, "判不了"), csv: (e) => e.event_move ?? e.event_inside ?? "" },
      { label: "财报", render: (e) => triChip(e.earnings_before_expiry, "到期前 ⚠", "未知"), csv: (e) => e.earnings_before_expiry ?? "" },
      { label: "合约", cls: "r", render: (e) => num(e.n_rows, 0), csv: (e) => e.n_rows },
    ];
    const ivEl = h("div", { class: "chart short" });
    const expPanel = panel("先挑期限：逐到期日", {
      sub: `RV 窗口 = 到期前交易日数（夹在 ${rules.rv_n_min}–${rules.rv_n_max}）；点一行只看该到期日${pick ? `（当前：${pick}，再点取消）` : ""}`,
      right: h("button", { class: "btn", type: "button", onclick: () => downloadCSV(`alphabot-${t}-premium-expiries.csv`, expCols, ex) }, "导出 CSV") },
      ex.length ? ivEl : null,
      ex.length ? table(expCols, ex, { onRow: (e) => { pick = pick === e.expiry ? null : e.expiry; apply(wanted || pref(PREF_KEY, {})); },
        rowClass: (e) => (e.expiry === pick ? "sel" : "") })
        : h("div", { class: "empty" }, "天数范围内没有到期日。"),
      h("p", { class: "note" }, "同一到期日里所有合约共用一个 RV，所以到期日内按 IV/RV 排 = 按 IV 排（排出来的是偏度，通常是虚值 put 在前）。先在这里挑「整条期限都贵」的到期日，再到下表挑行权价。"));

    // ── 逐合约（服务端已筛好、排好、截断好）
    const rows = r.rows || [];
    const signedPts = (v) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${num(Math.abs(v), 1)}` : "—");
    const flagChips = (x) => {
      const f = x.flags || {};
      const out = [];
      if (f.earnings_before_expiry === true) out.push(h("span", { class: "chip warn" }, "财报在到期前"));
      if (f.event_inside === true) out.push(h("span", { class: "chip warn" }, `含疑似事件 ±${pct(x.event_move, 1)}`));
      if (f.dte_le_2) out.push(h("span", { class: "chip warn" }, `${rules.low_dte} 天内到期`));
      if (f.stale_last_trade === true) out.push(h("span", { class: "chip" }, "本场未成交"));
      if (f.tenor_exceeds_rv_window) out.push(h("span", { class: "chip" }, "期限长于 RV 窗口"));
      return out.length ? h("span", { style: { display: "inline-flex", gap: "4px", flexWrap: "wrap" } }, out) : "—";
    };
    const nowrap = (s) => h("span", { style: { whiteSpace: "nowrap" } }, s);
    // csvOnly：表上放不下（鼠标停留可见）、CSV 里给全的列
    const cols = [
      { label: "到期日", render: (x) => nowrap(x.expiry.slice(5)), csv: (x) => x.expiry },
      { label: "天数", cls: "r", render: (x) => num(x.dte, 0), csv: (x) => x.dte },
      { label: "方向", render: (x) => x.side, csv: (x) => x.side },
      { label: "行权价", cls: "r", render: (x) => num(x.strike, x.strike % 1 ? 1 : 0), csv: (x) => x.strike },
      { label: "Δ", cls: "r", render: (x) => num(x.delta, 2), csv: (x) => x.delta },
      { label: "bid / ask", cls: "r", render: (x) => nowrap(`${num(x.bid)} / ${num(x.ask)}`), csv: (x) => `${x.bid}/${x.ask}` },
      { label: "价差", cls: "r", render: (x) => pct(x.spread_pct, 0), csv: (x) => x.spread_pct },
      { label: "IV", cls: "r", render: (x) => pct(x.iv), csv: (x) => x.iv },
      { label: "RV", csvOnly: true, csv: (x) => x.rv ?? "" },
      { label: "RV 窗口", csvOnly: true, csv: (x) => x.rv_n },
      { label: "IV/RV", cls: "r", render: (x) => (isNum(x.iv_over_rv) ? h("b", { title: `RV ${pct(x.rv)}（${x.rv_n} 日窗口）；IV−RV ${signedPts(x.iv_minus_rv_pts)} 点` }, `${num(x.iv_over_rv, 2)}×`) : "—"), csv: (x) => x.iv_over_rv ?? "" },
      { label: "IV−RV 点", csvOnly: true, csv: (x) => x.iv_minus_rv_pts ?? "" },
      { label: "偏度溢价 点", cls: "r", render: (x) => (isNum(x.skew_pts)
        ? h("span", { title: `参照行权价 ${(x.ref_strikes || []).join(" / ")}` }, signedPts(x.skew_pts))
        : h("span", { class: "small muted", title: reasonZh(x.skew_reason) }, "—")), csv: (x) => x.skew_pts ?? "" },
      { label: "年化（按 bid）", cls: "r", render: (x) => pct(x.yield_ann), csv: (x) => x.yield_ann ?? "" },
      { label: "σ 距离", cls: "r", render: (x) => num(x.sigma_distance, 2), csv: (x) => x.sigma_distance ?? "" },
      { label: "OI", cls: "r", render: (x) => num(x.oi, 0), csv: (x) => x.oi },
      { label: "成交量", csvOnly: true, csv: (x) => x.volume ?? "" },
      { label: "标记", render: flagChips, csv: (x) => Object.entries(x.flags || {}).filter(([, v]) => v === true).map(([k]) => k).join(" ") },
    ];
    const SORT_LABEL = { iv_over_rv: "IV/RV", yield_ann: "年化收益", skew_pts: "偏度溢价" };
    const sorts = (rules.sorts || ["iv_over_rv"]).map((k) => [k, SORT_LABEL[k] || k]);
    const funnelLine = (r.funnel || []).map((s) => `${stageZh(s.stage)} ${s.n}`).join(" → ");
    const rowPanel = panel(pick ? `再挑行权价：${pick}` : "再挑行权价：逐合约", {
      sub: `${r.rows_total} 个合约（行权价 × call/put）过了全部筛选${r.truncated ? `，按当前排序只列前 ${r.max_rows} 个` : ""}；年化 = bid ÷ 抵押（put 按行权价、call 按现价）× 365 ÷ 天数；鼠标停在 IV/RV、偏度溢价上看明细；CSV 含全部列（列出的这些行）`,
      right: h("div", { class: "controls" },
        seg(sorts, p.sort, (v) => setParam("sort", v), "排序"),
        h("button", { class: "btn", type: "button", onclick: () => downloadCSV(`alphabot-${t}-premium.csv`, cols, rows) }, "导出 CSV")) },
      rows.length ? table(cols.filter((c) => !c.csvOnly), rows) : h("div", { class: "empty" },
        ch.no_bids ? `没有合约：${reasonZh(ch.no_bids)}。` : `没有合约过筛选——卡在「${stageZh(r.empty_reason)}」这一道。`),
      h("p", { class: "note" }, `漏斗：${funnelLine}`),
      h("p", { class: "note" }, plain(r.disclaimer || "")));

    // ── 首选（服务端按公开规则挑好）
    const pk = r.picks || {};
    const why = (x) => [`IV/RV ${num(x.iv_over_rv, 2)}×`, `年化 ${pct(x.yield_ann)}`,
      isNum(x.sigma_distance) ? `离现价 ${num(x.sigma_distance, 2)}σ` : null, `OI ${num(x.oi, 0)}`, `价差 ${pct(x.spread_pct, 0)}`]
      .filter(Boolean).join(" · ");
    const contract = (x) => `${x.expiry.slice(5)} ${x.side} ${num(x.strike, x.strike % 1 ? 1 : 0)}（${num(x.dte, 0)} 天，Δ ${num(x.delta, 2)}）`;
    const pickCols = [
      { label: "#", cls: "r", render: (it) => it.i },
      { label: "合约", render: (it) => h("b", {}, contract(it.row)) },
      { label: "bid", cls: "r", render: (it) => num(it.row.bid) },
      { label: "理由", render: (it) => h("span", {}, why(it.row),
        it.caveats.length ? h("span", { style: { display: "inline-flex", gap: "4px", flexWrap: "wrap", marginLeft: "6px" } },
          it.caveats.map((c) => h("span", { class: c.startsWith("pick_event_inside") ? "chip warn" : "chip" }, reasonZh(c)))) : null) },
    ];
    const sideBlock = (side, b) => {
      if (!b) return null;
      const counts = Object.entries(b.excluded_counts || {}).map(([k, n]) => `${reasonZh(k)} ${n}`).join("、");
      return h("div", { style: { marginBottom: "14px" } },
        h("h3", { style: { margin: "4px 0 6px" } }, side === "put" ? "卖 put" : "卖 call"),
        b.items.length ? table(pickCols, b.items.map((it, i) => ({ ...it, i: i + 1 })))
          : h("div", { class: "empty" }, ch.no_bids ? reasonZh(ch.no_bids) : reasonZh(b.empty_reason)),
        b.excluded.length ? h("p", { class: "small" }, "IV/RV 更高但被排除：",
          b.excluded.map((e, i) => h("span", {}, i ? "；" : "", h("b", {}, contract(e.row)), ` IV/RV ${num(e.row.iv_over_rv, 2)}× — ${e.reasons.map(reasonZh).join("、")}`))) : null,
        counts ? h("p", { class: "small muted" }, `这一边 ${b.n_candidates} 张里被排除的：${counts}（一张可能有多个原因）`) : null);
    };
    const picksPanel = panel("首选", {
      sub: `规则：先排除已知有风险的（到期前有财报、${rules.low_dte} 天内到期、没有 RV），`
        + `再按 IV/RV 每 ${rules.pick_ivrv_band} 一档、档内年化收益高的在前；同一到期日只取一张，各取前 ${rules.pick_n}。`
        + "判不了的、期限结构里的疑似事件（启发式，写出大小）、最近一场还没成交的不排除，标在理由后面。" },
      sideBlock("put", pk.put), sideBlock("call", pk.call),
      h("p", { class: "note" }, "按上面的规则挑出，不是「最优」：这套排序从未被前向检验过，高 IV/RV 也可能是市场看到了你没看到的风险。"));

    fill(body, noticeBox(), head, h("div", { style: { height: "20px" } }), controls, h("div", { style: { height: "20px" } }),
      picksPanel, h("div", { style: { height: "20px" } }), expPanel, h("div", { style: { height: "20px" } }), rowPanel);
    if (ex.length) {
      const c = C();
      lines(ivEl, { x: ex.map((e) => e.expiry.slice(5)), yFmt: (x) => pct(x, 0),
        series: [{ name: "ATM IV", data: ex.map((e) => (isNum(e.atm_iv) ? e.atm_iv : null)), color: c.ink2, symbol: true },
          { name: "期限匹配 RV", data: ex.map((e) => (isNum(e.rv) ? e.rv : null)), color: c.clay, symbol: true, dash: true }] });
    }
  }

  await apply(pref(PREF_KEY, {}), { initial: true });
}
