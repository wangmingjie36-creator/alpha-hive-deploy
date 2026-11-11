// 图表：ECharts（本地托管）+ 深色终端主题（v0.45.762）。颜色从 CSS 变量读，保证图与页面是同一套 token。
import { isNum, usdShort, num, money } from "./lib.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
export const C = () => ({
  ink: css("--ink"), ink2: css("--ink-2"), ink3: css("--ink-3"), line: css("--line"), lineStrong: css("--line-strong"),
  sunk: css("--sunk"), surface: css("--surface"), pos: css("--pos"), neg: css("--neg"), neutral: css("--neutral"),
  clay: css("--clay"), paper: css("--paper"), posSoft: css("--pos-soft"), negSoft: css("--neg-soft"),
});
const FONT = '-apple-system, BlinkMacSystemFont, "PingFang SC", "Noto Sans SC", "Helvetica Neue", Arial, sans-serif';

let registered = false;
function theme() {
  if (registered) return;
  const c = C();
  window.echarts.registerTheme("alphabot", {
    backgroundColor: "transparent",
    textStyle: { fontFamily: FONT, color: c.ink2 },
    color: [c.pos, c.neg, c.ink2, c.neutral],
    grid: { left: 64, right: 24, top: 28, bottom: 36, containLabel: false },
    categoryAxis: { axisLine: { lineStyle: { color: c.lineStrong } }, axisTick: { show: false },
      axisLabel: { color: c.ink3, fontSize: 11 }, splitLine: { show: false } },
    valueAxis: { axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: c.ink3, fontSize: 11 },
      splitLine: { lineStyle: { color: c.line } } },
    tooltip: { backgroundColor: c.surface, borderColor: c.lineStrong, borderWidth: 1, padding: [8, 11],
      textStyle: { color: c.ink, fontSize: 12.5, fontFamily: FONT }, extraCssText: "box-shadow:none;border-radius:8px;" },
    legend: { textStyle: { color: c.ink2, fontSize: 12 }, icon: "roundRect", itemWidth: 12, itemHeight: 8 },
  });
  registered = true;
}

// 悬停提示（v0.45.771）。本站 CSP 不许行内样式属性，而 ECharts 自带的提示排版（缺省 formatter / `valueFormatter` / `p.marker`）
// 整段都是行内样式——被拦掉后色点没了、名字和数值挤在一起，每悬停一次控制台报一串错。所以：
//   · 提示一律自己排版，只用样式类（`tip-dot` / `tip-name`）；
//   · 色点的颜色放在 `data-c` 里，由 `mount` 里的观察器经 CSSOM 涂上（脚本设 style 属性不受 CSP 的行内样式限制）。
const dot = (color) => `<i class="tip-dot" data-c="${typeof color === "string" ? color.replace(/[^#\w(),.% -]/g, "") : ""}"></i>`;
const tipValue = (p) => (Array.isArray(p.value) ? p.value[p.value.length - 1] : p.value);
// 轴触发的提示：第一行是横轴上的位置，其后每个系列一行（色点 + 名字 + 数值）
const axisTip = (fmt) => (ps) => `${ps[0].axisValueLabel ?? ps[0].name ?? ""}`
  + ps.map((p) => `<br>${dot(p.color)}${p.seriesName && !/^series\d+$/.test(p.seriesName) ? `<span class="tip-name">${p.seriesName}</span>` : ""}<b>${fmt(tipValue(p))}</b>`).join("");
function paintDots(el) {
  const paint = () => el.querySelectorAll("i.tip-dot[data-c]").forEach((n) => { if (n.dataset.c) n.style.backgroundColor = n.dataset.c; });
  const obs = new MutationObserver(paint);
  obs.observe(el, { childList: true, subtree: true });
  return obs;
}

const live = new Set();
let resizeBound = false;
export function mount(el, option) {
  theme();
  const chart = window.echarts.init(el, "alphabot", { renderer: "canvas" });
  chart.setOption(option);
  declutterHLabels(chart);
  chart.__tipObs = paintDots(el);     // 图释放时一起断开（见 disposeAll / release）
  live.add(chart);
  if (!resizeBound) {
    resizeBound = true;
    let t = null;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => live.forEach((ch) => { ch.resize(); declutterHLabels(ch); }), 120); });
  }
  return chart;
}

