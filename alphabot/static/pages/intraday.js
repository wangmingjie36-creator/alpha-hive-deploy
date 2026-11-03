// 盘中：关注列表定时快照 → 水平走势、GEX 热力图、回看、路由变动。只看单日（跨日拼接 = 历史路由叠其后价格）。
import { api, h, helpLink, num, usdShort, seg, table, panel, pref, setPref, ROUTE_SHORT, fill } from "../lib.js";
import { lines, gammaTrace, strikeBars, release, C } from "../charts.js";
import { state, refreshMeta } from "../app.js";
import { sidebar, tLabel } from "./ticker.js";

export async function renderIntraday(root, t, alive) {
  const [dates, meta] = await Promise.all([api(`/api/intraday-dates/${encodeURIComponent(t)}`), refreshMeta()]);
  if (!alive()) return;
  let date = (dates.dates || [])[0] || (meta.session?.date_et);
  let view = pref("idView", "le_45dte");
  let basis = "oi";
  let mode = "level";
  const isAct = () => basis === "activity" || basis === "activity_volume";
  let back = 3;
  let charts = [];
  const main = h("div", {});
  fill(root, h("div", { class: "with-side" }, sidebar(t, "#/intraday/{T}"), main));

  async function draw() {
    const q = date ? `date=${date}&` : "";
    const [d, hm] = await Promise.all([api(`/api/intraday/${encodeURIComponent(t)}?${q}`),
      api(`/api/intraday-heat/${encodeURIComponent(t)}?${q}view=${view}&basis=${basis}&mode=${isAct() ? "level" : mode}`)]);
    if (!alive()) return;
    charts.forEach(release); charts = [];
    const snaps = d.snapshots || [];
    const head = h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, `${tLabel(t)} 盘中`, h("span", { class: "sub" }, `${d.date} · ${snaps.length} 张快照`)),
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
    const hmHead = h("div", { class: "controls" },
      seg([["oi", "按 OI"], ["volume", "按成交量"], ["activity", "成交活跃度"], ["activity_volume", "活跃度（张）"]],
        basis, (x) => { basis = x; draw(); }, "口径"),
      // 成交活跃度本身就是逐拍增量，没有「逐拍变化」
      isAct() ? null : seg([["level", "当前值"], ["delta", "逐拍变化"]], mode, (x) => { mode = x; draw(); }, "显示"));
    const totEl = h("div", { class: "chart short" });
    const lbEl = h("div", { class: "chart tall" });

    const idx = basis === "oi" ? 1 : 2;     // 回看面板只比 OI / 成交量口径的净 GEX（活跃度口径时比成交量口径）

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
      panel(hm.signed === false ? "成交活跃度热力图 · 价格路径" : "净 GEX 热力图 · 价格路径", { sub: heatSub(hm), right: hmHead },
        hm.available ? hmEl : h("div", { class: "empty" }, hm.reason_label || "—"),
        activityNotes(hm),
        heatNotes(hm)),
      h("div", { style: { height: "20px" } }),
      h("div", { class: "grid two" },
        panel("现价与水平", { sub: "现价（实线）· 最近 Zero Gamma（虚线）· 净 Major+ / Major−" }, lvEl),
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

    charts.push(lines(lvEl, { x: times, yFmt: (x) => num(x, 0), series: [
      { name: "现价", data: snaps.map((s) => s.underlying_price), color: c.ink },
      { name: "Zero Gamma", data: snaps.map((s) => V(s).zero_gamma?.nearest ?? null), color: c.ink3, dash: true },
      { name: "Major+", data: snaps.map((s) => V(s).majors?.net_major_pos_strike ?? null), color: c.pos, width: 1.5, step: "end" },
      { name: "Major−", data: snaps.map((s) => V(s).majors?.net_major_neg_strike ?? null), color: c.neg, width: 1.5, step: "end" }] }));
    if (hm.available) charts.push(gammaTrace(hmEl, hm));
    charts.push(lines(totEl, { x: times, yFmt: usdShort, hmarks: [{ value: 0, label: "0", color: c.lineStrong, type: "solid" }],
      series: [{ name: "总净 GEX", data: snaps.map((s) => V(s).net_gex_total ?? null), color: c.ink2 }] }));
    if (lbRows.length) charts.push(strikeBars(lbEl, { rows: lbRows, spot: last.underlying_price, series: [
      { name: "最新", value: (r) => r.now, lane: 0 },
      { name: `${back} 拍前`, value: (r) => r.before, color: c.neutral, lane: 1 }] }));
  }
  await draw();
}

