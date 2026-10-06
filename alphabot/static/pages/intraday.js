// 盘中：关注列表定时快照 → 水平走势、GEX 热力图、回看、路由变动。只看单日（跨日拼接 = 历史路由叠其后价格）。
import { api, h, helpLink, num, usdShort, seg, table, panel, isNum, pref, setPref, ROUTE_SHORT, fill } from "../lib.js";
import { lines, heatmap, strikeBars, C } from "../charts.js";
import { state, refreshMeta } from "../app.js";
import { sidebar } from "./ticker.js";

export async function renderIntraday(root, t, alive) {
  const [dates, meta] = await Promise.all([api(`/api/intraday-dates/${encodeURIComponent(t)}`), refreshMeta()]);
  if (!alive()) return;
  let date = (dates.dates || [])[0] || (meta.session?.date_et);
  let view = pref("idView", "le_45dte");
  let basis = "oi";
  let back = 3;
  const main = h("div", {});
  fill(root, h("div", { class: "with-side" }, sidebar(t, "#/intraday/{T}"), main));

  async function draw() {
    const d = await api(`/api/intraday/${encodeURIComponent(t)}${date ? `?date=${date}` : ""}`);
    if (!alive()) return;
    const snaps = d.snapshots || [];
    const head = h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, `${t} 盘中`, h("span", { class: "sub" }, `${d.date} · ${snaps.length} 张快照`)),
        h("p", { class: "lede" }, "关注列表里的标的在交易时段内定时拍照（只存聚合数值，不存原始链）。OI 在盘中不变，水平的移动来自现价与 IV 的变化；成交量口径随成交累积。 ", helpLink("intraday"))),
      h("div", { class: "controls" },
        (dates.dates || []).length > 1 ? h("select", { class: "sel", "aria-label": "日期", onchange: (e) => { date = e.target.value; draw(); } },
          dates.dates.map((x) => h("option", { value: x, selected: x === date }, x))) : null,
        seg([["le_45dte", "≤45 天"], ["next_expiry", "下一到期"]], view, (x) => { view = x; setPref("idView", x); draw(); }, "期限视图")));

    const settingsPanel = settingsBlock(t, () => renderIntraday(root, t, alive));
    if (!snaps.length) {
      fill(main, head, panel(null, {}, h("div", { class: "empty" },
        state.meta?.settings?.focus?.includes(t) ? "今天还没有快照：只在美股常规交易时段按间隔拍照。" : "这只标的不在盘中关注列表里，没有快照。")),
      h("div", { style: { height: "20px" } }), settingsPanel);
      return;
    }
    const c = C();
    const times = snaps.map((s) => (s.ts || "").slice(11, 16));
    const V = (s) => s.views?.[view] || {};
    const lvEl = h("div", { class: "chart" });
    const hmEl = h("div", { class: "chart tall" });
    const totEl = h("div", { class: "chart short" });
    const lbEl = h("div", { class: "chart tall" });

    // 热力图数据
    const idx = basis === "oi" ? 1 : 2;
    const strikeSet = new Set();
    snaps.forEach((s) => (V(s).strikes || []).forEach((r) => strikeSet.add(r[0])));
    const strikes = [...strikeSet].sort((a, b) => a - b);
    const sIdx = new Map(strikes.map((k, i) => [k, i]));
    const cells = [];
    snaps.forEach((s, ti) => (V(s).strikes || []).forEach((r) => { if (isNum(r[idx])) cells.push([ti, sIdx.get(r[0]), r[idx]]); }));

    // 回看
    const last = snaps[snaps.length - 1];
    const prevI = Math.max(0, snaps.length - 1 - back);
    const prev = snaps[prevI];
    const prevMap = new Map((V(prev).strikes || []).map((r) => [r[0], r[idx]]));
    const lbRows = (V(last).strikes || []).map((r) => ({ strike: r[0], now: r[idx], before: prevMap.get(r[0]) }));

    // 路由变动
    const routeKey = (s) => ["monthly", "weekly"].map((tn) => `${s.route?.[tn]?.put}/${s.route?.[tn]?.call}`).join("|");
    const changes = snaps.filter((s, i) => i === 0 || routeKey(s) !== routeKey(snaps[i - 1]));

    fill(main, head,
      panel("现价与水平", { sub: "现价（实线）· 最近 Zero Gamma（虚线）· 净 Major+ / Major−" }, lvEl),
      h("div", { style: { height: "20px" } }),
      h("div", { class: "grid two" },
        panel("净 GEX 热力图", { sub: "行权价 × 时间；蓝 = 正、橙 = 负、暖灰 = 接近 0",
          right: seg([["oi", "按 OI"], ["volume", "按成交量"]], basis, (x) => { basis = x; draw(); }, "口径") },
        cells.length ? hmEl : h("div", { class: "empty" }, "—")),
        panel("回看", { sub: `最新一拍 vs ${back} 拍前（${times[prevI]}）`,
          right: seg([[1, "1 拍"], [3, "3 拍"], [6, "6 拍"]], back, (x) => { back = x; draw(); }, "回看") },
        lbRows.length ? lbEl : h("div", { class: "empty" }, "—"))),
      h("div", { style: { height: "20px" } }),
      h("div", { class: "grid halves" },
        panel("总净 GEX", { sub: "美元 / 现价每变动 1%" }, totEl),
        panel("路由变动", { sub: "只列出与上一拍不同的时刻（月度 · 周度，put / call）" },
          table([{ label: "时间", render: (s) => (s.ts || "").slice(11, 16) },
            { label: "月度", render: (s) => `${ROUTE_SHORT[s.route?.monthly?.put] || "—"} / ${ROUTE_SHORT[s.route?.monthly?.call] || "—"}` },
            { label: "周度", render: (s) => `${ROUTE_SHORT[s.route?.weekly?.put] || "—"} / ${ROUTE_SHORT[s.route?.weekly?.call] || "—"}` },
            { label: "现价", cls: "r", render: (s) => num(s.underlying_price) }], changes),
          h("p", { class: "note" }, "盘中路由只供观察：预注册检验只用收盘后的账本行（盘中报价的行不进检验）。"))),
      h("div", { style: { height: "20px" } }), settingsPanel);

    lines(lvEl, { x: times, yFmt: (x) => num(x, 0), series: [
      { name: "现价", data: snaps.map((s) => s.underlying_price), color: c.ink },
      { name: "Zero Gamma", data: snaps.map((s) => V(s).zero_gamma?.nearest ?? null), color: c.ink3, dash: true },
      { name: "Major+", data: snaps.map((s) => V(s).majors?.net_major_pos_strike ?? null), color: c.pos, width: 1.5, step: "end" },
      { name: "Major−", data: snaps.map((s) => V(s).majors?.net_major_neg_strike ?? null), color: c.neg, width: 1.5, step: "end" }] });
    if (cells.length) heatmap(hmEl, { times, strikes, cells });
    lines(totEl, { x: times, yFmt: usdShort, hmarks: [{ value: 0, label: "0", color: c.lineStrong, type: "solid" }],
      series: [{ name: "总净 GEX", data: snaps.map((s) => V(s).net_gex_total ?? null), color: c.ink2 }] });
    if (lbRows.length) strikeBars(lbEl, { rows: lbRows, spot: last.underlying_price, series: [
      { name: "最新", value: (r) => r.now, lane: 0 },
      { name: `${back} 拍前`, value: (r) => r.before, color: c.neutral, lane: 1 }] });
  }
  await draw();
}

