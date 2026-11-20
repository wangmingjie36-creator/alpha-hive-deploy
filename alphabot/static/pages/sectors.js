// 板块（v0.45.783）：标普 500 成分股按 SPY 权重 × 涨跌幅的热力图。数都由 /api/sector-map 算好（口径见帮助页 #/help/sectors），
// 这里只排版、上色。切周期不重取（两个周期的数在同一份回复里）。
// 续刷：交易时段每 `refresh_sec` 秒、其余时间每 `quote_ttl_idle_sec` 秒问一次本机服务；真正打不打 Yahoo 由服务端的缓存与失败冷却决定（请求量的硬上限在那边），
// 所以这里不需要 retry_gate——限流时服务端回的是上一份报价 + 「刷新失败」，页面照写。
import { api, h, num, isNum, panel, seg, pref, setPref, fill, loading, timeAgo, signedPct, helpLink } from "../lib.js";
import { sectorTreemap, heatColor, release } from "../charts.js";
import { go } from "../app.js";

const PREF_KEY = "sectors.period";
const STATE_ZH = { REGULAR: "盘中", PRE: "盘前（显示上一场收盘）", PREPRE: "盘前（显示上一场收盘）", POST: "盘后（显示今天收盘）",
  POSTPOST: "已收盘", CLOSED: "已收盘" };