// 热力图口径说明：数字只取服务端回传的 rules / clip（intraday_heat.py），这里不另写常量
function heatSub(hm) {
  if (hm.signed === false) {
    const what = hm.basis === "activity" ? "这一拍比上一拍新增的成交量 × gamma（美元 / 现价每变动 1%）" : "这一拍比上一拍新增的成交量（张）";
    return `行权价 × 拍；${what}；call 与 put 都按正值相加，颜色越深越活跃。实线 = 现价，虚线 = 最近 Zero Gamma`;
  }
  const what = hm.mode === "delta" ? "这一拍减上一拍同一行权价" : "净 GEX（美元 / 现价每变动 1%）";
  return `行权价 × 拍；${what}；蓝 = 正、橙 = 负。实线 = 现价，虚线 = 最近 Zero Gamma`;
}
function heatNotes(hm) {
  if (!hm.available) return null;
  const r = hm.rules || {};
  const items = [
    hm.signed === false
      ? `颜色按 ${Math.round((r.clip_quantile ?? 0) * 100)}% 分位截断在 ${fmtHeat(hm, hm.clip)}（最大 ${fmtHeat(hm, hm.max_abs)}，${hm.n_clipped} 格按截断色画，悬停看真值）。`
      : `颜色按 |值| 的 ${Math.round((r.clip_quantile ?? 0) * 100)}% 分位截断在 ±${usdShort(hm.clip)}（最大 ${usdShort(hm.max_abs)}，${hm.n_clipped} 格按截断色画，悬停看真值）。`,
    `只存现价 ±${Math.round((r.strike_band ?? 0) * 100)}% 内的行权价，现价移动时边缘格子会空着（不是 0）。`,
  ];
  if (hm.bucket) items.push(`行权价共 ${hm.n_strikes_raw} 个、超过 ${r.max_rows} 行：每 ${num(hm.bucket)} 点分一桶、桶内求和（GEX 按行权价可加），悬停看区间与个数。窗口边缘的桶在现价移动时只装得下一部分行权价，那里的颜色变化可能只是进出窗口，悬停看个数。`);
  if (hm.mode === "delta") items.push(hm.basis === "oi"
    ? "OI 盘中不变：OI 口径的逐拍变化只来自现价与 IV 的移动。"
    : "成交量口径的逐拍变化 ≈ 这段时间新增成交带来的 GEX——没有买卖方向（延迟快照分不出主动买卖），不是 orderflow。");
  if ((hm.gaps || []).length) items.push(`x 轴按拍排列、不等距：${hm.gaps.map((g) => `${hm.times[g.index]} 前隔了 ${g.minutes} 分钟`).join("、")}。`);
  if (hm.n_bad_lines) items.push(`快照文件有 ${hm.n_bad_lines} 行读不了，已跳过。`);
  return h("ul", { class: "note" }, items.map((x) => h("li", {}, x)));
}

const fmtHeat = (hm, v) => (hm.basis === "activity_volume" ? `${num(v, 0)} 张` : `${usdShort(v)} / 1%`);

// 成交活跃度的口径与限制（无论这天能不能画都给：说清楚它是什么、不是什么，以及哪几拍为什么空着）
function activityNotes(hm) {
  if (hm.signed !== false) return null;
  const labels = hm.activity_reason_labels || {};
  const items = [
    "只是活跃度：分不出买卖方向、分不出开仓还是平仓（CBOE 延迟快照没有这些信息），价差 / 跨式的每条腿都算——不是 GEXBot 的 State / Orderflow，不能据此推断做市商仓位朝哪边。",
    "逐个合约先减成交量、再乘这一拍的 gamma，所以现价移动造成的 gamma 变化不会混进来；gamma 加权会让临近到期的平值合约压过其余行权价，「活跃度（张）」是不加权的对照。",
    "CBOE 数据约延迟 15 分钟；两拍之间的成交只看得到总量。只展示，未经前向检验。",
  ];
  const by = {};
  for (const m of hm.activity_missing || []) (by[m.reason] = by[m.reason] || []).push(hm.times?.[m.index] ?? m.index);
  for (const [why, ts] of Object.entries(by)) {
    const which = ts.length > 6 ? `${ts[0]} 到 ${ts[ts.length - 1]} 之间的 ${ts.length} 拍` : `${ts.join("、")} 这几拍`;
    items.push(`${which}空着：${labels[why] || why}。`);
  }
  const n = hm.activity_counts || {};
  const skipped = [["n_no_previous", "上一拍没有这个合约"], ["n_volume_decreased", "成交量比上一拍小（数据源修正）"],
    ["n_no_volume", "缺成交量"], ["n_no_gamma", "缺 gamma"], ["n_duplicate_key", "合约身份重复"], ["n_bad_contract", "合约形状不对"]]
    .filter(([k]) => n[k]).map(([k, lab]) => `${lab} ${n[k]}`);
  if (skipped.length) items.push(`没算进去的合约（各拍合计；算进去的是 ${n.n_used ?? 0} 个合约 × 拍）：${skipped.join("，")}。某个行权价一个合约都没算进去时那格空着，不是 0。`);
  return h("ul", { class: "note" }, items.map((x) => h("li", {}, x)));
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
      focus.map((x) => h("span", { class: "chip" }, h("a", { href: `#/intraday/${x}` }, tLabel(x)),
        h("button", { type: "button", "aria-label": `移除 ${tLabel(x)}`, onclick: () => save({ focus: focus.filter((y) => y !== x) }) }, "×"))),
      focus.length < max ? h("form", { onsubmit: (e) => { e.preventDefault(); const v = input.value.trim().toUpperCase(); if (v) save({ focus: [...focus, v] }); } },
        input, " ", h("button", { class: "btn", type: "submit" }, "加入")) : null,
      !focus.includes(t) && focus.length < max ? h("button", { class: "btn", type: "button", onclick: () => save({ focus: [...focus, t] }) }, `关注 ${tLabel(t)}`) : null),
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
        finally { e.target.disabled = false; e.target.textContent = `立即给 ${tLabel(t)} 拍一张`; }
      } }, `立即给 ${tLabel(t)} 拍一张`), msg),
    h("p", { class: "note" }, poller.disabled ? "定时快照未启动（--no-poll 或演示模式）。"
      : `后台：${poller.running ? "运行中" : "未运行"} · 已跑 ${poller.rounds ?? 0} 轮 · 最近一轮 ${poller.last_round_et ? poller.last_round_et.slice(11, 16) + " ET" : "—"}${lastRes ? " · " + lastRes : ""}${poller.errors ? ` · 异常 ${poller.errors} 次` : ""}`),
    st.settings_error ? h("p", { class: "callout warn" }, `设置文件读不了，按缺省处理：${st.settings_error}`) : null);
}