// 横线标签避让：现价与 Major+ 之类离得近的水平线，标签缺省都贴在同一侧的线上方，会叠成一团。
// 渲染后按像素位置从上到下逐个放：先试自己的位置，再试同侧翻到线下方，再试另一侧（左 / 右）；
// 都放不下才留原位。只动 inside* 位置的横线标签（position: "end" 之类在图外的不碰）。
// 原位置记在图实例上，缩放后按原位置重排（否则一次避让后的位置会被当成「原位置」）。
const LABEL_H = 15;
const SLOTS = ["insideEndTop", "insideEndBottom", "insideStartTop", "insideStartBottom"];
function slotOrder(pref) {
  const side = pref.includes("Start") ? "Start" : "End", other = side === "Start" ? "End" : "Start";
  const v = pref.endsWith("Bottom") ? "Bottom" : "Top", flip = v === "Top" ? "Bottom" : "Top";
  return [`inside${side}${v}`, `inside${side}${flip}`, `inside${other}${v}`, `inside${other}${flip}`];
}
export function declutterHLabels(chart) {
  let opt;
  try { opt = chart.getOption(); } catch { return; }
  const series = opt.series || [];
  if (!chart.__hlPref) {
    chart.__hlPref = series.map((s) => (s.markLine?.data || []).map((d) => {
      const p = d && !Array.isArray(d) && isNum(d.yAxis) && d.label?.show !== false ? (d.label?.position || "insideEndTop") : null;
      return p && SLOTS.includes(p) ? p : null;
    }));
  }
  const items = [];
  series.forEach((s, si) => (s.markLine?.data || []).forEach((d, di) => {
    const pref = chart.__hlPref[si]?.[di];
    if (!pref) return;
    let y;
    try { y = chart.convertToPixel({ yAxisIndex: s.yAxisIndex || 0 }, d.yAxis); } catch { return; }
    if (isNum(y)) items.push({ si, di, y, pref });
  }));
  if (items.length < 2) return;
  items.sort((a, b) => a.y - b.y);
  const placed = [];
  const box = (y, pos) => ({ side: pos.includes("Start") ? "S" : "E",
    y0: pos.endsWith("Top") ? y - LABEL_H - 1 : y + 1, y1: pos.endsWith("Top") ? y - 1 : y + LABEL_H + 1 });
  const clash = (b) => placed.some((q) => q.side === b.side && b.y0 < q.y1 && q.y0 < b.y1);
  let changed = false;
  for (const it of items) {
    const pos = slotOrder(it.pref).find((p) => !clash(box(it.y, p))) || it.pref;
    placed.push(box(it.y, pos));
    const lab = series[it.si].markLine.data[it.di].label || {};
    if (lab.position !== pos) { series[it.si].markLine.data[it.di].label = { ...lab, position: pos }; changed = true; }
  }
  if (changed) chart.setOption({ series: series.map((s) => (s.markLine ? { markLine: { data: s.markLine.data } } : {})) });
}
export function disposeAll() { live.forEach((ch) => { ch.__tipObs?.disconnect(); ch.dispose(); }); live.clear(); }
// 单张图换掉时释放（同页重建子视图时用）：不释放的话旧实例留在 live 里，每次窗口缩放都要白白 resize 一个已脱离页面的图
export function release(ch) { if (ch) { live.delete(ch); ch.__tipObs?.disconnect(); try { ch.dispose(); } catch { /* 已释放 */ } } }

function hline(value, label, color, type = "dashed", width = 1, position = "insideEndTop") {
  return { yAxis: value, lineStyle: { color, type, width },
    label: { formatter: label, position, color, fontSize: 11, fontWeight: 500 } };
}
function vline(value, label, color, type = "dashed", width = 1) {
  return { xAxis: value, lineStyle: { color, type, width },
    label: { formatter: label, position: "insideEndTop", color, fontSize: 11 } };
}

function stepOf(values) {
  const s = [...new Set(values.filter(isNum))].sort((a, b) => a - b);
  let best = Infinity;
  for (let i = 1; i < s.length; i++) best = Math.min(best, s[i] - s[i - 1]);
  return Number.isFinite(best) ? best : 1;
}