function settingsBlock(t, rerender) {
  const st = state.meta?.settings || {};
  const focus = st.focus || [];
  const max = state.meta?.max_focus || 5;
  const poller = state.meta?.poller || {};
  const msg = h("span", { class: "small muted", "aria-live": "polite" });
  const save = async (patch) => {
    try { await api("/api/settings", { body: patch }); await refreshMeta(); rerender(); }
    catch (e) { msg.textContent = e.message; msg.classList.add("err"); }
  };
  const input = h("input", { class: "sel", placeholder: "代码", style: { width: "90px", textTransform: "uppercase" } });
  const lastRes = (poller.last_results || []).map((r) => `${r.ticker} ${r.recorded ? "✓" : (r.reason || "×")}`).join(" · ");
  return panel("盘中快照设置", { sub: `关注最多 ${max} 只（每只每拍约 1.5MB、串行 4–7 秒）· 只在美股常规交易时段拍` },
    h("div", { class: "focus-list" },
      focus.map((x) => h("span", { class: "chip" }, h("a", { href: `#/intraday/${x}` }, x),
        h("button", { type: "button", "aria-label": `移除 ${x}`, onclick: () => save({ focus: focus.filter((y) => y !== x) }) }, "×"))),
      focus.length < max ? h("form", { onsubmit: (e) => { e.preventDefault(); const v = input.value.trim().toUpperCase(); if (v) save({ focus: [...focus, v] }); } },
        input, " ", h("button", { class: "btn", type: "submit" }, "加入")) : null,
      !focus.includes(t) && focus.length < max ? h("button", { class: "btn", type: "button", onclick: () => save({ focus: [...focus, t] }) }, `关注 ${t}`) : null),
    h("div", { class: "controls", style: { marginTop: "14px" } },
      h("label", { class: "small" }, "间隔 ",
        h("select", { class: "sel", onchange: (e) => save({ interval_min: Number(e.target.value) }) },
          [5, 10, 15, 30, 60].map((m) => h("option", { value: m, selected: m === st.interval_min }, `${m} 分钟`)))),
      h("button", { class: "btn", type: "button", onclick: () => save({ poll_enabled: !st.poll_enabled }) },
        st.poll_enabled ? "暂停定时快照" : "恢复定时快照"),
      state.meta?.demo ? null : h("button", { class: "btn primary", type: "button", onclick: async (e) => {
        e.target.disabled = true; e.target.textContent = "拉取中…";
        try { const r = await api(`/api/intraday/${encodeURIComponent(t)}/snap`, { method: "POST" });
          msg.textContent = r.recorded ? `已记录 ${r.ts.slice(11, 16)}` : `未记录：${r.reason}`; rerender(); }
        catch (err) { msg.textContent = err.message; }
        finally { e.target.disabled = false; e.target.textContent = `立即给 ${t} 拍一张`; }
      } }, `立即给 ${t} 拍一张`), msg),
    h("p", { class: "note" }, poller.disabled ? "定时快照未启动（--no-poll 或演示模式）。"
      : `后台：${poller.running ? "运行中" : "未运行"} · 已跑 ${poller.rounds ?? 0} 轮 · 最近一轮 ${poller.last_round_et ? poller.last_round_et.slice(11, 16) + " ET" : "—"}${lastRes ? " · " + lastRes : ""}${poller.errors ? ` · 异常 ${poller.errors} 次` : ""}`),
    st.settings_error ? h("p", { class: "callout warn" }, `设置文件读不了，按缺省处理：${st.settings_error}`) : null);
}
