// 标的 · 成交（成交榜，v0.45.776）：这只票整条链的合约按当日累计成交量排名。
// 数都由 /api/volume-board 算好（口径见帮助页 #/help/volume）；筛、排、截断都在服务端，这里只排版。
// 读的是「标的」页刚现算用的那份链，不另取；顶部「重新拉取」已经让标的页强制现算过（新链随之留在服务端），
// 所以本页**从不带 force**——再带一次就是同一次「重新拉取」打两遍 CBOE。
import { api, h, num, isNum, panel, seg, table, downloadCSV, pref, setPref, fill, loading, timeAgo,
  usdShort, signedPct, fetchReasonText } from "../lib.js";

const PREF_KEY = "volume.params";
const DEFAULT_PREFS = { side: "all", sort: "volume", limit: 50 };

export async function volume(body, d, t, routeAlive) {
  const alive = () => routeAlive() && body.isConnected;
  let pick = null;                         // 只看哪个到期日（不记在本机：到期日随链变）
  let notice = null;
  let seq = 0;
  const saved = pref(PREF_KEY, {}) || {};
  let prefs = { side: saved.side || DEFAULT_PREFS.side, sort: saved.sort || DEFAULT_PREFS.sort,
    limit: saved.limit || DEFAULT_PREFS.limit };
  const isDefault = () => Object.keys(DEFAULT_PREFS).every((k) => String(prefs[k]) === String(DEFAULT_PREFS[k]));

  async function load() {
    const mine = ++seq;
    const q = { ...prefs, ...(pick ? { expiry: pick } : {}) };
    const qs = Object.entries(q).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
    let r;
    try {
      r = await api(`/api/volume-board/${encodeURIComponent(t)}?${qs}`);
    } catch (e) {
      if (!alive() || mine !== seq) return;
      if (e.status === 400 && (e.param === "expiry") && pick) { pick = null; notice = `${e.message}，已改回全部到期日`; return load(); }
      if (e.status === 400 && ["side", "sort", "limit"].includes(e.param) && !isDefault()) {
        // 本机记的筛选被服务端拒（比如旧版本记下的取值）：清掉、回缺省（缺省本身被拒就别再重试，落到下面报错）
        setPref(PREF_KEY, {}); prefs = { ...DEFAULT_PREFS };
        notice = `筛选被拒（${e.message}），已回到缺省`; return load();
      }
      fill(body, h("div", { class: "empty err" }, `成交榜取不到：${e.message}`),
        h("p", {}, h("button", { class: "btn", type: "button", onclick: () => { fill(body, loading()); load(); } }, "重试")));
      return;
    }
    if (!alive() || mine !== seq) return;
    setPref(PREF_KEY, prefs);
    draw(r);
  }

  const setP = (k, v) => { prefs = { ...prefs, [k]: v }; notice = null; load(); };

  function draw(r) {
    const R = r.rules || {};
    const reasonZh = (code) => (R.reasons || {})[code] || fetchReasonText(code, r.retry_after_sec) || code || "—";
    if (!r.data_available) {
      fill(body, panel(null, {}, h("div", { class: "empty" }, "成交榜现在算不出来。"),
        h("p", { class: "callout warn" }, `原因：${reasonZh(r.reason)}`)));
      return;
    }
    const s = r.summary || {}, c = r.counts || {}, ch = r.chain || {};
    const sortOpts = Object.entries(R.sorts || { volume: "成交量" });
    const controls = h("div", { class: "controls" },
      seg(Object.entries(R.sides || { all: "全部" }), prefs.side, (v) => setP("side", v), "方向"),
      seg(sortOpts, prefs.sort, (v) => setP("sort", v), "排序"),
      seg((R.limit_choices || [50]).map((n) => [n, `前 ${n}`]), Number(prefs.limit), (v) => setP("limit", v), "列几行"),
      h("select", { "aria-label": "到期日", onchange: (e) => { pick = e.target.value || null; notice = null; load(); } },
        h("option", { value: "", selected: !pick }, "全部到期日"),
        (r.expiries || []).map((x) => h("option", { value: x, selected: x === pick }, x))));

    const kpi = (k, v, sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
      sub ? h("div", { class: "small muted" }, sub) : null);
    const kpis = h("div", { class: "kpis" },
      kpi("总成交量", num(s.total_volume, 0), `${num(s.n_traded, 0)} 个合约有成交`),
      kpi("Call / Put 成交量", `${num(s.call_volume, 0)} / ${num(s.put_volume, 0)}`,
        isNum(s.call_pct) ? `Call 占 ${num(s.call_pct, 1)}%` : null),
      kpi("Put ÷ Call（成交量）", isNum(s.put_call_volume) ? num(s.put_call_volume, 2) : "—", "当日成交，不是 OI"),
      kpi("约成交额 Call / Put", `${usdShort(s.call_notional_est)} / ${usdShort(s.put_notional_est)}`, "按现在的中间价估算"),
      kpi("列出的占比", isNum(s.top_volume_share_pct) ? `${num(s.top_volume_share_pct, 1)}%` : "—",
        `列出的 ${num(s.n_listed, 0)} 个合约占这个范围总成交量`));

    const cols = [
      { label: "#", cls: "r", render: (x) => x.rank, csv: (x) => x.rank },
      { label: "侧", render: (x) => h("span", { class: `chip ${x.side === "call" ? "pos" : "neg"}` }, x.side === "call" ? "CALL" : "PUT"),
        csv: (x) => x.side },
      { label: "行权价", cls: "r", render: (x) => num(x.strike, 2), csv: (x) => x.strike },
      { label: "到期", render: (x) => h("span", {}, x.expiry, isNum(x.dte) ? h("span", { class: "sub" }, ` ${num(x.dte, 0)} 天`) : null),
        csv: (x) => x.expiry },
      { label: "虚值 %（负 = 实值）", cls: "r", render: (x) => signedPct(x.otm_pct, 1), csv: (x) => x.otm_pct },
      { label: "成交量", cls: "r", render: (x) => h("b", {}, num(x.volume, 0)), csv: (x) => x.volume },
      { label: "占比", cls: "r", render: (x) => (isNum(x.volume_share_pct) ? `${num(x.volume_share_pct, 1)}%` : "—"), csv: (x) => x.volume_share_pct },
      { label: "OI（前一日）", cls: "r", render: (x) => num(x.oi, 0), csv: (x) => x.oi },
      { label: "成交 ÷ OI", cls: "r", render: (x) => (isNum(x.vol_oi) ? `${num(x.vol_oi, 2)}×` : "—"), csv: (x) => x.vol_oi },
      { label: "bid / ask", cls: "r", render: (x) => `${num(x.bid, 2)} / ${num(x.ask, 2)}`, csv: (x) => `${x.bid ?? ""}/${x.ask ?? ""}` },
      { label: "约成交额", cls: "r", render: (x) => usdShort(x.notional_est), csv: (x) => x.notional_est },
      { label: "IV", cls: "r", render: (x) => (isNum(x.iv) ? `${num(x.iv * 100, 1)}%` : "—"), csv: (x) => x.iv },
      { label: "Δ", cls: "r", render: (x) => num(x.delta, 2), csv: (x) => x.delta },
    ];
    const missing = [
      c.n_volume_missing ? `${c.n_volume_missing} 个合约链里没有成交量，没进榜` : null,
      c.n_unparseable ? `${c.n_unparseable} 个合约认不出行权价 / 方向 / 到期日，没进榜` : null,
    ].filter(Boolean).join("；");
    const sub = `链 ${ch.as_of || "—"} · 最后成交 ${(ch.payload_last_trade_time || "—").replace("T", " ")} · 链取于 ${timeAgo(r.chain_age_sec)}`
      + `${ch.session_live ? " · 盘中（行情延迟约 15 分钟）" : ""}`;
    const vo = prefs.sort === "vol_oi" ? ` · 按「成交 ÷ OI」排时成交量不足 ${R.vol_oi_min_volume} 张的、或 OI 为 0 的排在最后` : "";
    const csvBtn = r.rows.length ? h("button", { class: "btn", type: "button",
      onclick: () => downloadCSV(`${t}_成交榜_${ch.as_of || ""}.csv`, cols, r.rows) }, "导出 CSV") : null;

    fill(body,
      notice ? h("p", { class: "callout warn" }, notice) : null,
      h("p", { class: "callout" }, r.disclaimer),
      panel("成交概况", { sub: pick ? `只看 ${pick}` + (prefs.side !== "all" ? ` · 只看 ${prefs.side.toUpperCase()}` : "")
        : (prefs.side !== "all" ? `只看 ${prefs.side.toUpperCase()}` : "整条链") }, kpis),
      panel("成交榜", { sub: sub + vo, right: csvBtn }, controls,
        r.rows.length ? table(cols, r.rows) : h("div", { class: "empty" }, reasonZh(r.reason || "no_volume_today")),
        missing ? h("p", { class: "small muted" }, missing) : null));
  }

  fill(body, loading("排成交榜…"));
  await load();
}