// 按行权价的横向条形（GEX 梯子 / DEX / vanna / charm / OI）。y 为数值轴（现价、ZG 能画在两个行权价之间），
// 条形用 custom series 自己画——ECharts 的 bar 在两根数值轴上没有基准轴。
export function strikeBars(el, { rows, series, spot, levels = [], fmt = usdShort, unit = "" }) {
  const c = C();
  const strikes = rows.map((r) => r.strike);
  const step = stepOf(strikes);
  const lo0 = Math.min(...strikes, spot ?? Infinity) - step, hi0 = Math.max(...strikes, spot ?? -Infinity) + step;
  const iv = niceInterval(hi0 - lo0);
  const lo = Math.floor(lo0 / iv) * iv, hi = Math.ceil(hi0 / iv) * iv;
  const marks = [];
  if (isNum(spot)) marks.push(hline(spot, `现价 ${num(spot)}`, c.ink, "solid", 1.4));
  for (const l of levels) if (isNum(l.value)) marks.push(hline(l.value, l.label, l.color || c.ink2, l.type || "dashed", 1, l.pos));
  // 同号的系列（如 call 正 / put 负）共用一条；不同号的并排
  const lanes = series.some((s) => s.lane !== undefined) ? Math.max(...series.map((s) => (s.lane ?? 0) + 1)) : 1;
  const out = series.map((s, i) => ({
    name: s.name, type: "custom",
    data: rows.map((r) => [s.value(r), r.strike]).filter((d) => isNum(d[0])),
    encode: { x: 0, y: 1, tooltip: [0, 1] },
    itemStyle: { color: s.color || c.pos },
    renderItem: (params, api) => {
      const v = api.value(0), k = api.value(1);
      const p0 = api.coord([0, k]), p1 = api.coord([v, k]);
      const band = Math.abs(api.size([0, step])[1]);
      const hgt = Math.max(1.5, Math.min(18, (band * 0.74) / lanes));
      const lane = s.lane ?? 0;
      const yc = p0[1] - (band * 0.74) / 2 + hgt * (lane + 0.5);
      const fill = s.color || (v >= 0 ? c.pos : c.neg);
      return { type: "rect", shape: { x: Math.min(p0[0], p1[0]), y: yc - hgt / 2, width: Math.max(1, Math.abs(p1[0] - p0[0])), height: hgt, r: 2 },
        style: { fill }, emphasis: { style: { opacity: 0.8 } } };
    },
    markLine: i === 0 ? { symbol: "none", silent: true, animation: false, data: marks } : undefined,
  }));
  return mount(el, {
    grid: { left: 56, right: el.clientWidth < 520 ? 64 : 110, top: series.length > 1 ? 30 : 16, bottom: 40 },
    legend: series.length > 1 ? { top: 0, right: 0, data: series.map((s) => ({ name: s.name, itemStyle: { color: s.color || c.pos } })) } : undefined,
    tooltip: {
      trigger: "item",
      formatter: (p) => `行权价 <b>${num(p.value[1])}</b><br>${p.seriesName}：${fmt(p.value[0])}${unit}`,
    },
    xAxis: { type: "value", axisLabel: { formatter: (v) => fmt(v) }, splitLine: { lineStyle: { color: c.line } } },
    yAxis: { type: "value", min: lo, max: hi, interval: iv, axisLabel: { formatter: (v) => num(v, step < 1 ? 1 : 0) } },
    series: out,
  });
}
function niceInterval(range) {
  const raw = range / 10, p = 10 ** Math.floor(Math.log10(raw)), m = raw / p;
  return (m < 1.5 ? 1 : m < 3 ? 2 : m < 7 ? 5 : 10) * p;
}

// gamma 曲线：总 GEX 对假想现价。0 以上绿、0 以下红：在过零处插入 y=0 的点，拆成两条带面积的线
// （不用 visualMap：它在两根数值轴的折线上会抛错，ECharts 5.5.1 实测）。
export function gammaCurve(el, curve, spot) {
  const c = C();
  const pts = (curve.grid || []).map((x, i) => [x, curve.total?.[i]]).filter((d) => isNum(d[1]));
  const dense = [];
  pts.forEach((p, i) => {
    if (i > 0) {
      const q = pts[i - 1];
      if ((q[1] > 0 && p[1] < 0) || (q[1] < 0 && p[1] > 0)) dense.push([q[0] - q[1] * (p[0] - q[0]) / (p[1] - q[1]), 0]);
    }
    dense.push(p);
  });
  const part = (keep) => dense.map((d) => (keep(d[1]) || d[1] === 0 ? d : [d[0], null]));
  const marks = [];
  if (isNum(spot)) marks.push(vline(spot, `现价 ${num(spot)}`, c.ink, "solid", 1.4));
  (curve.crossings || []).forEach((x) => marks.push(vline(x, `零点 ${num(x)}`, c.ink3, "dashed")));
  const line = (name, data, color, withMarks) => ({
    name, type: "line", data, showSymbol: false, connectNulls: false, lineStyle: { width: 2, color }, itemStyle: { color },
    areaStyle: { color, opacity: 0.12 },
    markLine: withMarks ? { symbol: "none", silent: true, animation: false,
      data: [...marks, { yAxis: 0, lineStyle: { color: c.lineStrong, type: "solid", width: 1 }, label: { show: false } }] } : undefined,
  });
  return mount(el, {
    grid: { left: 72, right: 28, top: 26, bottom: 40 },
    tooltip: { trigger: "axis", formatter: (ps) => { const p = ps.find((x) => isNum(x.value[1])) || ps[0];
      return `假想现价 <b>${num(p.value[0])}</b><br>总 GEX：${usdShort(p.value[1])} / 1%`; },
    axisPointer: { type: "line", lineStyle: { color: c.lineStrong } } },
    xAxis: { type: "value", min: "dataMin", max: "dataMax", axisLabel: { formatter: (v) => num(v, 0) } },
    yAxis: { type: "value", axisLabel: { formatter: (v) => usdShort(v) } },
    series: [line("正 gamma", part((y) => y > 0), c.pos, true), line("负 gamma", part((y) => y < 0), c.neg, false)],
  });
}

