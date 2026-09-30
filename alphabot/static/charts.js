// 图表：ECharts（本地托管）+ 一套暖色纸面主题。颜色从 CSS 变量读，保证图与页面是同一套 token。
import { isNum, usdShort, num, money } from "./lib.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
export const C = () => ({
  ink: css("--ink"), ink2: css("--ink-2"), ink3: css("--ink-3"), line: css("--line"), lineStrong: css("--line-strong"),
  sunk: css("--sunk"), surface: css("--surface"), pos: css("--pos"), neg: css("--neg"), neutral: css("--neutral"),
  clay: css("--clay"), posSoft: css("--pos-soft"), negSoft: css("--neg-soft"),
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

const live = new Set();
let resizeBound = false;
export function mount(el, option) {
  theme();
  const chart = window.echarts.init(el, "alphabot", { renderer: "canvas" });
  chart.setOption(option);
  live.add(chart);
  if (!resizeBound) {
    resizeBound = true;
    let t = null;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => live.forEach((ch) => ch.resize()), 120); });
  }
  return chart;
}
export function disposeAll() { live.forEach((ch) => ch.dispose()); live.clear(); }

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

// gamma 曲线：总 GEX 对假想现价。0 以上蓝、0 以下橙：在过零处插入 y=0 的点，拆成两条带面积的线
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

export function lines(el, { x, series, xType = "category", yFmt = (v) => num(v), xFmt, bands = [], vmarks = [], hmarks = [], yMin }) {
  const c = C();
  const marks = [
    ...vmarks.map((m) => vline(m.value, m.label, m.color || c.ink, m.type || "solid", m.width || 1.2)),
    ...hmarks.map((m) => hline(m.value, m.label, m.color || c.ink2, m.type || "dashed", m.width || 1)),
  ];
  const area = bands.map((b) => [{ yAxis: b.lo, itemStyle: { color: b.color || c.sunk, opacity: 0.6 }, label: { show: !!b.label, formatter: b.label, color: c.ink3, fontSize: 11, position: "insideTopLeft" } }, { yAxis: b.hi }]);
  return mount(el, {
    grid: { left: 60, right: el.clientWidth < 520 ? 56 : 96, top: series.length > 1 ? 36 : 20, bottom: 40 },
    legend: series.length > 1 ? { top: 0, left: 0 } : undefined,
    tooltip: { trigger: "axis", valueFormatter: (v) => (isNum(v) ? yFmt(v) : "—"), axisPointer: { lineStyle: { color: c.lineStrong } } },
    xAxis: xType === "category" ? { type: "category", data: x, boundaryGap: false }
      : { type: "value", min: "dataMin", max: "dataMax", axisLabel: { formatter: xFmt || ((v) => num(v, 0)) } },
    yAxis: { type: "value", scale: yMin === undefined, min: yMin, axisLabel: { formatter: yFmt } },
    series: series.map((s, i) => ({
      name: s.name, type: "line", data: s.data, showSymbol: s.symbol || false, symbolSize: 7, connectNulls: false,
      step: s.step, lineStyle: { width: s.width || 2, type: s.dash ? "dashed" : "solid", color: s.color },
      itemStyle: { color: s.color }, areaStyle: s.area ? { opacity: 0.1, color: s.color } : undefined,
      markLine: i === 0 && marks.length ? { symbol: "none", silent: true, animation: false, data: marks } : undefined,
      markArea: i === 0 && area.length ? { silent: true, data: area } : undefined,
    })),
  });
}

export function columns(el, { x, values, fmt = usdShort, diverging = true, color }) {
  const c = C();
  return mount(el, {
    grid: { left: 72, right: 20, top: 16, bottom: 56 },
    tooltip: { trigger: "axis", valueFormatter: (v) => fmt(v), axisPointer: { type: "shadow", shadowStyle: { color: c.sunk, opacity: 0.6 } } },
    xAxis: { type: "category", data: x, axisLabel: { rotate: x.length > 8 ? 35 : 0 } },
    yAxis: { type: "value", axisLabel: { formatter: (v) => fmt(v) } },
    series: [{ type: "bar", data: values, barMaxWidth: 28,
      itemStyle: { borderRadius: [3, 3, 0, 0], color: diverging ? (p) => (p.value >= 0 ? c.pos : c.neg) : (color || c.ink2) } }],
  });
}

// 盘中热力图：x 时间、y 行权价、值 = 净 GEX（发散：橙—暖灰—蓝）
export function heatmap(el, { times, strikes, cells }) {
  const c = C();
  const maxAbs = Math.max(1, ...cells.map((d) => Math.abs(d[2])).filter(isNum));
  return mount(el, {
    grid: { left: 64, right: 90, top: 12, bottom: 44 },
    tooltip: { formatter: (p) => `${times[p.value[0]]} · 行权价 <b>${num(strikes[p.value[1]])}</b><br>净 GEX：${usdShort(p.value[2])} / 1%` },
    xAxis: { type: "category", data: times, splitArea: { show: false } },
    yAxis: { type: "category", data: strikes.map((s) => num(s, s % 1 ? 1 : 0)) },
    visualMap: { type: "continuous", min: -maxAbs, max: maxAbs, calculable: false, orient: "vertical", right: 0, top: "middle",
      itemHeight: 160, itemWidth: 10, text: ["正", "负"], textStyle: { color: c.ink3, fontSize: 11 },
      inRange: { color: [c.neg, c.negSoft, c.sunk, c.posSoft, c.pos] }, formatter: (v) => usdShort(v) },
    series: [{ type: "heatmap", data: cells, itemStyle: { borderColor: c.surface, borderWidth: 1 } }],
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
      ps.map((p) => `${p.marker}${p.seriesName}：${money(p.value[1])} / 股`).join("<br>") },
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