export async function renderSectors(app, routeAlive) {
  const host = h("div", {});
  fill(app, host);
  const alive = () => routeAlive() && host.isConnected;
  let period = pref(PREF_KEY, "1d");
  let last = null, chart = null, timer = null, seq = 0, busy = false;

  async function load(refresh) {
    const mine = ++seq;
    clearTimeout(timer);
    busy = true;
    if (last) draw(last);                         // 让「刷新」按钮立刻变灰
    let r;
    try {
      r = await api(`/api/sector-map${refresh ? "?refresh=1" : ""}`);
    } catch (e) {
      if (!alive() || mine !== seq) return;
      busy = false;
      again(last);                                // 一次没问到（本机服务重启等）不该让续刷从此停掉
      if (last) { last = { ...last, _error: e.message }; draw(last); } else {
        fill(host, head(null), h("div", { class: "empty err" }, `板块图取不到：${e.message}`),
          h("p", { class: "c" }, h("button", { class: "btn", type: "button", onclick: () => { fill(host, loading()); load(true); } }, "重试")));
      }
      return;
    }
    if (!alive() || mine !== seq) return;
    busy = false;
    last = r;
    draw(r);
    again(r);
  }

  // 下一次什么时候再问本机服务：盘中按 `refresh_sec`；不在交易时段按服务端的闲时缓存期（开盘前打开的页面要能自己接上开盘，
  // 收盘前取的那一份也要换成真正的收盘价）。画不出来时也按闲时间隔再试。
  function again(r) {
    const idle = r?.rules?.quote_ttl_idle_sec;
    const sec = r?.available && r.session?.live && isNum(r.refresh_sec) ? r.refresh_sec : isNum(idle) ? idle : 900;
    timer = setTimeout(() => { if (alive()) load(false); }, sec * 1000);
  }

  function head(r) {
    const P = r?.rules?.periods || {};
    return h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "板块", h("span", { class: "sub" }, "标普 500 热力图")), helpLink("sectors")),
      h("div", { class: "controls" },
        r?.available ? seg(Object.entries(P).map(([k, v]) => [k, v.label]), period, (v) => { period = v; setPref(PREF_KEY, v); draw(last); }, "周期") : null,
        h("button", { class: "btn", type: "button", disabled: busy, onclick: () => load(true) }, busy ? "刷新中…" : "刷新")));
  }

  function draw(r) {
    if (chart) { release(chart); chart = null; }
    if (!r.available) {
      const wait = isNum(r.retry_after_sec) ? `，约 ${num(Math.max(r.retry_after_sec, 1), 0)} 秒后再试` : "";
      fill(host, head(r), panel(null, {}, h("div", { class: "empty" }, "板块图现在画不出来。"),
        h("p", { class: "callout warn" }, `原因：${r.reason_zh || r.reason || "未知"}${r.detail ? `（${r.detail}）` : ""}${wait}`)));
      return;
    }
    const P = r.rules.periods;
    if (!P[period]) period = "1d";
    // 顶部那排跟着选中的周期走；色块图只有 `map` 为真的周期（今日 / 52 周），选了 5 日 / 1 月时它仍画今日并写明
    const stripField = P[period].field;
    const mapKey = P[period].map === false ? "1d" : period;
    const field = P[mapKey].field;
    const clamp = P[mapKey].clamp_pct;
    const st = r.stats, hd = r.holdings, q = r.quotes, eb = r.etf_bars || {};
    const etfChange = (e) => e?.[stripField];
    const sectorChange = (s) => (period === "1d" && !isNum(s.etf?.change_1d) ? s.change_1d : etfChange(s.etf));
    // 每一格的悬停说明：5 日 / 1 月写基准日；没数写原因（不留一个光秃秃的「—」）
    const why = (e) => {
      if (P[period].map !== false) return "";
      const base = e?.[`${stripField}_base`], bad = e?.[`${stripField}_reason`];
      if (isNum(e?.[stripField])) return ` · 对 ${base} 收盘（复权，含分红）`;
      return ` · 没有数：${r.rules.reasons[bad] || bad || (eb.available ? "原因未知" : "日线没取到")}`;
    };
    const chip = (name, v, title) => h("span", { class: `chip ${isNum(v) ? (v > 0 ? "pos" : v < 0 ? "neg" : "") : ""}`, title: title || "" },
      name, h("b", {}, signedPct(v, period === "1d" ? 2 : 1)));
    const strip = h("div", { class: "sector-strip" },
      chip("标普 500（SPY）", etfChange(r.bench), `SPY 自己的报价${why(r.bench)}`),
      [...r.sectors].filter((s) => s.etf).sort((a, b) => (sectorChange(b) ?? -1e9) - (sectorChange(a) ?? -1e9))
        .map((s) => chip(s.name, sectorChange(s), `${s.etf.symbol} 自己的报价 · 占标普 ${num(s.weight, 1)}% · ${s.n} 只${why(s.etf)}`)));

    const el = h("div", { class: "chart sector-map", role: "img",
      "aria-label": `标普 500 板块热力图，${P[mapKey].label}涨跌：上涨 ${st.up} 只，下跌 ${st.down} 只` });
    const steps = [-1, -0.66, -0.33, 0, 0.33, 0.66, 1];
    const legend = h("div", { class: "sector-legend" },
      h("span", {}, `−${clamp}% 及以下`),
      steps.map((t) => h("i", { style: { backgroundColor: heatColor(t * clamp, clamp) } })),
      h("span", {}, `+${clamp}% 及以上`),
      h("i", { style: { backgroundColor: heatColor(null, clamp) } }), h("span", {}, "无数据"));

    const when = (r.last_trade_et || "").replace("T", " ").slice(0, 16);
    const sub = [`${STATE_ZH[r.market_state] || r.market_state || "状态未知"}`,
      when ? `最后成交 ${when} ET` : null,
      r.session?.live && isNum(r.delay_min) ? `行情约延迟 ${num(r.delay_min, 0)} 分钟` : null,
      `报价取于 ${timeAgo(q.age_sec) || "刚刚"}`,
      r.session?.live ? `每 ${num(r.refresh_sec / 60, 0)} 分钟自动刷新` : `不在交易时段，每 ${num((r.rules.quote_ttl_idle_sec || 900) / 60, 0)} 分钟看一次`,
      `成分与权重截至 ${hd.as_of || "—"}`].filter(Boolean).join(" · ");

    const notes = [
      P[period].map === false ? `${P[period].label}涨跌只有顶部那一排（标普 500 和 11 只板块基金）有：现价对 ${r.rules.etf_lookback?.[period] ?? "若干"} 个交易日前的收盘，含分红。下面的色块图没有这个周期，画的是今日。` : null,
      mapKey === "1d" ? `上涨 ${st.up} 只 · 下跌 ${st.down} 只 · 平 ${st.flat} 只（共 ${st.n} 只）` : "52 周涨跌来自 Yahoo 报价；板块那一行是板块基金自己的 52 周涨跌。",
      st.n_missing ? `${st.n_missing} 只没上色（占权重 ${num(st.weight_total - st.weight_quoted, 2)}%）：`
        + st.missing.slice(0, 12).map((m) => `${m.ticker}（${r.rules.reasons[m.reason] || m.reason}）`).join("、")
        + (st.missing.length > 12 ? " 等" : "") : null,
      hd.skipped.length ? `持仓表里 ${hd.skipped.length} 项不是普通股、没进图：${hd.skipped.map((x) => x.ticker).join("、")}` : null,
    ].filter(Boolean);
    const warns = [
      r._error ? `刚才这次刷新没成功（${r._error}），显示的是上一份。` : null,
      q.refresh_failed ? `报价刷新失败（${q.refresh_failed.reason_zh || q.refresh_failed.reason}`
        + `${isNum(q.refresh_failed.retry_after_sec) ? `，约 ${num(Math.max(q.refresh_failed.retry_after_sec, 1), 0)} 秒后再试` : ""}），显示的是 ${timeAgo(q.age_sec)}取到的那一份。` : null,
      eb.refresh_failed ? `板块基金的日线没取到（${eb.refresh_failed.reason_zh || eb.refresh_failed.reason}），顶部那排的 5 日 / 1 月${eb.available ? "可能不全" : "写不出来"}；今日和 52 周不受影响。` : null,
      !eb.refresh_failed && Object.keys(eb.errors || {}).length ? `${Object.keys(eb.errors).join("、")} 的日线这次没取到，它们的 5 日 / 1 月暂时没有，十分钟后自动再取。` : null,
      hd.refresh_failed ? `持仓文件刷新失败（${hd.refresh_failed.reason_zh || hd.refresh_failed.reason}），成分与权重沿用 ${hd.as_of || "上一份"}。` : null,
      hd.failed_sectors.length ? `${hd.failed_sectors.join("、")} 的板块文件这次没取到`
        + (hd.carried ? `，其中 ${hd.carried} 只沿用上一份的板块归属` : "") + "；归不了板块的在「未归类」里，十分钟后自动再取。" : null,
      Object.keys(hd.multi_sector).length ? `${Object.keys(hd.multi_sector).join("、")} 同时出现在多只板块基金里，按第一只归类。` : null,
    ].filter(Boolean);

    fill(host, head(r),
      warns.map((w) => h("p", { class: "callout warn" }, w)),
      panel(mapKey === period ? `${P[period].label}涨跌` : `板块 ${P[period].label} · 色块图 ${P[mapKey].label}`, { sub }, strip, el, legend,
        notes.map((n) => h("p", { class: "small muted" }, n))),
      h("p", { class: "callout" }, r.disclaimer, " 点色块打开那只票的「标的」页。"));
    chart = sectorTreemap(el, { sectors: r.sectors, field, clamp, periodLabel: P[mapKey].label, reasons: r.rules.reasons,
      onPick: (k) => go(`#/t/${encodeURIComponent(k.ticker)}/levels`) });
  }

  fill(host, head(null), loading("取成分与报价…（每天第一次要下载 12 份持仓文件和 12 只基金的日线，约一分钟）"));
  await load(false);
}
