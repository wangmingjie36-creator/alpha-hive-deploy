// 账本：预注册检验的就绪度（只看样本量与标签计数，不看结果）+ 按档校准 + 按日期浏览账本行（盲期版）。
import { api, h, num, pct, seg, table, panel, isNum, ROUTE_SHORT, EARN_LABEL, TENOR_LABEL, fill } from "../lib.js";
import { lines, C } from "../charts.js";

const STATUS = { undetermined: "无法判定", accruing: "攒样本中", ready: "已就绪", error: "判定失败" };

export async function renderLedger(root, alive) {
  const [a, dates] = await Promise.all([api("/api/assess"), api("/api/ledger/dates")]);
  if (!alive()) return;
  const head = h("div", { class: "page-head" }, h("div", {},
    h("h1", {}, "账本与检验"),
    h("p", { class: "lede" }, "每个扫描日收盘后，日报钩子给每只标的记一行（月度、周度分开记），到期后补上到期收盘。「GEX 环境路由挑不挑得出更差的卖权环境」要等样本够了才检验一次，检验只跑一次、由日报钩子冻结。")));
  const sd = a.state_dir || {};
  const blocks = [head];
  if (sd.exists === false) blocks.push(h("p", { class: "callout warn" }, `没找到账本目录 ${sd.path}：${sd.hint || ""}`));
  blocks.push(h("p", { class: "callout" },
    "盲期规则：检验冻结之前，这里只显示样本量、标签与「报价可否」的计数，不显示任何结果；按日期浏览账本时结算字段整列隐藏。",
    "自己把某天的路由与其后的价格拼起来比较，等于偷看（预注册 §7），会作废这次检验。"));

  const cards = h("div", { class: "grid halves" });
  const calEls = [];
  for (const tenor of ["monthly", "weekly"]) {
    const t = a.tenors?.[tenor] || {};
    const pg = t.progress || {}; const need = t.need || {}; const ps = pg.per_side || {};
    const gate = (label, have, want) => {
      const ratio = isNum(have) && want ? Math.min(1, have / want) : 0;
      return h("div", { class: "gate" }, h("span", {}, label),
        h("div", { class: `bar ${ratio >= 1 ? "done" : ""}`, role: "progressbar", "aria-valuenow": String(have ?? 0), "aria-valuemax": String(want ?? 0), "aria-label": label },
          h("i", { style: { width: `${(ratio * 100).toFixed(1)}%` } })),
        h("span", { class: "num r small" }, `${have ?? 0} / ${want ?? "—"}`));
    };
    const calEl = h("div", { class: "chart short" });
    calEls.push([calEl, t.calibration]);
    const kv = (k, v) => h("tr", {}, h("td", {}, k), h("td", { class: "r" }, v));
    const fmtCounter = (o) => Object.entries(o || {}).map(([k, v]) => `${k || "—"} ${v}`).join("、") || "无";
    cards.append(panel(TENOR_LABEL[tenor], { sub: STATUS[t.status] || t.status || "—" },
      gate("独立单位", pg.n_independent, need.min_independent_per_tenor),
      gate("不同到期日", pg.n_distinct_expiries, need.min_distinct_expiries),
      gate("put · flag 组（信息块）", ps.put?.n_flagged_informative, need.min_per_group),
      gate("put · normal 组（信息块）", ps.put?.n_normal_informative, need.min_per_group),
      gate("call · flag 组（信息块）", ps.call?.n_flagged_informative, need.min_per_group),
      gate("call · normal 组（信息块）", ps.call?.n_normal_informative, need.min_per_group),
      h("div", { style: { height: "10px" } }),
      h("table", {}, h("tbody", {},
        kv("记录行 / 不可得", `${pg.n_recorded ?? 0} / ${Object.values(pg.n_unavailable || {}).reduce((x, y) => x + y, 0)}`),
        kv("已结算 / 待结算 / 放弃", `${pg.n_settled ?? 0} / ${pg.n_pending ?? 0} / ${pg.n_give_up ?? 0}`),
        kv(`待结算中超期 ${pg.pending_overdue_days ?? 5} 天以上`, `${pg.n_pending_overdue ?? 0}`),
        kv("财报污染排除的单位", `${pg.n_units_earnings_excluded ?? 0}`),
        kv("非收盘后报价（不进检验）", `${pg.n_rows_price_source_excluded ?? 0}`),
        kv("版本戳不符（不进检验）", `${pg.n_rows_version_excluded ?? 0}`),
        kv("路由不可用的行", `${pg.n_route_unavailable ?? 0}`),
        kv("不可得原因", fmtCounter(pg.n_unavailable)),
        kv("报价来源", fmtCounter(pg.price_source)))),
      h("p", { class: "note" }, t.summary || ""),
      t.frozen ? h("p", { class: "callout" }, `检验已于 ${t.frozen.ready_date} 冻结（${t.frozen.n_units} 个单位）。`, h("a", { href: "#/results" }, "看结果")) : null,
      h("h3", { style: { marginTop: "16px" } }, "按档校准：实际到期 ITM 频率 vs N(d2)"),
      calEl,
      h("p", { class: "note" }, "不分 flag，不破盲（预注册 §8）。卖方有波动率风险溢价时，实际频率应低于风险中性概率。")));
  }
  blocks.push(cards);

  // 浏览
  const browse = h("div", {});
  blocks.push(h("div", { style: { height: "20px" } }), browse);
  fill(root, ...blocks);
  const c = C();
  for (const [el, cal] of calEls) {
    const rungs = ["0.10", "0.16", "0.20", "0.25", "0.30"];
    const nTotal = rungs.reduce((s, k) => s + (cal?.put?.[k]?.n || 0) + (cal?.call?.[k]?.n || 0), 0);
    if (!nTotal) { el.replaceWith(h("div", { class: "empty small" }, "还没有已结算的单位。")); continue; }
    lines(el, { x: rungs, yFmt: (v) => pct(v, 0), yMin: 0, series: [
      { name: "put 实际", data: rungs.map((k) => cal.put?.[k]?.actual_itm_freq ?? null), color: c.neg, symbol: true },
      { name: "put N(d2)", data: rungs.map((k) => cal.put?.[k]?.mean_itm_prob_risk_neutral ?? null), color: c.neg, dash: true, symbol: true },
      { name: "call 实际", data: rungs.map((k) => cal.call?.[k]?.actual_itm_freq ?? null), color: c.pos, symbol: true },
      { name: "call N(d2)", data: rungs.map((k) => cal.call?.[k]?.mean_itm_prob_risk_neutral ?? null), color: c.pos, dash: true, symbol: true }] });
  }

  const ds = dates.dates || [];
  let date = ds[0] || null;
  let tenor = "monthly";
  async function drawBrowse() {
    if (!date) { fill(browse, panel("按日期浏览", {}, h("div", { class: "empty" }, "账本里还没有行。"))); return; }
    const d = await api(`/api/ledger/rows?date=${date}`);
    if (!alive()) return;
    const rows = d.tenors?.[tenor] || [];
    const blind = d.settlement_blinded;
    const cols = [
      { label: "代码", render: (r) => h("a", { href: `#/t/${r.ticker}/sell` }, r.ticker) },
      { label: "状态", render: (r) => (r.status === "recorded" ? "已记录" : `不可得：${r.unavailable_reason || "—"}`) },
      { label: "现价", cls: "r", render: (r) => num(r.underlying_price) },
      { label: "到期", render: (r) => `${r.expiry || "—"}${isNum(r.dte) ? `（${r.dte} 天）` : ""}` },
      { label: "路由 put / call", render: (r) => `${ROUTE_SHORT[r.route?.put] || "—"} / ${ROUTE_SHORT[r.route?.call] || "—"}` },
      { label: "0.20Δ put K", cls: "r", render: (r) => num(r.base_legs?.put?.strike) },
      { label: "0.20Δ call K", cls: "r", render: (r) => num(r.base_legs?.call?.strike) },
      { label: "财报", render: (r) => EARN_LABEL[r.earnings_status] || "—" },
      { label: "结算", render: (r) => r.settle_status || "—" },
    ];
    if (!blind) cols.push({ label: "到期收盘", cls: "r", render: (r) => num(r.expiry_close) });
    fill(browse, panel("按日期浏览", {
      sub: blind ? "盲期：到期收盘等结算字段不返回（全部期限的检验都冻结后才显示）" : "检验已全部冻结：显示结算字段",
      right: h("div", { class: "controls" },
        h("select", { class: "sel", "aria-label": "日期", onchange: (e) => { date = e.target.value; drawBrowse(); } },
          ds.map((x) => h("option", { value: x, selected: x === date }, x))),
        seg([["monthly", "月度"], ["weekly", "周度"]], tenor, (x) => { tenor = x; drawBrowse(); }, "期限")) },
    table(cols, rows)));
  }
  await drawBrowse();
}
