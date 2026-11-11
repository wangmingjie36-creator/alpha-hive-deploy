// 标的「价格」页的盘中 1 分钟 K 线（v0.45.763）。
//
// 只画单日：哪一天的价格只叠那一天盘中快照里的水平路径；今天、且这只票没有快照时，另画此刻的水平线。
// 活性 = 路由活 且 自己这块还挂着（切回日线 / 换标的会重建，旧实例的定时器靠它自己停）。
//
// 刷新的规矩：
//   · 只在看的是今天、且在交易时段时续刷，间隔由服务端给（`refresh_sec`）；服务端自己另有限频，页面刷得再勤也不会多发请求；
//   · 被 Yahoo 限流 ⇒ 停掉续刷，交给共用的 retry_gate：每一轮限流最多自动重试一次，仍被限流就停下、交给用户点「再试」
//     （连着撞只会多挨几条 429，还会连累 Alpha Bot 里别的走 Yahoo 的功能）；
//   · 定时器只有一个心跳（setInterval），到点才取数；页面不在了它自己清掉。
import { api, h, num, signedPct, panel, isNum, fill } from "../lib.js";
import { candles, priceRange, release, C } from "../charts.js";
import { state } from "../app.js";
import { createRetryGate } from "../retry_gate.js";

const TICK_MS = 5000;
const RETRY_NOTE = {
  scheduled: "冷却一结束会自动再试一次。",
  tried: "已自动再试过一次、仍被限流，不再自动重试；过一会儿点「再试」。",
  manual: "要等的时间较长，不自动重试；过一会儿点「再试」。",
  none: "",
};

