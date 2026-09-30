// 口径：单位、符号约定、局限、收益口径、路由规则、证据等级、与 GEXBot 的差异。数值都取自服务端（代码里那一份）。
import { api, h, num, table, fill } from "../lib.js";

export async function renderMethod(root, alive) {
  const m = await api("/api/method");
  if (!alive()) return;
  const r = m.route || {}; const L = m.ladder || {};
  const code = (x) => h("code", {}, x);
  fill(root, h("article", { class: "prose" },
    h("h1", {}, "口径与局限"),
    h("p", {}, m.disclaimer?.replace(/\*\*/g, "") || ""),

    h("h2", {}, "这些数字是什么"),
    h("ul", {},
      h("li", {}, h("b", {}, "GEX"), "：每张合约 sign·γ·OI·100·S²·0.01，单位是「现价每变动 1% 对应的美元」。call 记正、put 记负——即假设做市商多 call、空 put（朴素 OI 口径，GEXBot 的 Classic 也是这个口径，它自己称之为 naive）。"),
      h("li", {}, h("b", {}, "DEX"), "：Δ·OI·100·S，", h("b", {}, "持有者口径"), "（call 为正、put 为负），与 GEX 的做市商符号相反，不能加在一起。"),
      h("li", {}, h("b", {}, "Vanna / Charm"), "：做市商符号；vanna 是每 1 个波动率点的美元 delta，charm 是每个日历日的美元 delta。"),
      h("li", {}, h("b", {}, "Zero Gamma"), "：每张合约固定自身 IV，在现价 ±20% 的 81 个假想价格上重算总 GEX，找曲线过零点。不是「相邻行权价净 GEX 变号」——后者几乎总落在现价旁边。"),
      h("li", {}, h("b", {}, "净 Major+ / Major−"), "：净 GEX 最大为正 / 最小为负的行权价。旧「call wall / put wall」是单边极值，另列为「单边极值」，两者不是一个量。"),
      h("li", {}, h("b", {}, "P(ITM)"), "：到期 ITM 的风险中性概率 N(d2)，不是 |Δ|。")),

    h("h2", {}, "证据等级"),
    table([{ label: "用法", render: (x) => x[0] }, { label: "等级", render: (x) => h("b", {}, x[1]) }, { label: "本系统怎么用", render: (x) => x[2] }], [
      ["到期 ITM 概率 = N(d2)", "A", "梯子按 |Δ| 选档，同时记 N(d2) 与 σ 距离"],
      ["卖权溢价长期为正（VRP）", "A / 个股偏弱", "不检验；它是实际 ITM 频率低于风险中性概率的原因"],
      ["做市商净多 gamma → 随后实现波动更低", "B+", "路由的理论依据：负 gamma 环境更该退后"],
      ["Zero Gamma 作为分界线", "C", "路由用它，所以才要预注册检验"],
      ["call / put wall 是硬支撑阻力", "D", "只展示，不进路由、不进检验"]]),

    h("h2", {}, "环境路由（冻结规则 v", String(r.rule_version ?? "—"), "）"),
    h("ol", {},
      h("li", {}, "读 ", code(r.view || "le_45dte"), " 视图；扫描用到的合约少于 ", String(r.min_sweep_contracts ?? "—"), " 张 ⇒ 两侧不可用。"),
      h("li", {}, "现价处净 gamma 为负 ⇒ put、call 都退到远档 ", num(r.far_rung, 2), "Δ。"),
      h("li", {}, "为正时：下方最近零点距现价 ≤ ", num(r.flip_buffer_pct, 1), "% ⇒ put 退到远档，否则基线档 ", num(r.base_rung, 2), "Δ；call 一律基线档。")),
    h("p", {}, "路由只决定「取梯子的哪一档」，不产出任何加减分，也不进 Alpha Hive 的评分。梯子各档：",
      (L.deltas || []).map((x) => num(x, 2)).join(" / "), "；|Δ| 容差 ", num(L.delta_tol, 2),
      "；短腿点差超过 ", num((L.max_spread_pct || 0) * 100, 0), "% 不可报价；价差保护腿离短腿 ≥ ", num(L.wing_width_sigma, 1), "σ。"),

    h("h2", {}, "收益口径"),
    h("p", {}, (m.yield_note || "").replace(/\*\*/g, "")),

    h("h2", {}, "局限"),
    h("ul", {}, (m.caveats || []).map((x) => h("li", {}, x.replace(/\*\*/g, "")))),

    h("h2", {}, "盲期：为什么有些东西不给看"),
    h("p", {}, "前向账本在攒样本，检验「被 flag 的环境里卖权是不是更差」要等样本够了由日报钩子跑一次、冻结。冻结之前，Alpha Bot 不显示账本行的到期结果、不按 flag 分组比较、也不把历史路由 / 水平和其后的价格画在同一张图上。信息论上盲不了（路由与价格都是公开的），所以这是行为规则：自己拼出来比较，就是偷看，会作废这次检验。盘中时序只看单日也是这个原因。"),

    h("h2", {}, "与 GEXBot 的差别"),
    h("ul", {},
      h("li", {}, "数据：CBOE 15 分钟延迟快照 + 前一交易日 OI；没有逐笔成交，所以不做 GEXBot 的 State / Orderflow（靠成交分类推断仓位）。"),
      h("li", {}, "Zero Gamma 用重定价扫描；成交量口径的 GEX 只作展示（成交量看不出买卖方向）。"),
      h("li", {}, "GEXBot 没有的：delta 梯子、六种结构报价、到期盈亏、环境路由与预注册检验。")),

    h("h2", {}, "运行边界"),
    h("ul", {},
      h("li", {}, "只在本机 127.0.0.1 上运行，不上网站；对卖权账本只读，冻结检验的唯一写者是日报钩子。"),
      h("li", {}, "现算结果缓存 60 秒；CBOE 每只约 1.5MB、串行 4–7 秒，所以总览读账本、不逐只现拉。"),
      h("li", {}, "盘中快照写在 Alpha Bot 自己的状态目录，只存聚合数值。"))));
}
