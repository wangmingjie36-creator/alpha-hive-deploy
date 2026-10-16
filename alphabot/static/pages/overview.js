// 总览：账本某日（缺省最新一日）全部标的一屏。读的是收盘后记录的行，不逐只现拉（每只 4–7 秒）。
import { api, h, helpLink, num, pct, pctPts, signChip, seg, table, panel, downloadCSV, pref, setPref, isNum,
  ROUTE_SHORT, EARN_LABEL, SOURCE_LABEL, TENOR_LABEL, CURVE_LABEL, money, fill } from "../lib.js";
import { go } from "../app.js";

export async function renderOverview(root, alive) {
  const dates = await api("/api/ledger/dates");
  let date = null;
  let tenor = pref("ovTenor", "monthly");
  let filter = "all";

  async function draw() {
    const q = date ? `?date=${encodeURIComponent(date)}` : "";
    const d = await api(`/api/overview${q}`);
    if (!alive()) return;
    date = d.as_of || date;
    const rows = (d.tenors?.[tenor] || []);
    const rec = rows.filter((r) => r.status === "recorded");
    const unav = rows.filter((r) => r.status !== "recorded");
    const flagged = (r) => r.route && (r.route.flag_put || r.route.flag_call);
    const shown = rec.filter((r) =>
      filter === "all" ? true
        : filter === "neg" ? r.env?.sign_at_spot === "negative"
          : filter === "flag" ? flagged(r)
            : r.earnings_status === "before_expiry" || r.earnings_status === "unknown");

    const head = h("div", { class: "page-head" },
      h("div", {},
        h("h1", {}, "总览", h("span", { class: "sub" }, d.source === "demo" ? "演示数据" : (date ? `账本 ${date}` : ""))),
        h("p", { class: "lede" }, "每只标的的 GEX 环境、路由与卖权候选。数据取自每日收盘后记录的账本行；要看实时报价，点进标的页现拉。 ", helpLink("overview"))),
      h("div", { class: "controls" },
        (dates.dates || []).length && d.source !== "demo" ? h("select", { class: "sel", "aria-label": "账本日期",
          onchange: (e) => { date = e.target.value; draw(); } },
          (dates.dates || []).map((x) => h("option", { value: x, selected: x === date }, x))) : null,
        seg([["monthly", "月度"], ["weekly", "周度"]], tenor, (v) => { tenor = v; setPref("ovTenor", v); draw(); }, "期限"),
      ));

    if (!d.data_available) {
      const sd = d.state_dir || {};
      fill(root, head, panel(null, {},
        h("div", { class: "empty" }, sd.exists === false
          ? `没找到卖权账本目录：${sd.path || "—"}` : "账本里还没有任何行。"),
        sd.hint ? h("p", { class: "callout warn" }, sd.hint) : null,
        h("p", { class: "small muted" }, "可以直接在右上角输入代码，到标的页现拉实时期权链。")));
      return;
    }

    const assess = d.assess?.[tenor];
    const nNeg = rec.filter((r) => r.env?.sign_at_spot === "negative").length;
    const kpis = h("div", { class: "kpis" },
      kpi("已记录", `${rec.length}`), kpi("负 gamma", `${nNeg}`),
      kpi("路由退到远档", `${rec.filter(flagged).length}`), kpi("不可得", `${unav.length}`));

    const cols = [
      { label: "代码", render: (r) => h("b", {}, r.ticker), csv: (r) => r.ticker },
      { label: "现价", cls: "r", render: (r) => num(r.underlying_price), csv: (r) => r.underlying_price },
      { label: "现价处 gamma", render: (r) => h("span", {}, signChip(r.env?.sign_at_spot),
        h("span", { class: "sub" }, CURVE_LABEL[r.env?.curve_state] || "")), csv: (r) => r.env?.sign_at_spot },
      { label: "最近零点", cls: "r", render: (r) => h("span", {}, num(r.env?.zg_nearest),
        h("span", { class: "sub" }, zgDist(r.env))), csv: (r) => r.env?.zg_nearest },
      { label: "净 Major+ / −", cls: "r", render: (r) => `${num(r.env?.net_major_pos_strike, 0)} / ${num(r.env?.net_major_neg_strike, 0)}`,
        csv: (r) => `${r.env?.net_major_pos_strike ?? ""}/${r.env?.net_major_neg_strike ?? ""}` },
      { label: "路由 put · call", render: (r) => h("span", {}, routeTxt(r.route?.put), " · ", routeTxt(r.route?.call)),
        csv: (r) => `${r.route?.put}/${r.route?.call}` },
      { label: "到期", render: (r) => h("span", {}, r.expiry || "—", h("span", { class: "sub" }, isNum(r.dte) ? `${r.dte} 天` : "")),
        csv: (r) => r.expiry },
      { label: "卖 put（路由档）", cls: "r", render: (r) => legCell(r.route_legs?.put), csv: (r) => r.route_legs?.put?.strike },
      { label: "卖 call（路由档）", cls: "r", render: (r) => legCell(r.route_legs?.call), csv: (r) => r.route_legs?.call?.strike },
      { label: "财报", render: (r) => r.earnings_status === "before_expiry" || r.earnings_status === "unknown"
        ? h("span", { class: "chip warn" }, EARN_LABEL[r.earnings_status]) : (EARN_LABEL[r.earnings_status] || "—"),
      csv: (r) => r.earnings_status },
      { label: "报价", render: (r) => h("span", { class: "small muted" }, SOURCE_LABEL[r.underlying_price_source] || r.underlying_price_source || "—"),
        csv: (r) => r.underlying_price_source },
    ];
    const filters = seg([["all", "全部"], ["neg", "负 gamma"], ["flag", "路由退后"], ["earn", "财报冲突"]], filter,
      (v) => { filter = v; draw(); }, "筛选");
    const tbl = table(cols, shown, { onRow: (r) => go(`#/t/${r.ticker}/sell`) });
    const exportBtn = h("button", { class: "btn", type: "button",
      onclick: () => downloadCSV(`alphabot-overview-${tenor}-${date || "live"}.csv`, cols, shown) }, "导出 CSV");

    fill(root, head,
      assess?.summary ? h("p", { class: "callout" }, assess.summary) : null,
      panel(`${TENOR_LABEL[tenor]}`, { right: h("div", { class: "controls" }, filters, exportBtn) },
        kpis, h("div", { style: { height: "14px" } }), tbl,
        h("p", { class: "note" }, "点任一行进入该标的的卖权工作台。单笔回报 = 权利金 ÷ 占用资金（卖 put 按现金担保 K）；年化只在标的页作次要参考。")),
      unav.length ? panel("不可得", { sub: "当日没有记成候选的标的与原因" },
        table([{ label: "代码", render: (r) => r.ticker }, { label: "原因", render: (r) => r.unavailable_reason || "—" }], unav)) : null);
  }
  await draw();
}

function kpi(k, v) { return h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v)); }
function routeTxt(v) { return v === "far" ? h("b", {}, "far") : (ROUTE_SHORT[v] || "—"); }
function zgDist(env) {
  if (!env) return "";
  const parts = [];
  if (isNum(env.zg_below_pct)) parts.push(`下方 ${pctPts(env.zg_below_pct)}`);
  if (isNum(env.zg_above_pct)) parts.push(`上方 ${pctPts(env.zg_above_pct)}`);
  return parts.join(" · ");
}
function legCell(leg) {
  if (!leg) return "—";
  if (!isNum(leg.strike)) return h("span", { class: "muted small" }, "无合约");
  return h("span", {}, `${num(leg.strike)} · ${num(Math.abs(leg.delta ?? NaN), 2)}Δ`,
    h("span", { class: "sub" }, leg.quotable ? `单笔 ${pct(leg.yield_raw, 2)} · bid ${money(leg.bid)}` : `不可报价：${leg.reason || "—"}`));
}