export async function minutePrice(body, d, t, routeAlive, makeSwitch) {
  const host = h("div", {}, h("div", { class: "empty" }, "加载分钟线…"));
  fill(body, host);
  const alive = () => routeAlive() && host.isConnected;
  const c = C();
  const retry = createRetryGate();
  const KEY = `minute:${t}`;
  let day = null, chart = null, busy = false, nextAt = null;
  const dates = await api(`/api/intraday-dates/${encodeURIComponent(t)}`).catch(() => ({ dates: [] }));
  if (!alive()) return;
  const today = state.meta?.session?.date_et;
  const dayAge = (x) => (today ? Math.round((Date.parse(today) - Date.parse(x)) / 86400000) : 0);

  const beat = setInterval(() => {
    if (!alive()) { clearInterval(beat); release(chart); return; }
    if (nextAt !== null && Date.now() >= nextAt) load(true);
  }, TICK_MS);

  async function load(isRefresh) {
    if (busy) return;
    busy = true; nextAt = null;
    retry.begin(KEY);
    let m;
    try { m = await api(`/api/minute-bars/${encodeURIComponent(t)}${day ? `?date=${day}` : ""}`); }
    catch (e) { m = { available: false, reason: "exception", detail: String(e.message || e), bars: [], level_path: [] }; }
    finally { busy = false; }
    if (!alive()) return;
    const fail = m.available ? m.refresh_failed : m;
    const limited = fail?.reason === "rate_limited";
    const plan = retry.plan(KEY, { limited, retryAfterSec: fail?.retry_after_sec, alive, run: () => load(true) });
    draw(m, isRefresh, plan);
    // 续刷：只在取到了、没在限流、看的是今天且在交易时段时安排下一拍
    if (m.available && !m.refresh_failed && m.is_today && m.session_live && isNum(m.refresh_sec)) nextAt = Date.now() + m.refresh_sec * 1000;
  }

  function draw(m, isRefresh, plan) {
    const zoom = isRefresh && chart ? (() => { const z = chart.getOption()?.dataZoom?.[0]; return z ? { start: z.start, end: z.end } : undefined; })() : undefined;
    release(chart); chart = null;
    const label = (code) => m.reason_labels?.[code] || code || "原因不明";
    const maxAge = m.max_age_days ?? 7;
    const opts = [m.date, ...(dates.dates || [])].filter((x, i, a) => x && a.indexOf(x) === i && dayAge(x) <= maxAge).sort().reverse();
    const pick = opts.length > 1 ? h("select", { class: "sel", "aria-label": "哪一天", onchange: (e) => { day = e.target.value; load(false); } },
      opts.map((x) => h("option", { value: x, selected: x === m.date }, x === today ? `${x}（今天）` : x))) : null;
    const again = h("button", { class: "btn", type: "button", onclick: () => load(false) }, "再试");
    const right = h("div", { class: "head-tools" }, pick, makeSwitch());
    const bars = m.bars || [];
    if (!m.available || !bars.length) {
      const wait = isNum(m.retry_after_sec) ? `，约 ${num(m.retry_after_sec, 0)} 秒后冷却结束` : "";
      fill(host, panel("盘中 1 分钟 K 线", { right }, h("div", { class: "empty" }, "分钟线不可得。"),
        h("p", { class: "callout warn" }, `原因：${label(m.reason)}${m.detail ? `（${m.detail}）` : ""}${wait}。分钟线只从 Yahoo 取，不换别的源。`),
        h("p", { class: "small muted" }, RETRY_NOTE[plan], " ", m.reason === "out_of_range" || m.reason === "index_not_supported" ? null : again)));
      return;
    }
    const last = bars[bars.length - 1];
    const hmarks = [], notes = [];
    const lp = m.level_path || [];
    const paths = [
      { name: "ZG", color: c.ink3, type: "dashed", points: lp.map((p) => ({ t: p.t, value: p.zero_gamma })) },
      { name: "Major+", color: c.pos, type: "dotted", points: lp.map((p) => ({ t: p.t, value: p.major_pos })) },
      { name: "Major−", color: c.neg, type: "dotted", points: lp.map((p) => ({ t: p.t, value: p.major_neg })) },
    ];
    const hasPath = lp.length > 0;
    if (m.is_today && !hasPath) {       // 这只票今天没有盘中快照 ⇒ 只有此刻这一组水平，画成横线
      const v = d.views?.le_45dte || {}; const zg = v.zero_gamma || {}; const mj = v.majors || {};
      if (isNum(zg.nearest)) hmarks.push({ value: zg.nearest, label: `ZG ${num(zg.nearest)}`, color: c.ink3 });
      if (isNum(mj.net_major_pos_strike)) hmarks.push({ value: mj.net_major_pos_strike, label: `Major+ ${num(mj.net_major_pos_strike, 0)}`, color: c.pos, type: "dotted" });
      if (isNum(mj.net_major_neg_strike)) hmarks.push({ value: mj.net_major_neg_strike, label: `Major− ${num(mj.net_major_neg_strike, 0)}`, color: c.neg, type: "dotted" });
      notes.push(h("p", { class: "note" }, "这只票今天没有盘中快照（不在盘中关注列表里），水平只有此刻这一组，画成横线；加进关注列表后才有随时间变的路径。"));
    } else if (!hasPath) {
      notes.push(h("p", { class: "note" }, "这一天没有这只票的盘中快照，所以不叠水平：今天的水平不画在过去的价格上。"));
    } else {
      notes.push(h("p", { class: "note" }, `水平路径来自这一天的 ${lp.length} 张盘中快照，每个点放在快照里行情的最后成交时刻（CBOE 延迟约 15 分钟），所以路径比最新价格短一截；路径不往后延伸。`));
    }
    const badVol = bars.filter((r) => isNum(r.volume_raw)).length;
    if (badVol) notes.push(h("p", { class: "note" }, `${badVol} 根的成交量没画：Yahoo 给的数值比当天一般的一根大几十倍以上（疑似把当日累计量记进了单根，未核实）。价格不受影响。`));
    // 纵轴只贴着价格：不在图内的水平用文字写出来（看不到 ≠ 没有）
    const pr = priceRange(bars);
    const lastOf = (k) => { const pts = lp.filter((p) => isNum(p[k]) && p.t <= last.t); return pts.length ? pts[pts.length - 1][k] : null; };
    const latest = hasPath ? [["Zero Gamma", lastOf("zero_gamma")], ["Major+", lastOf("major_pos")], ["Major−", lastOf("major_neg")]]
      : hmarks.map((x) => [x.label.split(" ")[0], x.value]);
    const off = latest.filter(([, v]) => isNum(v) && pr && (v < pr.lo || v > pr.hi))
      .map(([name, v]) => `${name} ${num(v, 2)}（${v > pr.hi ? "上方" : "下方"}，距最新价 ${signedPct((v / last.close - 1) * 100)}）`);
    if (off.length) notes.unshift(h("p", { class: "note" }, `纵轴只贴着当天价格。不在图内的水平：${off.join("；")}。`));
    if (m.refresh_failed) {
      const f = m.refresh_failed;
      notes.unshift(h("p", { class: "callout warn" }, `刷新失败（${label(f.reason)}），图停在 ${last.t}`
        + `${isNum(f.retry_after_sec) ? `，约 ${num(f.retry_after_sec, 0)} 秒后冷却结束` : ""}。${RETRY_NOTE[plan]} `, again));
    }
    const el = h("div", { class: "chart tall" });
    const status = m.is_today ? (m.refresh_failed ? "刷新已停" : m.session_live ? `盘中，每 ${m.refresh_sec} 秒刷新` : "不在交易时段，不刷新") : "历史日";
    fill(host, panel("盘中 1 分钟 K 线", { right,
      sub: `${m.date} · ${bars.length} 根（${m.source}，${status}）；实体 = 开 → 收，影线 = 低 ~ 高，收高于开为绿、低于开为红；正在走的这一分钟不画；滚轮可缩放` },
    el, ...notes));
    chart = candles(el, { bars, hmarks, paths, day: m.date, zoom, fit: "price", yFmt: (x) => num(x, 2) });
  }
  await load(false);
}
