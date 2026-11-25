// 「水平」子页 · Zero Gamma 日间拆解（v0.45.816）：两份当日原始链存档逐项替换，看是哪一项把 Zero Gamma 推动的。
// 只排版：结论、状态文字、阈值都来自 /api/zg-decompose（规则唯一真相在 alphabot/zg_decompose.py）。
import { h, num, usdShort, signedPct, table, panel, isNum } from "../lib.js";

const REASON_ZH = {
  no_current_archive: "这只票还没有当日原始链存档（存档从 v0.45.815 起、Alpha Bot 盘中快照落盘时才写）。",
  no_previous_archive: "只有一天的存档，要等下一个交易日有第二份才能比。",
  archive_missing: "存档文件不见了。",
  archive_corrupt: "存档文件读坏了。",
  underlying_price_unavailable: "存档里没有现价，算不了。",
};

function stateTxt(s) {
  if (!s) return "—";
  if (s.curve_state === "crosses") return isNum(s.nearest) ? `零点 ${num(s.nearest)}` : "过零";
  return { all_positive: "全段为正", all_negative: "全段为负", insufficient_contracts: "合约不足" }[s.curve_state] || s.curve_state;
}

// 单换这一项后和出发点一样 ⇒ 写明「没动」，免得读者以为表里重复的数是笔误
function same(s, base) {
  const t = stateTxt(s);
  return t === stateTxt(base) ? h("span", { class: "muted" }, `${t}（没动）`) : h("b", {}, t);
}

export function zgDecompBody(r, view) {
  const rules = r.rules || {};
  const labels = rules.factor_labels || {};
  const vlabels = rules.verdict_labels || {};
  if (!r.available) {
    const why = REASON_ZH[r.reason] || (String(r.reason || "").startsWith("exception") ? `拆解出错：${r.reason}` : `不可得：${r.reason}`);
    return [h("div", { class: "empty" }, why),
      r.bad_date ? h("p", { class: "small muted" }, `出问题的是 ${r.bad_date} 的存档${r.error ? "：" + r.error : ""}`) : null];
  }
  const v = r.views?.[view];
  if (!v) return [h("div", { class: "empty" }, "这个视图没有拆解结果。")];
  const apart = r.trading_days_apart;
  const gap = apart == null ? "（交易日历判不了隔几天）" : apart > 1 ? `（中间隔了 ${apart} 个交易日，不是相邻两天）` : "";
  const asOf = r.cur_final ? "" : `；今天的存档截至 ${(r.cur_as_of || "—").replace("T", " ")}，盘中还会被覆盖`;
  const cols = [
    { label: "这一项", render: (x) => labels[x.f] || x.f },
    { label: "从昨天只换它", render: (x) => same(v.forward[x.f].state, v.prev) },
    { label: "从今天只换回它", render: (x) => same(v.backward[x.f].state, v.cur) },
  ];
  const factors = rules.factors || Object.keys(v.forward);
  const mt = v.matching || {};
  const so = v.strike_oi || [];
  const SRC = { cboe: "CBOE", yfinance: "Yahoo 备用链" };
  return [
    r.source_changed ? h("p", { class: "callout warn" },
      `两份存档的取数源不同（${r.prev_date} ${SRC[r.source_prev] || r.source_prev} / ${r.date} ${SRC[r.source_cur] || r.source_cur}）：`,
      "两家的 IV 与 OI 口径不一样，下面拆出来的 IV / OI 变化可能就是换源本身，不能当成市场变了。") : null,
    h("p", { class: `callout ${v.highlight ? "warn" : ""}` },
      h("b", {}, vlabels[v.verdict?.verdict] || v.verdict?.verdict, "："), v.headline),
    h("p", { class: "small muted" },
      `${r.prev_date}（现价 ${num(r.spot_prev)}）→ ${r.date}（现价 ${num(r.spot_cur)}）${gap}${asOf}。`,
      v.flags?.expiry_rolled && view === "next_expiry" ? " ⚠️ 两天比的是不同的到期日，合约集合一项按构造必然占主导，优先看 ≤45 天视图。" : null),
    table(cols, factors.map((f) => ({ f }))),
    h("p", { class: "small muted" },
      `基准：昨天全取 ${stateTxt(v.prev)}，今天全取 ${stateTxt(v.cur)}`,
      isNum(v.nearest_move_pct) ? `，最近零点移动 ${signedPct(v.nearest_move_pct)}（占今天现价）` : "",
      `。合约对齐：两天都有 ${mt.n_common ?? "—"}，只在昨天 ${mt.n_only_prev ?? "—"}，只在今天 ${mt.n_only_cur ?? "—"}`,
      mt.n_common_iv_missing ? `；其中 ${mt.n_common_iv_missing} 张有一天缺 IV（不进扫描）` : "", "。"),
    so.length ? h("div", {},
      h("h3", {}, "OI 变化最大的行权价（只把两天都有的合约的 OI 换回昨天，其余按今天）"),
      table([
        { label: "行权价", cls: "r", render: (x) => num(x.strike, 1) },
        { label: "净 GEX 变化 / 1%", cls: "r", render: (x) => usdShort(x.delta_net_gex_usd_per_1pct) },
        { label: "call OI 变化", cls: "r", render: (x) => num(x.delta_call_oi, 0) },
        { label: "put OI 变化", cls: "r", render: (x) => num(x.delta_put_oi, 0) },
      ], so)) : null,
    h("p", { class: "note" }, rules.read_me || ""),
  ];
}

export function zgDecompPanel(content) {
  return panel("Zero Gamma 日间拆解", { sub: "两份当日原始链存档逐项替换：合约集合 / OI / IV / 剩余时间 / 现价 · 证据 C · 只本机计算" }, content);
}
