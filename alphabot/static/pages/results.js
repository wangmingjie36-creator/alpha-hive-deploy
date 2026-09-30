// 结果：预注册检验冻结后才有内容。冻结前只说「为什么锁着、还差多少」。
import { api, h, num, panel, table, TENOR_LABEL, fill } from "../lib.js";
import { lines, C } from "../charts.js";
import { tickers } from "../app.js";

const DECISION = { reject_h0: "拒绝 H0：被 flag 的环境里 0.20Δ 卖权显著更差", fail_to_reject_h0: "未能拒绝 H0：没有证据说路由挑得出更差的环境" };

export async function renderResults(root, ticker, alive) {
  const a = await api("/api/assess");
  if (!alive()) return;
  const head = h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "检验结果"),
    h("p", { class: "lede" }, "问题只有一个：被 GEX 环境路由标记（flag）的日子里，同样的 0.20Δ 卖权到期结果是不是更差。单侧、按记录日分块置换、四个检验 Bonferroni 校正，每个 α = 0.0125。")));
  const blocks = [head];
  for (const tenor of ["monthly", "weekly"]) {
    const t = a.tenors?.[tenor] || {};
    if (!t.test) {
      blocks.push(panel(TENOR_LABEL[tenor], { sub: "未冻结" },
        h("p", { class: "callout lock" }, t.awaiting_freeze
          ? "样本已达就绪闸，等今晚的日报钩子跑一次检验并冻结（前端不会、也不能替它跑）。"
          : "样本还没达到就绪闸，检验尚未运行。这不是「结果不好」，是「还不能看」。"),
        h("p", { class: "note" }, t.summary || ""), h("a", { href: "#/ledger" }, "看就绪进度")));
      blocks.push(h("div", { style: { height: "20px" } }));
      continue;
    }
    const rows = ["put", "call"].map((side) => ({ side, ...(t.test[side] || {}) }));
    blocks.push(panel(TENOR_LABEL[tenor], { sub: `冻结于 ${t.frozen?.ready_date || "—"} · ${t.frozen?.n_units ?? "—"} 个独立单位 · 只跑这一次` },
      table([
        { label: "侧", render: (r) => r.side },
        { label: "判定", render: (r) => h("b", {}, DECISION[r.decision] || r.decision || r.reason || "—") },
        { label: "flag − normal（pnl/权利金均值差）", cls: "r", render: (r) => num(r.observed, 3) },
        { label: "p 值", cls: "r", render: (r) => num(r.p, 4) },
        { label: "α", cls: "r", render: (r) => num(r.alpha_each, 4) },
        { label: "flag / normal 单位", cls: "r", render: (r) => `${r.n_flagged ?? "—"} / ${r.n_normal ?? "—"}` },
        { label: "信息块", cls: "r", render: (r) => r.n_informative_blocks ?? "—" },
      ], rows),
      h("p", { class: "note" }, "两组 0.20 档结果缺失率明显不同时，判定要打折看（缺失原因按 flag / normal 分列在账本页）。")));
    blocks.push(h("div", { style: { height: "20px" } }));
  }

  // 历史水平叠价格：全部 tenor 冻结后才开放
  const hist = h("div", {});
  blocks.push(hist);
  fill(root, ...blocks);
  if (!a.all_unblinded) {
    fill(hist, panel("历史水平与价格", {}, h("p", { class: "callout lock" },
      "全部期限的检验冻结之前，不提供把历史路由 / 水平与其后价格画在一起的图——那正是「自己拼结果、按 flag 比较」的捷径（预注册 §7）。")));
    return;
  }
  const t = (ticker || tickers()[0] || "").toUpperCase();
  const d = await api(`/api/history/${encodeURIComponent(t)}`);
  if (!alive()) return;
  if (d.locked) { fill(hist, panel("历史水平与价格", {}, h("p", { class: "callout lock" }, d.reason))); return; }
  const s = d.series?.monthly || [];
  const el = h("div", { class: "chart" });
  const c = C();
  fill(hist, panel(`${t} 历史水平与价格（月度行）`, { right: h("select", { class: "sel", onchange: (e) => { location.hash = `#/results/${e.target.value}`; } },
    tickers().map((x) => h("option", { value: x, selected: x === t }, x))) }, s.length ? el : h("div", { class: "empty" }, "没有记录。")));
  if (s.length) lines(el, { x: s.map((r) => r.date), yFmt: (v) => num(v, 0), series: [
    { name: "记录日现价", data: s.map((r) => r.underlying_price), color: c.ink },
    { name: "最近零点", data: s.map((r) => r.zg_nearest), color: c.ink3, dash: true },
    { name: "Major+", data: s.map((r) => r.net_major_pos_strike), color: c.pos, width: 1.5 },
    { name: "Major−", data: s.map((r) => r.net_major_neg_strike), color: c.neg, width: 1.5 }] });
}