export function lines(el, { x, series, xType = "category", yFmt = (v) => num(v), xFmt, bands = [], vmarks = [], hmarks = [], yMin, yMax }) {
  const c = C();
  const marks = [
    ...vmarks.map((m) => vline(m.value, m.label, m.color || c.ink, m.type || "solid", m.width || 1.2)),
    ...hmarks.map((m) => hline(m.value, m.label, m.color || c.ink2, m.type || "dashed", m.width || 1)),
  ];
  const area = bands.map((b) => [{ yAxis: b.lo, itemStyle: { color: b.color || c.sunk, opacity: 0.6 }, label: { show: !!b.label, formatter: b.label, color: c.ink3, fontSize: 11, position: "insideTopLeft" } }, { yAxis: b.hi }]);
  return mount(el, {
    grid: { left: 60, right: el.clientWidth < 520 ? 56 : 96, top: series.length > 1 ? 36 : 20, bottom: 40 },
    legend: series.length > 1 ? { top: 0, left: 0 } : undefined,
    tooltip: { trigger: "axis", formatter: axisTip((v) => (isNum(v) ? yFmt(v) : "—")), axisPointer: { lineStyle: { color: c.lineStrong } } },
    xAxis: xType === "category" ? { type: "category", data: x, boundaryGap: false }
      : { type: "value", min: "dataMin", max: "dataMax", axisLabel: { formatter: xFmt || ((v) => num(v, 0)) } },
    yAxis: { type: "value", scale: yMin === undefined && yMax === undefined, min: yMin, max: yMax, axisLabel: { formatter: yFmt } },
    series: series.map((s, i) => ({
      name: s.name, type: "line", data: s.data, showSymbol: s.symbol || false, symbolSize: 7, connectNulls: false,
      step: s.step, lineStyle: { width: s.width || 2, type: s.dash ? "dashed" : "solid", color: s.color },
      itemStyle: { color: s.color }, areaStyle: s.area ? { opacity: 0.1, color: s.color } : undefined,
      markLine: i === 0 && marks.length ? { symbol: "none", silent: true, animation: false, data: marks } : undefined,
      markArea: i === 0 && area.length ? { silent: true, data: area } : undefined,
    })),
  });
}

// 散点（跨式校准图）：两根数值轴、一个系列；参考线（阈值 / y=1）。身份不靠颜色：实心 = 开过仓、空心 = 只有信号，
// 点旁直接标代码。坐标范围把参考线也包进来，免得阈值线落在画布外被裁掉。
export function scatter(el, { points, xName, yName, vlines = [], hlines = [], xFmt = (v) => num(v, 2), yFmt = (v) => num(v, 2), tip }) {
  const c = C();
  const span = (vals) => {
    const lo = Math.min(...vals), hi = Math.max(...vals), pad = Math.max((hi - lo) * 0.12, 0.05);
    return [Math.floor((lo - pad) * 10) / 10, Math.ceil((hi + pad) * 10) / 10];
  };
  const [x0, x1] = span([...points.map((p) => p.x), ...vlines.map((m) => m.value)].filter(isNum));
  const [y0, y1] = span([...points.map((p) => p.y), ...hlines.map((m) => m.value)].filter(isNum));
  // 竖线标签横排放在线顶上方（ECharts 缺省的 insideEndTop 在竖线上是旋转的，会压住点旁的代码）
  const marks = [...vlines.map((m) => ({ xAxis: m.value, lineStyle: { color: m.color || c.ink3, type: "dashed", width: 1 },
    label: { formatter: m.label, position: "end", color: m.color || c.ink3, fontSize: 11 } })),
  ...hlines.map((m) => hline(m.value, m.label, m.color || c.ink3, "dashed"))];
  return mount(el, {
    grid: { left: 64, right: 32, top: 26, bottom: 52 },
    tooltip: { trigger: "item", formatter: (p) => (tip ? tip(p.data.raw) : String(p.data.raw.label || "")) },
    xAxis: { type: "value", name: xName, nameLocation: "middle", nameGap: 30, min: x0, max: x1, axisLabel: { formatter: xFmt } },
    yAxis: { type: "value", name: yName, nameLocation: "middle", nameGap: 46, min: y0, max: y1, axisLabel: { formatter: yFmt } },
    series: [{
      type: "scatter", symbolSize: 11,
      data: points.map((p) => ({
        value: [p.x, p.y], raw: p,
        itemStyle: p.filled ? { color: c.ink2, borderColor: c.surface, borderWidth: 2 } : { color: c.surface, borderColor: c.ink2, borderWidth: 2 },
        label: { show: true, formatter: p.label, position: "right", color: c.ink3, fontSize: 11 },
      })),
      markLine: marks.length ? { symbol: "none", silent: true, animation: false, data: marks } : undefined,
    }],
  });
}

// 日线涨跌柱（标的页「价格」）：每根的实体 = 前一日收盘 → 当日收盘，涨绿跌红；下方成交量按同一涨跌上色。
// ⚠️ 这不是完整 K 线：共享日线入口只有收盘与成交量，没有开 / 高 / 低 ⇒ 不画影线、不拿别的数冒充开盘价。
// 第一根没有「前一日」⇒ 画成一条平线（开 = 收）、成交量用中性色。水平线 / ±1σ 带与旧折线图同一套参数。
// 行可以是日线（`date`）也可以是分钟线（`t` = "HH:MM"，`day` 给提示用）。`paths`：随时间变的水平（盘中快照里的 Zero Gamma 等），
// 每条 `{name, color, type, points: [{t, value}]}`——点落在「时刻不晚于它的最后一根线」上，阶梯连接，**不往最后一个点之后延伸**
// （之后的水平还不知道）。`zoom`：`{start, end}`，定时重画时把缩放状态带过来。
// `fit`："all"（缺省）= 纵轴把水平线 / 带 / 路径都包进来；"price" = 纵轴只贴着价格（盘中一天的波动远小于水平之间的距离，
// 都包进来 K 线会被压成一条线）——图外的水平由调用方用文字写出来（`priceRange` 给出判据）。
export function priceRange(bars) {
  const v = bars.filter((r) => isNum(r.close)).flatMap((r) => (isNum(r.high) && isNum(r.low) ? [r.low, r.high] : [r.close]));
  if (!v.length) return null;
  const lo = Math.min(...v), hi = Math.max(...v), pad = Math.max((hi - lo) * 0.12, hi * 0.0008);
  return { lo: lo - pad, hi: hi + pad };
}
export function candles(el, { bars, hmarks = [], bands = [], paths = [], day = "", zoom, fit = "all", yFmt = (v) => num(v) }) {
  const c = C();
  const rows = bars.filter((r) => isNum(r.close));
  const x = rows.map((r) => r.t || r.date.slice(5));
  const pr = fit === "price" ? priceRange(rows) : null;
  if (pr) hmarks = hmarks.filter((m) => m.value >= pr.lo && m.value <= pr.hi);     // 图外的横线不画（否则标签会贴在边上冒充在图内）
  const volFmt = (v, dp = 2) => (Math.abs(v) >= 1e6 ? `${num(v / 1e6, dp)}M` : `${num(v / 1e3, dp ? 1 : 0)}K`);
  const pathSeries = paths.map((p) => {
    const data = new Array(rows.length).fill(null);
    let n = 0;
    for (const pt of p.points || []) {
      if (!isNum(pt.value) || !rows.length || pt.t < rows[0].t || pt.t > rows[rows.length - 1].t) continue;
      let i = rows.length - 1;
      while (i > 0 && rows[i].t > pt.t) i -= 1;
      data[i] = pt.value; n += 1;
    }
    return n ? { name: p.name, type: "line", step: "end", connectNulls: true, data, z: 3, symbol: "circle", symbolSize: 3,
      showSymbol: n === 1, lineStyle: { color: p.color || c.ink3, type: p.type || "dashed", width: p.width || 1.3 },
      itemStyle: { color: p.color || c.ink3 }, endLabel: { show: true, formatter: p.name, color: p.color || c.ink3, fontSize: 11 } } : null;
  }).filter(Boolean);
  // 这根有开高低 ⇒ 真 K 线（实体 = 开 → 收，影线 = 低 ~ 高，颜色按收对开）；
  // 没有 ⇒ 退回「前收 → 收盘」的无影线柱（颜色按收对前收）。逐根判，两种可以混在一张图里
  const full = (r) => isNum(r.open) && isNum(r.high) && isNum(r.low);
  const openOf = (i) => (full(rows[i]) ? rows[i].open : i > 0 ? rows[i - 1].close : rows[i].close);
  const dir = (i) => Math.sign(rows[i].close - openOf(i));
  const colorOf = (d) => (d > 0 ? c.pos : d < 0 ? c.neg : c.neutral);
  // ECharts 蜡烛数据顺序：[开, 收, 低, 高]
  const k = rows.map((r, i) => { const o = openOf(i), cl = r.close;
    return full(r) ? [o, cl, r.low, r.high] : [o, cl, Math.min(o, cl), Math.max(o, cl)]; });
  const hasVol = rows.some((r) => isNum(r.volume) && r.volume > 0);
  const marks = hmarks.map((m) => hline(m.value, m.label, m.color || c.ink2, m.type || "dashed", m.width || 1));
  const last = rows[rows.length - 1];
  if (last) {
    const lc = colorOf(dir(rows.length - 1));
    marks.push({ yAxis: last.close, lineStyle: { color: lc, type: "solid", width: 1, opacity: 0.7 },
      label: { formatter: yFmt(last.close), position: "end", color: c.paper, backgroundColor: lc,
        padding: [3, 5], borderRadius: 3, fontSize: 11, fontWeight: 600 } });
  }
  const area = bands.map((b) => [{ yAxis: b.lo, itemStyle: { color: b.color || c.sunk, opacity: 0.55 },
    label: { show: !!b.label, formatter: b.label, color: c.ink3, fontSize: 11, position: "insideTopLeft" } }, { yAxis: b.hi }]);
  // 水平线与 ±1σ 带可能在价格区间之外：把它们包进纵轴范围，否则被裁掉（看不到 ≠ 没有）
  const ys = [...rows.flatMap((r) => (full(r) ? [r.low, r.high] : [r.close])), ...pathSeries.flatMap((s) => s.data), ...hmarks.map((m) => m.value), ...bands.flatMap((b) => [b.lo, b.hi])].filter(isNum);
  const y0 = pr ? pr.lo : Math.min(...ys), y1 = pr ? pr.hi : Math.max(...ys), pad = pr ? 0 : (y1 - y0) * 0.04 || 1;
  const right = el.clientWidth < 520 ? 60 : 104;
  const tip = (ps) => {
    const i = ps[0].dataIndex, r = rows[i];
    // 涨跌幅一律对前收（和柱子颜色不是一回事：真 K 线的颜色是收对开，跳空高开后收低就是「涨了但柱子是红的」）
    const chg = i > 0 ? (r.close / rows[i - 1].close - 1) * 100 : null;
    const d = chg === null ? 0 : Math.sign(chg);
    const sign = d > 0 ? "+" : d < 0 ? "−" : "";
    return `${r.t ? `${day} ${r.t}` : r.date}<br>收盘 <b>${yFmt(r.close)}</b>`
      + (chg === null ? "" : ` <span class="${d > 0 ? "up" : d < 0 ? "down" : ""}">${sign}${num(Math.abs(chg), 2)}%</span>`)
      + (full(r) ? `<br>开 ${yFmt(r.open)}　高 ${yFmt(r.high)}　低 ${yFmt(r.low)}` : "<br>这根没有开高低")
      + (chg === null ? "" : `<br>${r.t ? "上一根收" : "前收"} ${yFmt(rows[i - 1].close)}`)
      + (isNum(r.volume) ? `<br>成交量 ${volFmt(r.volume)}` : "")
      + pathSeries.map((s) => { let j = i; while (j >= 0 && s.data[j] === null) j -= 1;
        return j >= 0 ? `<br>${s.name} ${yFmt(s.data[j])}` : ""; }).join("");
  };
  return mount(el, {
    animation: false,
    grid: hasVol ? [{ left: 60, right, top: 20, bottom: "27%" }, { left: 60, right, top: "78%", bottom: 40 }]
      : [{ left: 60, right, top: 20, bottom: 40 }],
    tooltip: { trigger: "axis", formatter: tip, axisPointer: { type: "cross",
      lineStyle: { color: c.lineStrong }, crossStyle: { color: c.lineStrong },
      label: { backgroundColor: c.sunk, color: c.ink, borderColor: c.lineStrong, borderWidth: 1 } } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    dataZoom: [{ type: "inside", xAxisIndex: hasVol ? [0, 1] : [0], start: zoom?.start ?? 0, end: zoom?.end ?? 100 }],
    xAxis: [{ type: "category", data: x, boundaryGap: true, gridIndex: 0, axisLabel: { show: !hasVol } },
      ...(hasVol ? [{ type: "category", data: x, boundaryGap: true, gridIndex: 1 }] : [])],
    yAxis: [{ type: "value", min: y0 - pad, max: y1 + pad, gridIndex: 0, axisLabel: { formatter: yFmt, showMinLabel: false, showMaxLabel: false } },
      ...(hasVol ? [{ type: "value", gridIndex: 1, splitNumber: 2, splitLine: { show: false },
        axisLabel: { formatter: (v) => (v === 0 ? "" : volFmt(v, 0)) } }] : [])],
    series: [
      { name: "日线", type: "candlestick", data: k, barMaxWidth: 14,
        itemStyle: { color: c.pos, color0: c.neg, borderColor: c.pos, borderColor0: c.neg, borderColorDoji: c.neutral },
        markLine: marks.length ? { symbol: "none", silent: true, animation: false, data: marks } : undefined,
        markArea: area.length ? { silent: true, data: area } : undefined },
      ...pathSeries,
      ...(hasVol ? [{ name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 14,
        data: rows.map((r, i) => ({ value: isNum(r.volume) ? r.volume : null, itemStyle: { color: colorOf(dir(i)), opacity: 0.55 } })) }] : []),
    ],
  });
}

export function columns(el, { x, values, fmt = usdShort, diverging = true, color }) {
  const c = C();
  return mount(el, {
    grid: { left: 72, right: 20, top: 16, bottom: 56 },
    tooltip: { trigger: "axis", formatter: axisTip((v) => fmt(v)), axisPointer: { type: "shadow", shadowStyle: { color: c.sunk, opacity: 0.6 } } },
    xAxis: { type: "category", data: x, axisLabel: { rotate: x.length > 8 ? 35 : 0 } },
    yAxis: { type: "value", axisLabel: { formatter: (v) => fmt(v) } },
    series: [{ type: "bar", data: values, barMaxWidth: 28,
      itemStyle: { borderRadius: [3, 3, 0, 0], color: diverging ? (p) => (p.value >= 0 ? c.pos : c.neg) : (color || c.ink2) } }],
  });
}

// 盘中热力图：x 时间、y 行权价、值 = 净 GEX（发散：红—深灰—绿）
// 盘中热力图（行权价 × 拍）叠现价 / Zero Gamma 路径。x 为类目轴（按拍、不等距），y 为数值轴——
// 价格线才能画在两个行权价之间；格子用 custom series 自己画（ECharts 的 heatmap 只认两根类目轴）。
// 数据由服务端 intraday_heat.build 排好：cells 每行 [拍, 行权价（分桶时 = 桶中心）, 真值, 上色用的截断值, 桶里几个行权价]。
export function gammaTrace(el, hm) {
  const c = C();
  const { times, strikes, cells, price, zero_gamma: zg, clip } = hm;
  const st = hm.step || 1;
  const lo = Math.min(...strikes) - st / 2, hi = Math.max(...strikes) + st / 2;
  const lineOf = (arr) => arr.map((v, i) => [i, isNum(v) ? v : null]);
  // 成交活跃度不分正负（signed === false）：单色由浅到深，0 起；净 GEX 仍是发散色
  const unsigned = hm.signed === false;
  const valFmt = hm.basis === "activity_volume" ? (v) => `${num(v, 0)} 张` : (v) => `${usdShort(v)} / 1%`;
  const cellLabel = unsigned ? (hm.basis === "activity_volume" ? "新增成交" : "新增成交 × gamma")
    : (hm.mode === "delta" ? "较上一拍" : "净 GEX");
  return mount(el, {
    grid: { left: 56, right: 84, top: 30, bottom: 44 },
    legend: { top: 0, left: 0, data: ["现价", "Zero Gamma"] },
    tooltip: { trigger: "item", formatter: (p) => p.seriesType === "custom"
      ? `${times[p.value[0]]} · 行权价 <b>${hm.bucket ? `${num(p.value[1] - hm.bucket / 2)}–${num(p.value[1] + hm.bucket / 2)}` : num(p.value[1])}</b>`
        + `${hm.bucket ? `（${p.value[4]} 个行权价合计）` : ""}<br>${cellLabel}：${valFmt(p.value[2])}`
      : `${times[p.value[0]]} · ${p.seriesName} <b>${num(p.value[1])}</b>` },
    xAxis: { type: "category", data: times, boundaryGap: true, splitArea: { show: false } },
    yAxis: { type: "value", min: lo, max: hi, splitLine: { show: false },
      axisLabel: { showMinLabel: false, showMaxLabel: false, formatter: (v) => num(v, st < 1 ? 1 : 0) } },
    visualMap: { type: "continuous", dimension: 3, seriesIndex: 0, min: unsigned ? 0 : -clip, max: clip, calculable: false,
      orient: "vertical", right: 0, top: "middle", itemHeight: 160, itemWidth: 10, text: unsigned ? ["多", "少"] : ["正", "负"],
      textStyle: { color: c.ink3, fontSize: 11 }, formatter: (v) => (hm.basis === "activity_volume" ? num(v, 0) : usdShort(v)),
      inRange: { color: unsigned ? [c.sunk, c.posSoft, c.pos] : [c.neg, c.negSoft, c.sunk, c.posSoft, c.pos] } },
    series: [
      { name: "GEX", type: "custom", clip: true, data: cells, encode: { x: 0, y: 1, tooltip: [0, 1, 2, 4] },
        renderItem: (params, api) => {
          const p = api.coord([api.value(0), api.value(1)]), sz = api.size([1, st]);
          return { type: "rect", shape: { x: p[0] - sz[0] / 2, y: p[1] - sz[1] / 2, width: sz[0], height: sz[1] },
            style: { fill: api.visual("color"), stroke: c.surface, lineWidth: 0.5 } };
        } },
      // 只有一拍时一条线画不出来（只有一个点）⇒ 那时显示点
      { name: "现价", type: "line", data: lineOf(price), showSymbol: times.length === 1, symbolSize: 8, connectNulls: false, z: 4,
        lineStyle: { width: 2.2, color: c.ink, shadowColor: c.surface, shadowBlur: 4 }, itemStyle: { color: c.ink } },
      { name: "Zero Gamma", type: "line", data: lineOf(zg), showSymbol: times.length === 1, symbol: "emptyCircle", symbolSize: 8, connectNulls: false, z: 3,
        lineStyle: { width: 1.6, color: c.ink, type: "dashed", shadowColor: c.surface, shadowBlur: 3 }, itemStyle: { color: c.ink } },
    ],
  });
}

export function payoff(el, { grid, series, spot, band }) {
  const c = C();
  const vmarks = [];
  if (isNum(spot)) vmarks.push({ value: spot, label: `现价 ${num(spot)}`, color: c.ink });
  const marks = vmarks.map((m) => vline(m.value, m.label, m.color, "solid", 1.2));
  if (band && isNum(band.lo) && isNum(band.hi)) {
    marks.push(vline(band.lo, "−1σ", c.ink3, "dotted"), vline(band.hi, "+1σ", c.ink3, "dotted"));
  }
  return mount(el, {
    grid: { left: 64, right: 28, top: 34, bottom: 40 },
    legend: { top: 0, left: 0 },
    tooltip: { trigger: "axis", formatter: (ps) => `到期价 <b>${num(ps[0].value[0])}</b><br>` +
      ps.map((p) => `${dot(p.color)}${p.seriesName}：${money(p.value[1])} / 股`).join("<br>") },
    xAxis: { type: "value", min: "dataMin", max: "dataMax", axisLabel: { formatter: (v) => num(v, 0) } },
    yAxis: { type: "value", axisLabel: { formatter: (v) => money(v, 0) } },
    series: series.map((s, i) => ({
      name: s.name, type: "line", showSymbol: false, data: grid.map((x, j) => [x, s.pnl[j]]),
      lineStyle: { width: 2, color: s.color, type: s.dash ? "dashed" : "solid" }, itemStyle: { color: s.color },
      markLine: i === 0 ? { symbol: "none", silent: true, animation: false,
        data: [...marks, { yAxis: 0, lineStyle: { color: c.lineStrong, type: "solid" }, label: { show: false } }] } : undefined,
    })),
  });
}
